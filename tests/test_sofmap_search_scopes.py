"""Synthetic regressions for the retained empty and grouped USED contracts.

The 2026-10-04 receipts establish three unfiltered empty results and two
grouped USED results (one and three cards). No captured HTML, real product SKU,
HTTP request, or browser is needed here. Listing cards only discover work.
"""
from copy import deepcopy
import hashlib
from html import escape
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, quote, urlsplit

from sale_monitor.http import FetchError
from sale_monitor.models import Offer
from sale_monitor.parsing import canonical
from sale_monitor.reporting import checkpoint
from sale_monitor.runner import Collector
from sale_monitor.sofmap_search import deferred_url, parse_listing
from sale_monitor.storage import read_json
from sofmap_fixtures import CFG, HOST, JAN, OBSERVED, OTHER_JAN, SCRIPT, page, shell
from test_sofmap_search import FakeClient, OLD


EMPTY = '<span class="span_redpart txt-red">該当商品がありませんでした。</span>'
PRODUCTS = tuple(HOST + '/product_detail.aspx?sku=' + sku for sku in ('111', '222', '333'))
RUN = 'sofmap-scope-fixture'
USED_SCRIPT = '\n'.join(
    line for line in SCRIPT.replace('strProductType="ALL"', 'strProductType="USED"').splitlines()
    if 'document.cookie =' not in line
)


def search_url(query=JAN, *, used=False):
    url = HOST + '/search_result.aspx?keyword=' + quote(query, safe='')
    return url + '&new_jan=' + query + '&product_type=USED' if used else url


def parts_url(query=JAN, *, used=False, initial=True):
    filters = 'product_type=USED&new_jan=' + query + '&' if used else ''
    url = HOST + '//product_list_parts.aspx?' + filters + 'keyword=' + quote(query, safe='')
    return url + '&is_page=serch_result&isFirst=true' if initial else url


def used_shell(query=JAN, *, script=USED_SCRIPT):
    body = shell(query, script=script)
    body = body.replace('name="ALL" class="current"', 'name="ALL"')
    body = body.replace('name="USED" href=', 'name="USED" class="current" href=')
    body = body.replace('product_type=USED&amp;keyword=',
                        'product_type=USED&amp;new_jan=' + query + '&amp;keyword=')
    # The captured source's ALL input is a form placeholder. Its selected tab
    # and initial loader establish USED; changing the placeholder to USED would
    # hide the source contract that these regressions must exercise.
    return body.replace('<input type="hidden" name="product_type" value="ALL">',
                        '<input type="hidden" name="new_jan" value="' + query + '">'
                        '<input type="hidden" name="product_type" value="ALL">')


def source(query=JAN, *, used=False, body=None, url=None, status=200):
    return page(query, body=body if body is not None else (used_shell(query) if used else shell(query)),
                url=url or search_url(query, used=used), status=status)


def fragment(query=JAN, *, used=False, count=0, body=None, url=None, status=200):
    product_type = 'USED' if used else 'ALL'
    field = '<input type="hidden" name="product_type" value="' + product_type + '">'
    new_jan = '<input type="hidden" name="new_jan" value="' + query + '">' if used else ''
    cards = ''.join('<li><a class="product_name" href="' + escape(product, quote=True) + '">'
                    'Synthetic item ' + str(i) + '</a><span class="price">9,999円</span></li>'
                    for i, product in enumerate(PRODUCTS[:count], 1))
    count_area = '<p class="pg_number_set">' + (f'<span>{count}</span>' if count else '') + '</p>'
    text = ('<section class="list_settings"><form name="search" method="get" action="/product_list.aspx">'
            + new_jan + field + '<input name="keyword" value="' + query + '">'
            '<input type="hidden" name="styp" value="p_srt"></form></section>'
            + count_area + '<ol class="paging_bl"></ol>'
            '<ul id="change_style_list" class="product_list flexcartbtn ftbtn">'
            + field + (cards if count else EMPTY) + '</ul>'
            + count_area + '<ol class="paging_bl"></ol>')
    return page(query, body=text if body is None else body,
                url=url or parts_url(query, used=used), status=status)


class SofmapRetainedScopeParsing(unittest.TestCase):
    def assert_unverified(self, responses, query=JAN):
        for label, response in responses.items():
            with self.subTest(case=label), self.assertRaisesRegex(FetchError, 'comparison_search_response_unverified'):
                parse_listing(response, query)

    def assert_source_rejected(self, bodies, *, query=JAN):
        for label, body in bodies.items():
            with self.subTest(case=label), self.assertRaises(ValueError):
                deferred_url(source(query, used=True, body=body), query, CFG)

    def test_unfiltered_explicit_empty_has_bound_query_and_receipt(self):
        for query in (JAN, OTHER_JAN, '1234567890123'):
            with self.subTest(query=query):
                response = fragment(query)
                products, dependencies, proof = parse_listing(response, query)
                self.assertEqual(([], []), (products, dependencies))
                self.assertEqual('no_results', proof['result'])
                self.assertEqual('sofmap_explicit_empty_fragment', proof['method'])
                self.assertEqual({'keyword': query}, proof['search_scope'])
                self.assertEqual(query, proof['query'])
                self.assertEqual(response.url, proof['url'])
                self.assertEqual(OBSERVED, proof['observed_at'])
                self.assertEqual(200, proof['http_status'])
                self.assertEqual(hashlib.sha256(response.body).hexdigest(), proof['content_hash'])

    def test_empty_requires_exact_initial_unfiltered_fragment_scope(self):
        url = parts_url()
        bad = {'search page': search_url(), 'missing initial': url.replace('&isFirst=true', ''),
               'false initial': url.replace('isFirst=true', 'isFirst=false'),
               'wrong page kind': url.replace('serch_result', 'search_result'),
               'pagination': url + '&page=2', 'sort': url + '&order_by=PRICE',
               'explicit ALL': url + '&product_type=ALL', 'USED': parts_url(used=True),
               'duplicate keyword': url + '&keyword=' + JAN,
               'unknown filter': url + '&category=memory', 'malformed': url + '&bad=%ZZ',
               'different query': url.replace(JAN, OTHER_JAN)}
        self.assert_unverified({k: fragment(url=v) for k, v in bad.items()})

    def test_empty_shell_missing_marker_and_http_failures_stay_unverified(self):
        body = fragment().text
        bad = {'bare empty': fragment(body=''), 'shell': source(),
               'empty UL': fragment(body=body.replace(EMPTY, '')),
               '503': fragment(status=503), '403': fragment(status=403),
               'challenge': fragment(body='<html><title>Access denied</title>Verify you are human</html>'),
               'marker alone': fragment(body=EMPTY),
               'generic text': fragment(body=body.replace(EMPTY, '該当商品がありませんでした。'))}
        self.assert_unverified(bad)

    def test_empty_marker_must_be_visible_direct_list_child(self):
        base = fragment().text
        variants = {
            'hidden marker': EMPTY.replace('<span ', '<span hidden '),
            'aria hidden marker': EMPTY.replace('<span ', '<span aria-hidden="true" '),
            'display none': EMPTY.replace('<span ', '<span style="display:none" '),
            'invisible': EMPTY.replace('<span ', '<span style="visibility:hidden" '),
            'nested': '<div>' + EMPTY + '</div>', 'aside': '<aside>' + EMPTY + '</aside>',
            'script': '<script type="text/html">' + EMPTY + '</script>',
            'comment': '<!--' + EMPTY + '-->', 'template': '<template>' + EMPTY + '</template>',
            'duplicate': EMPTY + EMPTY,
            'wrong class': EMPTY.replace('span_redpart', 'advertisement'),
            'wrong text': EMPTY.replace('ありませんでした', '見つかりません'),
        }
        responses = {k: fragment(body=base.replace(EMPTY, v)) for k, v in variants.items()}
        responses['outside advertisement'] = fragment(body=base.replace(EMPTY, '') + '<aside>' + EMPTY + '</aside>')
        responses['hidden UL'] = fragment(body=base.replace('<ul id=', '<ul hidden id='))
        responses['style'] = fragment(body='<style>.span_redpart{display:none}</style>' + base)
        responses['non-leaf marker'] = fragment(body=base.replace('該当商品がありませんでした。', '<b>該当商品がありませんでした。</b>'))
        self.assert_unverified(responses)

    def test_empty_cannot_conflict_with_products_counts_or_pagination(self):
        base = fragment().text
        card = '<li><a class="product_name" href="' + PRODUCTS[0] + '">Product</a></li>'
        next_link = '<a href="' + parts_url() + '&amp;page=2">2</a>'
        variants = {'card': base.replace(EMPTY, EMPTY + card),
                    'positive count': base.replace('<p class="pg_number_set"></p>', '<p class="pg_number_set"><span>1</span></p>'),
                    'written total': base.replace('<p class="pg_number_set"></p>', '<p class="pg_number_set">全1点</p>'),
                    'unsupported zero count': base.replace('<p class="pg_number_set"></p>', '<p class="pg_number_set"><span>0</span></p>'),
                    'missing count areas': base.replace('<p class="pg_number_set"></p>', ''),
                    'pagination': base.replace('<ol class="paging_bl"></ol>', '<ol class="paging_bl"><li>' + next_link + '</li></ol>')}
        self.assert_unverified({k: fragment(body=v) for k, v in variants.items()})

    def test_empty_requires_unambiguous_query_and_all_product_type_fields(self):
        base = fragment().text
        keyword = '<input name="keyword" value="' + JAN + '">'
        all_field = '<input type="hidden" name="product_type" value="ALL">'
        variants = {'wrong query': base.replace(keyword, keyword.replace(JAN, OTHER_JAN)),
                    'duplicate query': base.replace(keyword, keyword * 2),
                    'no query': base.replace(keyword, ''),
                    'no ALL': base.replace(all_field, ''),
                    'mixed type': base.replace(all_field, all_field.replace('ALL', 'USED'), 1),
                    'duplicate primary type': base.replace(all_field, all_field * 2, 1),
                    'unexpected group': base.replace(keyword, keyword + '<input name="new_jan" value="' + JAN + '">'),
                    'POST form': base.replace('method="get"', 'method="post"'),
                    'wrong action': base.replace('action="/product_list.aspx"', 'action="/search_result.aspx"'),
                    'foreign action': base.replace('action="/product_list.aspx"', 'action="https://elsewhere.invalid/product_list.aspx"'),
                    'action filter': base.replace('action="/product_list.aspx"', 'action="/product_list.aspx?product_type=USED"'),
                    'hidden form': base.replace('<form ', '<form hidden '),
                    'second form': base + '<section class="list_settings"><form>' + keyword + '</form></section>',
                    'duplicate UL': base + '<ul id="change_style_list" class="product_list"></ul>',
                    'base redirect': '<base href="https://elsewhere.invalid/">' + base}
        self.assert_unverified({k: fragment(body=v) for k, v in variants.items()})

    def test_used_source_placeholder_all_is_overridden_only_by_bound_used_contract(self):
        for query in (JAN, OTHER_JAN):
            with self.subTest(query=query):
                response = source(query, used=True)
                url, proof = deferred_url(response, query, CFG)
                self.assertEqual(parts_url(query, used=True), url)
                self.assertEqual({'keyword': query, 'new_jan': query, 'product_type': 'USED',
                                  'is_page': 'serch_result', 'isFirst': 'true'}, dict(parse_qsl(urlsplit(url).query)))
                self.assertEqual('USED', proof['anchor']['name'])
                self.assertEqual({'keyword': query, 'new_jan': query, 'product_type': 'USED'}, proof['search_scope'])
                self.assertEqual(query, proof['query']['requested'])
                self.assertEqual(response.url, proof['source_url'])
                self.assertEqual(url, proof['fragment_url'])
                self.assertEqual('GET', proof['loader']['method'])
                self.assertEqual({'isFirst': True}, proof['loader']['get_data'])
                self.assertEqual(hashlib.sha256(response.body).hexdigest(), proof['content_hash'])

    def test_used_source_rejects_unknown_duplicate_and_malformed_url_filters(self):
        url = search_url(used=True)
        variants = {'unknown': url + '&category=memory', 'page': url + '&page=2',
                    'duplicate group': url + '&new_jan=' + JAN, 'duplicate type': url + '&product_type=USED',
                    'duplicate keyword': url + '&keyword=' + JAN,
                    'malformed escape': url.replace('product_type=USED', 'product_type=%ZZ'),
                    'bare field': url + '&unsupported', 'blank type': url.replace('product_type=USED', 'product_type='),
                    'fragment': url + '#scope'}
        for label, target in variants.items():
            with self.subTest(case=label), self.assertRaises(ValueError):
                deferred_url(source(used=True, url=target), JAN, CFG)

    def test_used_source_requires_same_thirteen_digit_keyword_and_new_jan(self):
        for query in ('123456789012', '12345678901234', 'abcdefghijkl3', '１２３４５６７８９０１２３'):
            with self.subTest(query=query), self.assertRaises(ValueError):
                deferred_url(source(query, used=True), query, CFG)
        for target in (search_url(used=True).replace('new_jan=' + JAN, 'new_jan=' + OTHER_JAN),
                       search_url(used=True).replace('&new_jan=' + JAN, '')):
            with self.subTest(url=target), self.assertRaises(ValueError):
                deferred_url(source(used=True, url=target), JAN, CFG)
        base = used_shell()
        field = '<input type="hidden" name="new_jan" value="' + JAN + '">'
        self.assert_source_rejected({'missing form group': base.replace(field, ''),
                                     'wrong form group': base.replace(field, field.replace(JAN, OTHER_JAN)),
                                     'duplicate form group': base.replace(field, field * 2)})

    def test_used_source_selected_tab_and_loader_cannot_disagree(self):
        base = used_shell()
        wrong_tab = base.replace('name="USED" class="current"', 'name="USED"')
        self.assert_source_rejected({
            'selected ALL': wrong_tab.replace('name="ALL"', 'name="ALL" class="current"'),
            'no selected': wrong_tab,
            'two selected': base.replace('name="ALL"', 'name="ALL" class="current"'),
            'initial ALL': base.replace('strProductType="USED"', 'strProductType="ALL"'),
            'duplicate selection': base.replace('var strProductType="USED";', 'var strProductType="USED"; var strProductType="ALL";'),
            'POST': base.replace('type:"GET"', 'type:"POST"'),
            'initial false': base.replace('var isFirst = true;', 'var isFirst = false;'),
            'second initialization': base.replace('var isFirst = true;', 'var isFirst = true; isFirst = false;'),
        })

    def test_used_source_cannot_reintroduce_loader_cookie_assignments(self):
        base = used_shell()
        self.assert_source_rejected({
            'ptag': base.replace('e.preventDefault();', 'e.preventDefault(); document.cookie = \'ptag=USED\';'),
            'rparam': base.replace('CreateLoadingFlame(strTargetFlame);',
                                  "CreateLoadingFlame(strTargetFlame); document.cookie = 'rparam=x';"),
            'pparam': base.replace('CreateLoadingFlame(strTargetFlame);',
                                  "CreateLoadingFlame(strTargetFlame); document.cookie = 'pparam=x';"),
            'legacy cookie loader': used_shell(script=SCRIPT.replace('strProductType="ALL"', 'strProductType="USED"')),
        })

    def test_used_initial_selection_rejects_bare_conditional_and_mutated_writes(self):
        base = used_shell()
        initial = 'var strProductType="USED";'
        self.assertEqual(1, base.count(initial))
        mutations = {
            'bare single quotes': initial + " strProductType='ALL';",
            'bare double quotes': initial + ' strProductType="ALL";',
            'conditional reassignment': initial + " if(true) { strProductType='ALL'; }",
            'conditional declaration': 'if(false) { ' + initial + ' }',
            'mutated initializer': 'var strProductType = true ? "ALL" : "USED";',
            'dynamic redeclaration': initial + ' var strProductType = chooseProductType();',
        }
        self.assert_source_rejected({label: base.replace(initial, value)
                                     for label, value in mutations.items()})

    def test_used_initial_selection_rejects_assignment_in_second_inline_script(self):
        base = used_shell()
        # Neither script mentions GetSearchParts/isFirst: the assignment must
        # still participate in validation of the initial selection contract.
        mutations = {}
        for label, code in {
            'bare assignment': "strProductType='ALL';",
            'declaration': "var strProductType='ALL';",
            'conditional assignment': "if(true) { strProductType='ALL'; }",
        }.items():
            tag = '<script>' + code + '</script>'
            mutations[label + ' before'] = base.replace('<script>', tag + '<script>', 1)
            mutations[label + ' after'] = base.replace('</body>', tag + '</body>')
        self.assert_source_rejected(mutations)

    def test_used_initial_selection_keeps_comments_and_response_local_variable_valid(self):
        # The retained response callback has a separate local strProductType.
        # Its dynamic value updates displayed tabs after the request, and must
        # not be mistaken for a write to the initial ready-handler selection.
        callback = '''success: function(data, status) {
            var strProductType = jQuery(data).find("input[name='product_type']").val();
            jQuery("section#search_result_area ul.tab_list li a").each(function(){
                jQuery(this).removeClass("current");
                if(jQuery(this).attr("name") == strProductType) {
                    jQuery(this).addClass("current");
                }
            });'''
        script = USED_SCRIPT.replace('success: function(data, status) {', callback)
        script = script.replace('var strProductType="USED";',
                                'var strProductType="USED"; /* strProductType="ALL"; */')
        response = source(used=True, body=used_shell(script=script))
        url, proof = deferred_url(response, JAN, CFG)
        self.assertEqual(parts_url(used=True), url)
        self.assertEqual('USED', proof['loader']['initial_product_type'])
        self.assertEqual({'keyword': JAN, 'new_jan': JAN, 'product_type': 'USED'}, proof['search_scope'])

    def test_used_tab_href_must_preserve_every_original_filter(self):
        base = used_shell()
        href = escape(parts_url(used=True, initial=False), quote=True)
        self.assertIn(href, base)
        alternatives = {'dropped group': href.replace('new_jan=' + JAN + '&amp;', ''),
                        'dropped type': href.replace('product_type=USED&amp;', ''),
                        'wrong group': href.replace('new_jan=' + JAN, 'new_jan=' + OTHER_JAN),
                        'NEW': href.replace('product_type=USED', 'product_type=NEW'),
                        'unknown filter': href + '&amp;category=memory',
                        'duplicate': href + '&amp;product_type=USED',
                        'foreign': href.replace(HOST, 'https://elsewhere.invalid')}
        self.assert_source_rejected({k: base.replace(href, v) for k, v in alternatives.items()})

    def test_used_one_and_three_cards_are_only_discovery_metadata(self):
        for count in (1, 3):
            with self.subTest(count=count):
                response = fragment(used=True, count=count)
                products, dependencies, proof = parse_listing(response, JAN)
                self.assertEqual(list(PRODUCTS[:count]), [p['url'] for p in products])
                self.assertEqual([], dependencies)
                self.assertEqual('results', proof['result'])
                self.assertEqual(count, proof['returned_count'])
                self.assertEqual(count, proof['displayed_result_count'])
                self.assertEqual(JAN, proof['query'])
                self.assertEqual({'keyword': JAN, 'new_jan': JAN, 'product_type': 'USED'}, proof['search_scope'])
                self.assertEqual(response.url, proof['url'])
                for product in products:
                    self.assertEqual({'url', 'title', 'source', 'kind'}, set(product))
                    self.assertEqual(response.url, product['source'])
                    self.assertEqual('comparison', product['kind'])

    def test_used_fragment_requires_consistent_type_and_group_in_primary_form(self):
        base = fragment(used=True, count=1).text
        group = '<input type="hidden" name="new_jan" value="' + JAN + '">'
        kind = '<input type="hidden" name="product_type" value="USED">'
        variants = {'missing group': base.replace(group, ''),
                    'wrong group': base.replace(group, group.replace(JAN, OTHER_JAN)),
                    'duplicate group': base.replace(group, group * 2),
                    'missing type': base.replace(kind, ''),
                    'primary ALL': base.replace(kind, kind.replace('USED', 'ALL'), 1),
                    'list ALL': base.replace('<ul id=', '<input name="product_type" value="ALL"><ul id='),
                    'duplicate primary type': base.replace(kind, kind * 2, 1),
                    'POST form': base.replace('method="get"', 'method="post"'),
                    'wrong action': base.replace('action="/product_list.aspx"', 'action="/search_result.aspx"'),
                    'foreign action': base.replace('action="/product_list.aspx"', 'action="https://elsewhere.invalid/product_list.aspx"'),
                    'action filter': base.replace('action="/product_list.aspx"', 'action="/product_list.aspx?product_type=ALL"'),
                    'hidden form': base.replace('<form ', '<form hidden '),
                    'wrong keyword': base.replace('name="keyword" value="' + JAN, 'name="keyword" value="' + OTHER_JAN)}
        self.assert_unverified({k: fragment(used=True, count=1, body=v) for k, v in variants.items()})

    def test_used_fragment_cannot_drop_filters_redirect_or_resolve_unsupported_pages(self):
        url = parts_url(used=True)
        variants = {'ALL': url.replace('product_type=USED', 'product_type=ALL'),
                    'no type': url.replace('product_type=USED&', ''),
                    'no group': url.replace('new_jan=' + JAN + '&', ''),
                    'wrong group': url.replace('new_jan=' + JAN, 'new_jan=' + OTHER_JAN),
                    'page': url + '&page=2', 'sort': url + '&order_by=DEFAULT',
                    'unknown': url + '&category=memory', 'duplicate': url + '&product_type=USED',
                    'error redirect': HOST + '/error/index.aspx',
                    'foreign': url.replace(HOST, 'https://elsewhere.invalid')}
        self.assert_unverified({k: fragment(used=True, count=1, url=v) for k, v in variants.items()})

    def test_used_shell_empty_marker_and_conflicting_card_count_are_not_results(self):
        base = fragment(used=True, count=1).text
        self.assert_unverified({
            'shell': source(used=True), 'empty marker': fragment(used=True),
            'bare empty': fragment(used=True, body='<html></html>'),
            'positive without pagination': fragment(used=True, count=1, body=base.replace('<span>1</span>', '<span>2</span>')),
            'marker with cards': fragment(used=True, count=1, body=base.replace('</ul>', EMPTY + '</ul>')),
            '403': fragment(used=True, count=1, status=403),
            '503': fragment(used=True, count=1, status=503),
        })

    def test_used_pagination_and_other_group_dependencies_keep_exact_scopes(self):
        next_url = parts_url(used=True) + '&page=2&order_by=DEFAULT'
        group_url = search_url() + '&new_jan=' + JAN + '&product_type=NEW'
        body = fragment(used=True, count=1).text.replace('<span>1</span>', '<span>2</span>')
        body = body.replace('</li>', '<div class="used_box"><a href="' + escape(group_url, quote=True) + '">New variants</a></div></li>')
        body += '<ol class="paging_bl"><li><a href="' + escape(next_url, quote=True) + '">2</a></li></ol>'
        _, dependencies, proof = parse_listing(fragment(used=True, count=1, body=body), JAN)
        self.assertEqual({canonical(next_url), canonical(group_url)}, {d['url'] for d in dependencies})
        self.assertEqual([canonical(next_url)], proof['pagination'])
        self.assertEqual([canonical(group_url)], proof['group_dependencies'])
        self.assertTrue(all(d['query'] == JAN for d in dependencies))
        self.assert_unverified({'pagination pending': fragment(used=True, count=1, url=next_url),
                               'NEW pending': fragment(used=True, count=1, url=group_url)})


class SofmapRetainedScopeCollection(unittest.TestCase):
    def setUp(self):
        parent = Path(os.environ.get('CHECKPOINT_TEST_TMP_DIR', tempfile.gettempdir())).resolve()
        temporary = tempfile.TemporaryDirectory(prefix='sofmap-scopes-', dir=parent)
        self.root = Path(temporary.name)
        self.assertEqual(parent, self.root.parent)
        self.addCleanup(temporary.cleanup)

    def collector(self, responses, *, root=None, run=RUN):
        config = {'stores': {'sofmap': {'adapter': 'html', **deepcopy(CFG)}}, 'seller_aliases': {}}
        collector = Collector(root or self.root, 'sofmap', config, run, FakeClient(responses))
        collector.state.update(list_pages=0, listed_candidates=0, cycle_complete=False)
        collector.new_run = False
        return collector

    def task(self, *, used=False, **changes):
        # Exercise both stored field spellings used by the retained tasks.
        query_field = {'query': JAN, 'depth': 1} if used else {'search_query': JAN, 'depth': 0}
        return {'type': 'list', 'kind': 'comparison', 'url': search_url(used=used),
                'sale_page': False, 'created_at': OLD, 'attempts': 5, 'priority': 1,
                'last_error': 'comparison_search_response_unverified', **query_field, **changes}

    def collect_one(self, collector):
        # Bound one real scheduling/process/save iteration without wall-clock races
        # or allowing the following product fetch to masquerade as listing proof.
        with patch('sale_monitor.runner.time.monotonic', side_effect=[0.0, 0.1, 2.0]):
            return collector.collect(seconds=1)

    def stored(self, collector):
        value = read_json(collector.disk.root / 'stores/sofmap.json', None)
        self.assertEqual(collector.state, value)
        return value

    def protect_offer(self, collector):
        offer = Offer('sofmap', 'synthetic-old', PRODUCTS[0], title='Saved product',
                      jan=JAN, condition='used', stock='in_stock', price_yen=12345,
                      observed_at=OLD, observed_run_id='older-run', verified=True).to_dict()
        collector.state['offers'] = {'saved-offer': offer}
        collector.state['journal'] = [{'observation_id': 'older-observation', 'run_id': 'older-run',
                                       'offer': deepcopy(offer)}]
        return deepcopy(collector.state['offers']), deepcopy(collector.state['journal'])

    def test_empty_completion_only_removes_exact_original_scope_and_checkpoints_proof(self):
        c = self.collector({search_url(): source(), parts_url(): fragment()})
        original = self.task()
        completed = c.enqueue(deepcopy(original))
        other = self.task(used=True, priority=3, attempts=9)
        remaining = c.enqueue(deepcopy(other))
        state = self.collect_one(c)
        self.assertEqual([completed], state['done'])
        self.assertEqual({remaining: other}, state['queue'])
        record = state['comparison_searches'][canonical(original['url'])]
        self.assertEqual(original, record['original_task'])
        self.assertEqual(OLD, record['created_at'])
        self.assertEqual('no_results', record['result'])
        self.assertEqual(JAN, record['query'])
        self.assertEqual(search_url(), record['deferred_source']['source_url'])
        self.assertEqual(JAN, record['deferred_source']['query']['requested'])
        self.assertEqual([search_url(), parts_url()], c.client.calls)
        self.assertEqual(1, len(state['comparison_searches']))
        self.assertFalse(state['cycle_complete'])
        self.stored(c)
        destination = self.root / 'checkpoint'
        result = checkpoint(destination, c.disk.root, RUN)
        self.assertEqual(['sofmap'], result['accepted_stores'])
        self.assertEqual(state, read_json(destination / 'stores/sofmap.json', None))

    def test_empty_completion_never_reprices_marks_out_of_stock_or_deletes_history(self):
        c = self.collector({search_url(): source(), parts_url(): fragment()})
        offers, journal = self.protect_offer(c)
        original = self.task()
        key = c.enqueue(deepcopy(original))
        self.collect_one(c)
        saved = self.stored(c)
        self.assertEqual([key], saved['done'])
        self.assertEqual(offers, saved['offers'])
        self.assertEqual(journal, saved['journal'])
        self.assertEqual(5, saved['comparison_searches'][canonical(original['url'])]['original_task']['attempts'])

    def test_used_completion_discovers_children_with_original_age_without_observations(self):
        for count in (1, 3):
            with self.subTest(count=count):
                c = self.collector({search_url(used=True): source(used=True),
                                    parts_url(used=True): fragment(used=True, count=count)},
                                   root=self.root / str(count))
                original = self.task(used=True)
                key = c.enqueue(deepcopy(original))
                self.collect_one(c)
                state = self.stored(c)
                self.assertEqual([key], state['done'])
                children = list(state['queue'].values())
                self.assertEqual(set(PRODUCTS[:count]), {t['url'] for t in children})
                for child in children:
                    self.assertEqual(('product', OLD, 1, 0),
                                     (child['type'], child['created_at'], child['priority'], child['attempts']))
                    self.assertEqual(parts_url(used=True), child['source'])
                    self.assertNotIn('price_yen', child)
                    self.assertNotIn('condition', child)
                    self.assertNotIn('observed_run_id', child)
                self.assertEqual({}, state['offers'])
                self.assertEqual([], state['journal'])
                proof = state['comparison_searches'][canonical(original['url'])]
                self.assertEqual(original, proof['original_task'])
                self.assertEqual('USED', proof['deferred_source']['anchor']['name'])
                self.assertEqual(JAN, proof['query'])
                self.assertEqual([search_url(used=True), parts_url(used=True)], c.client.calls)

    def test_existing_product_attempts_and_age_survive_used_rediscovery(self):
        c = self.collector({search_url(used=True): source(used=True),
                            parts_url(used=True): fragment(used=True, count=1)})
        older = '2026-09-01T00:00:00+00:00'
        existing = {'type': 'product', 'kind': 'comparison', 'url': PRODUCTS[0], 'created_at': older,
                    'priority': 1, 'attempts': 17, 'last_error': 'http_403',
                    'last_attempt_at': OLD, 'last_attempt_run_id': 'older-run'}
        key = c.enqueue(deepcopy(existing))
        c.process(self.task(used=True))
        c.process(self.task(used=True))
        c.save()
        self.assertEqual({key: existing}, self.stored(c)['queue'])
        self.assertEqual([search_url(used=True), parts_url(used=True)], c.client.calls)

    def test_failed_fragment_is_atomic_retained_and_increments_attempt_normally(self):
        for label, response in {'http403': FetchError('http_403'), 'http503': FetchError('http_503'),
                                'empty shell': fragment(used=True, body='<html></html>'),
                                'filter redirect': fragment(count=1),
                                'wrong group': fragment(used=True, count=1,
                                    body=fragment(used=True, count=1).text.replace('name="new_jan" value="' + JAN,
                                                                                 'name="new_jan" value="' + OTHER_JAN))}.items():
            with self.subTest(case=label):
                c = self.collector({search_url(used=True): source(used=True), parts_url(used=True): response},
                                   root=self.root / label)
                offers, journal = self.protect_offer(c)
                original = self.task(used=True)
                key = c.enqueue(deepcopy(original))
                before = deepcopy(c.state)
                if not isinstance(response, Exception):
                    with self.assertRaises(FetchError):
                        c.process(c.state['queue'][key])
                    self.assertEqual(before, c.state)
                self.collect_one(c)
                state = self.stored(c)
                retained = state['queue'][key]
                self.assertEqual(OLD, retained['created_at'])
                self.assertEqual(6, retained['attempts'])
                self.assertEqual(original['url'], retained['url'])
                self.assertEqual(RUN, retained['last_attempt_run_id'])
                self.assertEqual([], state['done'])
                self.assertEqual({}, state.get('comparison_searches', {}))
                self.assertEqual(offers, state['offers'])
                self.assertEqual(journal, state['journal'])
                self.assertEqual([search_url(used=True), parts_url(used=True)], c.client.calls)
                self.assertFalse(state['cycle_complete'])

    def test_source_redirect_cannot_complete_original_used_scope(self):
        c = self.collector({search_url(used=True): source(), parts_url(): fragment()})
        original = self.task(used=True)
        key = c.enqueue(deepcopy(original))
        self.collect_one(c)
        state = self.stored(c)
        self.assertEqual({key}, set(state['queue']))
        self.assertEqual(original['url'], state['queue'][key]['url'])
        self.assertEqual(6, state['queue'][key]['attempts'])
        self.assertEqual([], state['done'])
        self.assertEqual([search_url(used=True)], c.client.calls)
        self.assertEqual({}, state.get('comparison_searches', {}))

    def test_checkpoint_resume_keeps_attempt_history_then_completes_exact_task(self):
        c = self.collector({search_url(used=True): source(used=True),
                            parts_url(used=True): FetchError('http_403')})
        offers, journal = self.protect_offer(c)
        key = c.enqueue(self.task(used=True))
        self.collect_one(c)
        failed = deepcopy(self.stored(c)['queue'][key])
        destination = self.root / 'resumed'
        checkpoint(destination, c.disk.root, RUN)
        resumed = self.collector({search_url(used=True): source(used=True),
                                  parts_url(used=True): fragment(used=True, count=1)},
                                 root=destination, run='resumed-scope-fixture')
        self.assertEqual(failed, resumed.state['queue'][key])
        self.collect_one(resumed)
        state = self.stored(resumed)
        self.assertEqual([key], state['done'])
        record = state['comparison_searches'][canonical(failed['url'])]
        self.assertEqual(failed, record['original_task'])
        self.assertEqual(6, record['original_task']['attempts'])
        self.assertEqual(OLD, record['created_at'])
        self.assertEqual([OLD], [t['created_at'] for t in state['queue'].values()])
        self.assertEqual(offers, state['offers'])
        self.assertEqual(journal, state['journal'])

    def test_pagination_children_remain_separate_pending_work_with_original_age(self):
        next_url = parts_url(used=True) + '&page=2&order_by=DEFAULT'
        response = fragment(used=True, count=1)
        body = response.text.replace('<span>1</span>', '<span>2</span>')
        body += '<ol class="paging_bl"><li><a href="' + escape(next_url, quote=True) + '">2</a></li></ol>'
        c = self.collector({search_url(used=True): source(used=True),
                            parts_url(used=True): fragment(used=True, count=1, body=body),
                            canonical(next_url): fragment(used=True, count=1, url=next_url)})
        original = self.task(used=True)
        key = c.enqueue(deepcopy(original))
        self.collect_one(c)
        state = self.stored(c)
        dependency_id, dependency = next((k, t) for k, t in state['queue'].items() if t['type'] == 'list')
        self.assertEqual(canonical(next_url), dependency['url'])
        self.assertEqual((OLD, 1, 0, 2, JAN),
                         (dependency['created_at'], dependency['priority'], dependency['attempts'], dependency['depth'], dependency['query']))
        self.assertEqual([key], state['done'])
        self.assertNotIn(dependency_id, state['done'])
        before = deepcopy(dependency)
        with self.assertRaises(FetchError):
            c.process(dependency)
        self.assertEqual(before, state['queue'][dependency_id])
        self.assertFalse(state['cycle_complete'])


if __name__ == '__main__':
    unittest.main()

"""Offline excerpts from both retained Sofmap query-shell receipts.

Source: followup-acquisition-20261004/windows/capture-manifest.json bodies
f067eb1a90c05c042680e888d106a132ec4af9d5e55293a2b9d6eb77e950eab3 and
f65a04805a5483970ccbf4c6d8db42151113260e1ee8107ecb1a3bd1a8c17a34.
The excerpts omit navigation, sorting widgets and loader UI callbacks. These
tests require neither the local capture directory nor network/filesystem output.
"""
from copy import deepcopy
from dataclasses import asdict
from html import escape
import socket
import unittest
from unittest.mock import patch
from urllib.parse import quote

from sale_monitor.http import Page
from monitor_lab.deferred_listing import MAX_BODY, MAX_SCRIPT, MAX_TASKS, deferred_listing
from monitor_lab.queueing import task_age

JAN = '0195553309745'
OTHER_JAN = '4711289500124'
HOST = 'https://www.sofmap.com'
CFG = {'seed_urls': [HOST + '/contents/?id=2959&sid=1'],
       'product_patterns': [r'/product_detail.aspx\?']}
OLDER = '2026-10-02T00:30:00+09:00'
NEWER = '2026-10-01T16:00:00Z'
OBSERVED = '2026-10-03T22:33:43.802470+00:00'

SCRIPT = r'''
<!--
var strLoadingArea = "#search_result_area";
var isFirst = true;
$(document).ready(function(){
    var strProductType="ALL";
    jQuery("section#search_result_area ul.tab_list li a").each(function(){
        jQuery(this).click(function(e){
            e.preventDefault();
            var strTargetUrl = jQuery(this).attr("href");
            document.cookie = 'ptag='+jQuery(this).attr("name");
            if(strTargetUrl !='' && strTargetUrl != '#') {
                GetSearchParts(strTargetUrl,strLoadingArea);
                if(isFirst) {
                    $('#pgtop').click();
                    isFirst = false;
                }
            }
        });
        if(jQuery(this).attr("name") == strProductType) {
            jQuery(this).click();
        }
    });
});
function GetSearchParts(strUrl,strTargetFlame)
{
    if(moving) { return; } else { moving = true }
    var tmpStrUrl = strUrl.split('?');
    var temp_params = tmpStrUrl[1].replace(/^\?/, '').split('&');
    var urlParams = {};
    for (key in temp_params) {
        var item = temp_params[key].split('=');
        if (item[0] in urlParams) {
            urlParams[item[0]] = urlParams[item[0]] + ',' + item[1];
        }
        else {
            urlParams[item[0]] = item[1];
        }
    }
    if(!urlParams['is_page']) {
        strUrl += (strUrl.split('?')[1] ? '&':'?') + 'is_page=serch_result';
    }
    var retValue = true;
    jQuery.ajax({
        url: strUrl,
        timeout: 60000,
        dataType: "html",
        type:"GET",
        cache: false,
        contentType: "application/x-www-form-urlencoded; charset=Shift_JIS",
        data: { 'isFirst' : isFirst } ,
        beforeSend: function()
        {
            CreateLoadingFlame(strTargetFlame);
            if(strUrl && strUrl !='' && strUrl != '#') {
                document.cookie = 'rparam='+encodeURIComponent(document.location.search);
                document.cookie = 'pparam='+encodeURIComponent(strUrl);
                //GetSearchParts(strUrl,strLoadingArea);
            }
        },
        success: function(data, status) {
            jQuery("ul.product_list").remove();
            jQuery("div.list-interface-bar").after(data);
        },
        complete: function(XMLHttpRequest, status) { moving = false; }
    });
    return retValue;
}
//-->
'''


def shell(query=JAN, *, href=None, script=SCRIPT, contents='\n\t'):
    """Keep the observed title, search form, tab scope and empty list structure."""
    encoded = quote(query, safe='')
    href = href if href is not None else HOST + '//product_list_parts.aspx?keyword=' + encoded
    return f'''<!doctype html><html lang="ja"><head>
    <title>{escape(query)}の検索結果｜新品・中古・買取りのソフマップ[sofmap]</title>
    </head><body><form action="/search_result.aspx" method="get">
    <input id="searchText" name="keyword" value="{escape(query, quote=True)}"></form>
    <section id="search_result_area">
    <ul class="tab_list col3">
    <li><a name="ALL" class="current" href="{escape(href, quote=True)}">全ての商品<span>( - 点)</span></a></li>
    <li><a name="NEW" href="{HOST}//product_list_parts.aspx?product_type=NEW&amp;keyword={encoded}">新品商品</a></li>
    <li><a name="USED" href="{HOST}//product_list_parts.aspx?product_type=USED&amp;keyword={encoded}">中古商品</a></li>
    </ul><div class="list-interface-bar"></div><section class="list_settings">
    <form action="/search_result.aspx" method="get" name="search">
    <input type="hidden" name="keyword" value="{escape(query, quote=True)}">
    <input type="hidden" name="product_type" value="ALL">
    </form></section><section class="paging_settings">
    <p class="pg_number_set"><span>1</span>件 (全7点)</p></section>
    <ul id="change_style_list" class="product_list">{contents}</ul></section>
    <script>{script}</script></body></html>'''


def page(query=JAN, *, body=None, url=None, status=200):
    return Page(url or HOST + '/search_result.aspx?keyword=' + quote(query, safe=''),
                (body if body is not None else shell(query)).encode('utf-8'), OBSERVED,
                content_type='text/html; charset=utf-8', status=status)


def task(query=JAN, **extra):
    return {'lab_store': 'sofmap', 'url': page(query).url, 'type': 'list', 'lab_kind': 'list',
            'kind': 'comparison', 'query': query, 'sale_page': False,
            'created_at': NEWER, 'lab_dependencies': ['candidate:1'], **extra}


class DeferredListingTest(unittest.TestCase):
    def extract(self, *, body=None, query=JAN, href=None, script=SCRIPT, tasks=None,
                cfg=None, url=None, status=200):
        response = page(query, body=body if body is not None else shell(query, href=href, script=script),
                        url=url, status=status)
        return deferred_listing([task(query)] if tasks is None else tasks, response, CFG if cfg is None else cfg)

    def test_both_retained_query_shell_excerpts_make_exact_fragment_dependencies(self):
        for query in (JAN, OTHER_JAN):
            with self.subTest(query=query):
                children, resolution = self.extract(query=query)
                self.assertEqual(1, len(children))
                child = children[0]
                expected = HOST + '//product_list_parts.aspx?keyword=' + query + '&is_page=serch_result&isFirst=true'
                self.assertEqual(expected, child['url'])
                self.assertEqual(expected, child['lab_resource_url'])
                self.assertEqual(query, child['query'])
                self.assertEqual(('list', 'list', 'comparison', False, 'pending'),
                                 tuple(child[k] for k in ('lab_kind', 'type', 'kind', 'sale_page', 'lab_status')))
                self.assertEqual(page(query).url, child['source'])
                proof = resolution['deferred_listing_evidence']
                self.assertTrue(resolution['deferred'])
                self.assertEqual('ALL', proof['anchor']['name'])
                self.assertEqual(query, proof['query']['requested'])
                self.assertEqual(query, proof['query']['form_keyword'])
                self.assertEqual('GET', proof['loader']['method'])
                self.assertEqual('html', proof['loader']['data_type'])
                self.assertEqual({'is_page': 'serch_result'}, proof['loader']['append_if_absent'])
                self.assertEqual({'isFirst': True}, proof['loader']['get_data'])
                self.assertEqual('var isFirst = true;', proof['loader']['initial_declaration'])
                self.assertEqual(['_'], proof['loader']['omitted_volatile_query'])
                self.assertEqual('static_source_only', proof['loader']['inspection'])
                self.assertEqual(64, len(proof['loader']['script_sha256']))
                self.assertEqual(OBSERVED, proof['observed_at'])
                for unsupported_claim in ('confirmed_empty', 'price_yen', 'verified', 'products', 'result_count'):
                    self.assertNotIn(unsupported_claim, resolution)
                    self.assertNotIn(unsupported_claim, child)

    def test_fresh_child_preserves_oldest_instant_dependencies_and_all_inputs(self):
        tasks = [task(attempts=9, lab_attempts=[{'error': 'old'}], last_error='timeout',
                      lab_status='evidence_wait', lab_child_keys=['old-child'], lab_selected=True),
                 task(created_at='2026-10-03T00:00:00Z', lab_origin_created_at=OLDER,
                      lab_dependencies=['candidate:2', 'candidate:1'])]
        response, cfg = page(), deepcopy(CFG)
        before = deepcopy((tasks, asdict(response), cfg))
        children, resolution = deferred_listing(tasks, response, cfg)
        self.assertEqual(before, (tasks, asdict(response), cfg))
        child = children[0]
        self.assertEqual(OLDER, child['created_at'])
        self.assertEqual(OLDER, task_age(child))
        self.assertEqual(['candidate:1', 'candidate:2'], child['lab_dependencies'])
        self.assertEqual([], child['lab_attempts'])
        for key in ('attempts', 'last_error', 'lab_child_keys', 'lab_selected'):
            self.assertNotIn(key, child)
        self.assertEqual(2, resolution['shared_source_tasks'])
        child['lab_dependencies'].append('new')
        child['deferred_listing_evidence']['query']['requested'] = 'changed'
        self.assertEqual(JAN, resolution['deferred_listing_evidence']['query']['requested'])
        self.assertEqual(before, (tasks, asdict(response), cfg))

    def test_equal_and_missing_ages_keep_existing_queue_age_semantics(self):
        for dates, expected in ((('', ''), ''), (('', OLDER), OLDER),
                                ((OLDER, '2026-10-01T15:30:00Z'), '2026-10-01T15:30:00Z')):
            with self.subTest(dates=dates):
                tasks = [task(created_at=d) for d in dates]
                child = self.extract(tasks=tasks)[0][0]
                self.assertEqual(expected, child['created_at'])
                self.assertEqual(child, self.extract(tasks=list(reversed(tasks)))[0][0])
        with self.assertRaisesRegex(ValueError, 'age'):
            self.extract(tasks=[task(created_at='yesterday')])

    def test_is_page_is_only_appended_when_absent_and_single_slash_is_supported(self):
        for path in ('/product_list_parts.aspx', '//product_list_parts.aspx'):
            for suffix in ('', '&is_page=serch_result'):
                with self.subTest(path=path, suffix=suffix):
                    base = HOST + path + '?keyword=' + JAN
                    child = self.extract(href=base + suffix)[0][0]
                    self.assertEqual(base + '&is_page=serch_result&isFirst=true', child['url'])
        href = HOST + '//product_list_parts.aspx?is_page=serch_result&keyword=' + JAN
        self.assertEqual(href + '&isFirst=true', self.extract(href=href)[0][0]['url'])

    def test_encoded_query_is_compared_decoded_but_href_bytes_are_preserved(self):
        query = 'RTX 5070 & SSD'
        href = HOST + '//product_list_parts.aspx?keyword=RTX+5070+%26+SSD'
        child = self.extract(query=query, href=href)[0][0]
        self.assertEqual(href + '&is_page=serch_result&isFirst=true', child['url'])
        self.assertEqual(query, child['query'])

    def test_unrelated_pages_and_populated_results_are_left_to_the_normal_parser(self):
        for body in ('', '<html><title>Shop</title><a href="/product_detail.aspx?sku=1">SSD</a></html>',
                     '<html><!-- GetSearchParts product_list_parts.aspx --></html>',
                     shell(contents='<li><a href="/product_detail.aspx?sku=1">SSD</a></li>')):
            with self.subTest(body=body[:40]):
                self.assertIsNone(self.extract(body=body))

    def test_rejects_missing_wrong_or_ambiguous_shell_structure(self):
        original = shell()
        variants = [original.replace('id="search_result_area"', 'id="other"'),
                    original.replace('<section id="search_result_area">', '<div id="search_result_area">'),
                    original.replace('id="change_style_list"', 'id="other"'),
                    original.replace('class="product_list"', 'class="product_list_extra"'),
                    original.replace('<ul id="change_style_list"', '<ol id="change_style_list"'),
                    original.replace('</body>', '<section id="search_result_area"></section></body>'),
                    original.replace('</body>', '<ul id="change_style_list" class="product_list"></ul></body>')]
        for body in variants:
            with self.subTest(body=body[-130:]), self.assertRaises(ValueError):
                self.extract(body=body)

    def test_rejects_ambiguous_or_unselected_all_links(self):
        for fragment in ('<a name="ALL" href="#">duplicate</a>',
                         '<a name="ALL" class="current" href="#">duplicate</a>',
                         '<a name="NEW" class="current" href="#">other selected</a>'):
            with self.subTest(fragment=fragment), self.assertRaises(ValueError):
                self.extract(body=shell().replace('<ul class="tab_list col3">', '<ul class="tab_list col3">' + fragment))
        for old, new in (('name="ALL" class="current"', 'name="ALL"'),
                         ('name="ALL" class="current"', 'name="NEW" class="current"'),
                         ('class="tab_list col3"', 'class="tab_list_extra"')):
            with self.subTest(new=new), self.assertRaises(ValueError):
                self.extract(body=shell().replace(old, new))

    def test_rejects_duplicate_attributes_and_relative_url_base_overrides(self):
        for body in (shell().replace('name="ALL"', 'href="https://evil.example/" name="ALL"'),
                     shell().replace('name="ALL"', 'name="NEW" name="ALL"'),
                     shell().replace('</head>', '<base href="https://evil.example/"></head>')):
            with self.subTest(body=body[:100]), self.assertRaises(ValueError):
                self.extract(body=body)

    def test_rejects_fragment_path_tricks_origins_credentials_and_malformed_urls(self):
        suffix = '?keyword=' + JAN
        targets = [HOST + path + suffix for path in ('/other/product_list_parts.aspx',
                   '///product_list_parts.aspx', '/product_list_parts.aspx/extra', '/PRODUCT_LIST_PARTS.aspx',
                   '/product_list_parts.aspx.evil', '/%70roduct_list_parts.aspx', '/x/../product_list_parts.aspx')]
        targets += [base + '//product_list_parts.aspx' + suffix for base in (
                    'http://www.sofmap.com', 'https://evil.example', 'https://www.sofmap.com.evil.example',
                    'https://sofmap.com', 'https://www.sofmap.com:444', 'https://user@www.sofmap.com',
                    'https://user:pass@www.sofmap.com', 'https://www.sofmap.com.', 'https://www.sofmap.com:bad')]
        targets += ['//product_list_parts.aspx' + suffix, '/product_list_parts.aspx' + suffix,
                    HOST + '//product_list_parts.aspx' + suffix + '#fragment',
                    HOST + '\\product_list_parts.aspx' + suffix, ' javascript:alert(1)',
                    HOST + '//product_list_parts.aspx' + suffix + '\n']
        for href in targets:
            with self.subTest(href=href), self.assertRaises(ValueError):
                self.extract(href=href)

    def test_rejects_fragment_query_mismatches_duplicates_and_unproved_filters(self):
        for query in ('keyword=' + OTHER_JAN, 'keyword=', 'keyword=' + JAN + '&keyword=' + JAN,
                      'keyword=' + JAN + '&is_page=other', 'keyword=' + JAN + '&is_page=',
                      'keyword=' + JAN + '&is_page=serch_result&is_page=serch_result',
                      'keyword=' + JAN + '&%69s_page=serch_result',
                      'keyword=' + JAN + '&isFirst=false', 'keyword=' + JAN + '&isFirst=true',
                      'keyword=' + JAN + '&product_type=NEW', 'keyword=' + JAN + '&page=2',
                      'keyword=%ZZ', 'keyword=%FF', 'keyword=' + JAN + '&broken',
                      'keyword=' + JAN + '%00', 'keyword=' + JAN + '&x=1&y=2&z=3&w=4'):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.extract(href=HOST + '//product_list_parts.aspx?' + query)

    def test_rejects_source_url_and_config_origin_mismatches(self):
        urls = [page().url.replace('https:', 'http:'), page().url.replace('www.sofmap.com', 'evil.example'),
                page().url.replace('/search_result.aspx', '/other/search_result.aspx'),
                page().url + '&keyword=' + JAN, page().url.replace(JAN, OTHER_JAN)]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.extract(url=url, tasks=[task(url=url)])
        for cfg in ({}, {'seed_urls': []}, {'seed_urls': ['https://sofmap.com/']},
                    {'seed_urls': ['https://evil.example/']}, {'seed_urls': ['http://www.sofmap.com/']}):
            with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                self.extract(cfg=cfg)

    def test_rejects_task_scope_query_and_resource_mismatches(self):
        for extra in ({'query': OTHER_JAN}, {'query': ''}, {'lab_store': 'ark'}, {'kind': 'sale'},
                      {'sale_page': True}, {'sale_page': None}, {'lab_kind': 'product'},
                      {'url': page(OTHER_JAN).url}, {'lab_resource_url': page(OTHER_JAN).url}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.extract(tasks=[task(), task(**extra)])
        with self.assertRaises(ValueError):
            self.extract(tasks=[])

    def test_rejects_missing_conflicting_forms_and_title_substring_matches(self):
        replacements = [('value="' + JAN + '"', 'value="' + OTHER_JAN + '"'),
                        ('name="keyword"', 'name="other"'),
                        ('method="get" name="search"', 'method="post" name="search"'),
                        ('action="/search_result.aspx"', 'action="https://evil.example/search_result.aspx"'),
                        ('<title>' + JAN, '<title>X' + JAN),
                        ('<title>' + JAN, '<title>' + JAN + '0'),
                        ('<title>', '<h1>'), ('</title>', '</h1>')]
        for old, new in replacements:
            with self.subTest(new=new), self.assertRaises(ValueError):
                self.extract(body=shell().replace(old, new))
        duplicate = '<input name="keyword" value="' + JAN + '">'
        with self.assertRaises(ValueError):
            self.extract(body=shell().replace('<input type="hidden" name="product_type"', duplicate + '<input type="hidden" name="product_type"'))

    def test_rejects_failed_http_shells(self):
        for status in (301, 403, 404, 429, 500):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.extract(status=status)

    def test_malformed_title_is_rejected_before_recovery_can_hide_deferred_markers(self):
        malformed = shell().replace('</title>', '</h1>')
        with patch('monitor_lab.deferred_listing.html.fromstring', side_effect=AssertionError('Do not repair malformed title')):
            with self.assertRaisesRegex(ValueError, 'title boundaries'):
                self.extract(body=malformed)

    def test_rejects_changed_loader_method_url_type_parameter_and_request_options(self):
        replacements = [('type:"GET"', 'type:"POST"'), ('dataType: "html"', 'dataType: "json"'),
                        ('url: strUrl,', 'url: otherUrl,'), ('jQuery.ajax({', 'jQuery.ajax({ method:"POST",'),
                        ('type:"GET",', 'type:"GET", type:"POST",'),
                        ('is_page=serch_result', 'is_page=search_result'),
                        ("if(!urlParams['is_page'])", "if(urlParams['is_page'])"),
                        ('strUrl +=', 'strUrl ='), ('var retValue = true;', 'strUrl = other; var retValue = true;'),
                        ('beforeSend: function()', 'beforeSend: function(xhr)'),
                        ('CreateLoadingFlame(strTargetFlame);', 'strUrl = other;'),
                        ("data: { 'isFirst' : isFirst }", "data: { 'keyword' : 'other' }"),
                        ('return retValue;', 'jQuery.ajax({url:other}); return retValue;')]
        for old, new in replacements:
            with self.subTest(new=new), self.assertRaises(ValueError):
                self.extract(script=SCRIPT.replace(old, new))

    def test_rejects_changed_link_binding_and_target(self):
        for old, new in (('strProductType="ALL"', 'strProductType="NEW"'),
                         ('strLoadingArea = "#search_result_area"', 'strLoadingArea = "#elsewhere"'),
                         ('attr("href")', 'attr("other")'),
                         ('GetSearchParts(strTargetUrl,strLoadingArea);', 'GetSearchParts(other,strLoadingArea);'),
                         ('section#search_result_area ul.tab_list li a', 'section#search_result_area ul.tab_list li a.other')):
            with self.subTest(new=new), self.assertRaises(ValueError):
                self.extract(script=SCRIPT.replace(old, new))

    def test_initial_get_data_requires_one_true_declaration_and_only_proved_later_writes(self):
        variants = [SCRIPT.replace('var isFirst = true;', 'var isFirst = false;'),
                    SCRIPT.replace('var isFirst = true;', ''),
                    SCRIPT.replace('var isFirst = true;', 'var isFirst = true; var isFirst = true;'),
                    SCRIPT.replace('var isFirst = true;', 'var isFirst = true; isFirst = false;'),
                    SCRIPT.replace('var isFirst = true;', 'if(false) var isFirst = true;'),
                    SCRIPT.replace('isFirst = false;', 'isFirst = true;'),
                    SCRIPT.replace('GetSearchParts(strTargetUrl,strLoadingArea);',
                                   'isFirst = false; GetSearchParts(strTargetUrl,strLoadingArea);'),
                    SCRIPT.replace("data: { 'isFirst' : isFirst }", "data: { 'isFirst' : false }"),
                    SCRIPT + "window['isFirst'] = false;", SCRIPT + 'isFirst++;']
        for script in variants:
            with self.subTest(script=script[:110]), self.assertRaisesRegex(ValueError, 'isFirst|GET/html'):
                self.extract(script=script)
        with self.assertRaisesRegex(ValueError, 'isFirst'):
            self.extract(body=shell().replace('</body>', '<script>isFirst = false;</script></body>'))

    def test_rejects_missing_duplicate_commented_data_external_and_malformed_loaders(self):
        definition = SCRIPT[SCRIPT.index('function GetSearchParts'):SCRIPT.index('//-->')]
        variants = ['', SCRIPT + definition, '/*' + SCRIPT + '*/',
                    'var documentation = ' + repr(SCRIPT) + ';',
                    SCRIPT.replace(definition, 'function wrapper(){' + definition + '}'),
                    SCRIPT + 'GetSearchParts = other;', SCRIPT + '/* unterminated',
                    '`' + SCRIPT + '`', SCRIPT.replace('return retValue;', 'return retValue; }'),
                    SCRIPT.replace('    });\n    return retValue;', '    );\n    return retValue;')]
        for script in variants:
            with self.subTest(script=script[:70]), self.assertRaises(ValueError):
                self.extract(script=script)
        for tag in ('<script src="/loader.js">', '<script type="application/json">'):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                self.extract(body=shell().replace('<script>', tag))

    def test_comment_decoys_do_not_prove_a_missing_or_changed_contract(self):
        script = SCRIPT.replace('type:"GET"', 'type:"POST" /* type:"GET" */')
        with self.assertRaises(ValueError):
            self.extract(script=script)
        script = SCRIPT.replace('var strProductType="ALL";', '//var strProductType="ALL";')
        with self.assertRaises(ValueError):
            self.extract(script=script)
        self.assertIsNotNone(self.extract(script=SCRIPT.replace('url: strUrl,', 'url: /* proof is code */ strUrl,')))

    def test_work_and_output_are_bounded(self):
        with self.assertRaisesRegex(ValueError, 'body'):
            self.extract(body='x' * (MAX_BODY + 1))
        with self.assertRaisesRegex(ValueError, 'script'):
            self.extract(script=SCRIPT + ' ' * MAX_SCRIPT)
        with self.assertRaisesRegex(ValueError, 'bounded'):
            self.extract(tasks=[task()] * (MAX_TASKS + 1))
        with self.assertRaisesRegex(ValueError, 'dependencies'):
            self.extract(tasks=[task(lab_dependencies=['a'] * 1025)])
        with self.assertRaises(ValueError):
            self.extract(query='x' * 257)

    def test_extraction_does_not_fetch_read_files_write_files_or_mutate_inputs(self):
        response, tasks, cfg = page(), [task()], deepcopy(CFG)
        before = deepcopy((tasks, asdict(response), cfg))
        with patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')), \
             patch('builtins.open', side_effect=AssertionError('filesystem forbidden')):
            first = deferred_listing(tasks, response, cfg)
            second = deferred_listing(tasks, response, cfg)
        self.assertEqual(first, second)
        self.assertEqual(before, (tasks, asdict(response), cfg))


if __name__ == '__main__':
    unittest.main()

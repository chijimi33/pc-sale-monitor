from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sale_monitor.http import FetchError, Page
from sale_monitor.runner import Collector
from sale_monitor.sofmap_search import deferred_url, parse_listing, request_query
from sofmap_fixtures import CFG, HOST, JAN, OBSERVED, SCRIPT, page, shell

OLD = '2026-09-20T01:00:00+00:00'
CONFIG = {'stores': {'sofmap': {'adapter': 'html', **CFG}}, 'seller_aliases': {}}
PRODUCT = HOST + '/product_detail.aspx?sku=23812490'
FRAGMENT = HOST + '//product_list_parts.aspx?keyword=' + JAN + '&is_page=serch_result&isFirst=true'


def listing(*, query=JAN, body=None, url=FRAGMENT):
    card = f'<li><a class="product_name" href="{PRODUCT}">Motherboard A</a><span class="price">999</span></li>'
    text = (f'<section class="list_settings"><form><input name="keyword" value="{query}"></form></section>'
            '<p class="pg_number_set"><span>1</span></p>'
            f'<ul id="change_style_list" class="product_list">{card}</ul>')
    return Page(url, (body if body is not None else text).encode(), OBSERVED, content_type='text/html; charset=utf-8')


class FakeClient:
    def __init__(self, responses):
        self.responses, self.calls, self.count = responses, [], 0
        self.retry_after, self.transport_retry_after = {}, {}

    def get(self, url):
        self.calls.append(url)
        self.count += 1
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        return value

    def rendered(self, url):
        raise AssertionError('A rejected result must not cause browser fallback')


class SofmapSearchParsing(unittest.TestCase):
    def test_verified_shell_returns_exact_request_and_evidence(self):
        url, proof = deferred_url(page(), JAN, CFG)
        self.assertEqual(FRAGMENT, url)
        self.assertEqual(JAN, proof['query']['requested'])
        self.assertEqual(64, len(proof['content_hash']))

    def test_loader_mismatch_query_mismatch_and_malformed_title_are_not_empty(self):
        samples = [shell().replace('type:"GET"', 'type:"POST"'),
                   shell().replace('var isFirst = true;', 'var isFirst = false;'),
                   shell().replace('</title>', ''),
                   shell().replace('name="keyword" value="'+JAN, 'name="keyword" value="other'),
                   shell().replace('class="current"', 'class="other"'),
                   shell().replace(HOST+'//product_list_parts.aspx', 'https://other.example/product_list_parts.aspx')]
        for body in samples:
            with self.subTest(body=body), self.assertRaises(ValueError):
                deferred_url(page(body=body), JAN, CFG)
        with self.assertRaises(ValueError):
            deferred_url(page(), 'different', CFG)

    def test_only_bound_product_cards_are_discovery_not_prices(self):
        response = listing(body=listing().text + '<a class="product_name" href="'+HOST+'/product_detail.aspx?sku=999">Ad</a>')
        products, dependencies, proof = parse_listing(response, JAN)
        self.assertEqual([PRODUCT], [p['url'] for p in products])
        self.assertEqual({'url','title','source','kind'}, set(products[0]))
        self.assertEqual([], dependencies)
        self.assertEqual('results', proof['result'])

    def test_empty_html_error_redirect_wrong_query_and_partial_results_stay_pending(self):
        samples = [listing(body='<html>empty</html>'), listing(query='wrong'),
                   listing(url=FRAGMENT+'&product_type=USED'),
                   listing(url=FRAGMENT+'&page=2'),
                   listing(url=HOST+'/error/index.aspx'),
                   listing(body=listing().text.replace('<span>1</span>', '<span>2</span>')),
                   listing(body=listing().text.replace('sku=23812490', 'sku=23812490&sku=999')),
                   listing(body=listing().text.replace('<li>', '<li><a class="product_name" href="'+HOST+'/product_detail.aspx?sku=999">Other</a>')),
                   listing(body=listing().text.replace('id="change_style_list"', 'id="other"')),
                   listing(body=listing().text + '<ul id="change_style_list" class="product_list"></ul>')]
        response = listing(); response.status=403; samples.append(response)
        for response in samples:
            with self.subTest(response=response), self.assertRaises(FetchError):
                parse_listing(response, JAN)

    def test_pagination_and_group_filters_are_preserved(self):
        next_url = HOST+'/product_list_parts.aspx?keyword='+JAN+'&page=2&order_by=DEFAULT'
        grouped = HOST+'/search_result.aspx?keyword='+JAN+'&product_type=USED&new_jan='+JAN
        body = listing().text.replace('<span>1</span>', '<span>2</span>').replace('</li>', '<div class="used_box"><a href="'+grouped+'">Used variants</a></div></li>')
        body += '<ol class="paging_bl"><li><a href="'+next_url+'">2</a></li></ol>'
        _, dependencies, proof = parse_listing(listing(body=body), JAN)
        self.assertEqual(2, len(dependencies))
        self.assertEqual(1, len(proof['pagination']))
        self.assertTrue(any('product_type=USED' in x['url'] for x in dependencies))
        self.assertTrue(any('order_by=DEFAULT' in x['url'] for x in dependencies))
        for row in dependencies:
            self.assertEqual(JAN, row['query'])

    def test_request_duplicates_filters_and_foreign_origins(self):
        self.assertEqual(JAN, request_query(FRAGMENT))
        for url in [FRAGMENT+'&keyword=other', FRAGMENT.replace(HOST, 'https://other.example'), FRAGMENT+'#x']:
            with self.subTest(url=url), self.assertRaises(FetchError):
                request_query(url)


class SofmapSearchCollection(unittest.TestCase):
    def collector(self, root, responses, run='test-run'):
        cfg = deepcopy(CONFIG)
        c = Collector(Path(root), 'sofmap', cfg, run, FakeClient(responses))
        c.state['list_pages'], c.state['listed_candidates'] = 0, 0
        c.state['cycle_complete'] = False
        c.new_run = False
        return c

    def task(self, **changes):
        return {'type':'list', 'kind':'comparison', 'url':page().url,
                'sale_page':False, 'created_at':OLD, 'attempts':25, 'priority':0,
                'last_error':'old_error', **changes}

    def test_shell_and_fragment_produce_product_work_with_old_age_and_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self.collector(tmp, {page().url:page(), FRAGMENT:listing()})
            before = self.task()
            c.process(before)
            self.assertEqual([page().url, FRAGMENT], c.client.calls)
            tasks = list(c.state['queue'].values())
            self.assertEqual(1, len(tasks))
            self.assertEqual((PRODUCT, OLD, 0), (tasks[0]['url'],tasks[0]['created_at'],tasks[0]['priority']))
            record = next(iter(c.state['comparison_searches'].values()))
            self.assertEqual(before, record['original_task'])
            self.assertEqual({}, c.state['offers'])
            self.assertEqual([], c.state['journal'])

    def test_fragment_failure_preserves_original_then_resume_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self.collector(tmp, {page().url:page(), FRAGMENT:FetchError('http_403')})
            original = self.task()
            c.enqueue(original)
            key = next(iter(c.state['queue']))
            state = c.collect(seconds=.05)
            self.assertEqual(OLD, state['queue'][key]['created_at'])
            self.assertEqual(26, state['queue'][key]['attempts'])
            self.assertEqual('http_403', state['queue'][key]['last_error'])
            self.assertFalse(state['cycle_complete'])
            self.assertEqual({}, state.get('comparison_searches',{}))
            self.assertEqual([], state['journal'])
            resumed = self.collector(tmp, {page().url:page(), FRAGMENT:listing()}, 'resumed')
            resumed.process(resumed.state['queue'][key])
            proof = next(iter(resumed.state['comparison_searches'].values()))
            self.assertEqual(26, proof['original_task']['attempts'])
            products = [t for t in resumed.state['queue'].values() if t['type']=='product']
            self.assertEqual([OLD], [t['created_at'] for t in products])

    def test_failed_parse_is_atomic_and_old_offer_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = listing(body=listing().text.replace('<span>1</span>', '<span>2</span>'))
            c = self.collector(tmp, {page().url:page(), FRAGMENT:bad})
            c.enqueue(self.task())
            before = deepcopy(c.state)
            with self.assertRaises(FetchError):
                c.process(next(iter(c.state['queue'].values())))
            self.assertEqual(before, c.state)

    def test_redirect_cannot_drop_original_filter_or_page_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            filtered = page().url+'&product_type=USED&page=2'
            c = self.collector(tmp, {filtered:page(), FRAGMENT:listing()})
            c.enqueue(self.task(url=filtered))
            key = next(iter(c.state['queue']))
            state = c.collect(seconds=.05)
            self.assertEqual(filtered,state['queue'][key]['url'])
            self.assertEqual(OLD,state['queue'][key]['created_at'])
            self.assertEqual(26,state['queue'][key]['attempts'])
            self.assertEqual([],state['done'])
            self.assertEqual([filtered],c.client.calls)
            self.assertEqual({},state.get('comparison_searches',{}))

    def test_fragment_redirect_cannot_drop_loader_parameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            redirected = listing(url=HOST+'/search_result.aspx?keyword='+JAN)
            c = self.collector(tmp,{page().url:page(),FRAGMENT:redirected})
            with self.assertRaises(FetchError):
                c.process(self.task())
            self.assertEqual({},c.state['queue'])
            self.assertEqual({},c.state.get('comparison_searches',{}))

    def test_repeated_discovery_deduplicates_product_and_keeps_existing_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self.collector(tmp, {page().url:page(), FRAGMENT:listing()})
            c.enqueue({'type':'product','kind':'comparison','url':PRODUCT, 'created_at':OLD,
                       'attempts':10,'last_error':'http_403'})
            c.process(self.task()); c.process(self.task())
            self.assertEqual(1, len(c.state['queue']))
            self.assertEqual(10, next(iter(c.state['queue'].values()))['attempts'])
            self.assertEqual([page().url,FRAGMENT], c.client.calls)


if __name__ == '__main__':
    unittest.main()

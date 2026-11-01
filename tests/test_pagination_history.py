from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sale_monitor.http import Page
from sale_monitor.retrying import runnable
from sale_monitor.runner import Collector


AGE = '2026-10-20T00:00:00+00:00'
NOW = '2026-11-01T00:00:00+00:00'
URL = 'https://www.ark-pc.co.jp/t/c/577/?offset=20'
NEXT = 'https://www.ark-pc.co.jp/t/c/577/?offset=40'
CFG = {'stores': {'ark': {'adapter': 'html', 'seed_urls': [],
    'product_patterns': [r'/i/\d+/'], 'sale_patterns': []},
    'yahoo': {'adapter': 'yahoo', 'seed_urls': [], 'queries': []}}}
HISTORY = {'attempts': 7, 'last_error': 'http_503', 'last_attempt_at': AGE,
           'last_attempt_run_id': 'previous', 'retry_at_epoch_seconds': 100}


class Fake:
    def __init__(self):
        self.count = 0
        self.retry_after = {}

    def get(self, url):
        self.count += 1
        return Page(url, ('<a href="/i/123/">SSD</a>'
            '<a rel="next" href="' + NEXT + '">Next</a>').encode(), NOW)


class PaginationHistory(unittest.TestCase):
    def collector(self, root, store='ark', run='current'):
        c = Collector(root, store, CFG, run, Fake())
        c.seed()
        c.state['queue'].clear()
        return c

    def parent(self, store='ark', **extra):
        if store == 'yahoo':
            task = {'type': 'yahoo', 'query': 'SSD', 'start': 1,
                    'kind': 'comparison', 'requested': True,
                    'comparison_queries': ['SSD']}
        else:
            task = {'type': 'list', 'url': URL, 'sale_page': True, 'depth': 2,
                    'discovery_origin': {'url': URL, 'created_at': AGE}}
        return {**task, 'created_at': AGE, 'priority': 1, **HISTORY, **extra}

    def process(self, c, parent):
        with patch('sale_monitor.runner.adapters.yahoo_page', return_value=([], 51, {})):
            c.process(parent)

    def child(self, c, store):
        return next((key, task) for key, task in c.state['queue'].items()
                    if (task.get('url') == NEXT if store == 'ark' else task.get('start') == 51))

    def test_new_pages_have_independent_attempts_and_survive_restart(self):
        for store in ('ark', 'yahoo'):
            with self.subTest(store=store), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                c = self.collector(root, store)
                parent_id = c.enqueue(self.parent(store))
                parent = c.state['queue'][parent_id]
                before = deepcopy(parent)
                self.process(c, parent)
                child_id, child = self.child(c, store)
                self.assertEqual(child['attempts'], 0)
                for key in HISTORY.keys() - {'attempts'}:
                    self.assertNotIn(key, child)
                for key in ('created_at', 'priority', 'kind', 'query', 'requested',
                            'comparison_queries', 'sale_page', 'depth', 'discovery_origin'):
                    if key in before:
                        self.assertEqual(child[key], before[key])
                self.assertEqual(parent, before)
                self.assertEqual(c.state['offers'], {})
                c.save()
                restarted = Collector(root, store, CFG, 'next-run', Fake())
                self.assertEqual(restarted.state['queue'][child_id], child)
                self.assertEqual(restarted.state['queue'][parent_id], before)
                self.assertNotIn(child_id, restarted.state['done'])
                self.assertNotIn(child_id, restarted.attempted)

    def test_existing_pages_keep_their_own_attempt_history(self):
        for store in ('ark', 'yahoo'):
            with self.subTest(store=store), tempfile.TemporaryDirectory() as directory:
                c = self.collector(Path(directory), store)
                parent = self.parent(store)
                child = {**parent, **({'url': NEXT} if store == 'ark' else {'start': 51}),
                         'attempts': 11, 'last_error': 'http_429',
                         'last_attempt_run_id': 'child-run', 'retry_at_epoch_seconds': 999,
                         'created_at': '2026-10-19T00:00:00+00:00'}
                child_id = c.enqueue(child)
                before = deepcopy(c.state['queue'][child_id])
                self.process(c, parent)
                self.assertEqual(c.state['queue'][child_id], before)

    def test_new_page_still_obeys_shared_host_backoff(self):
        for store, host in (('ark', 'www.ark-pc.co.jp'), ('yahoo', 'shopping.yahooapis.jp')):
            with self.subTest(store=store), tempfile.TemporaryDirectory() as directory:
                c = self.collector(Path(directory), store)
                self.process(c, self.parent(store))
                key, child = self.child(c, store)
                c.client.retry_after[host] = 250
                ready, waits = runnable({key: child}, set(), c.cfg, c.client, {}, 200)
                self.assertEqual(ready, [])
                self.assertEqual(waits[0]['affected_tasks'], 1)
                ready, waits = runnable({key: child}, set(), c.cfg, c.client, {}, 251)
                self.assertEqual(ready, [(key, child)])
                self.assertEqual(waits, [])

    def test_completed_page_is_not_requeued_and_parent_is_unchanged(self):
        for store in ('ark', 'yahoo'):
            with self.subTest(store=store), tempfile.TemporaryDirectory() as directory:
                c = self.collector(Path(directory), store)
                parent = self.parent(store)
                before = deepcopy(parent)
                self.process(c, parent)
                key, _ = self.child(c, store)
                del c.state['queue'][key]
                c.state['done'].append(key)
                self.process(c, parent)
                self.assertNotIn(key, c.state['queue'])
                self.assertEqual(parent, before)

    def test_comparison_page_preserves_query_filter_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            c = self.collector(Path(directory))
            parent = self.parent(kind='comparison', sale_page=False,
                                 requested=True, comparison_queries=['SSD'],
                                 discovery_origin=None, search_query='SSD')
            self.process(c, parent)
            _, child = self.child(c, 'ark')
            self.assertEqual(child['attempts'], 0)
            for key in ('kind', 'sale_page', 'requested', 'comparison_queries',
                        'search_query', 'priority', 'created_at'):
                self.assertEqual(child[key], parent[key])
            self.assertEqual(child['url'], NEXT)

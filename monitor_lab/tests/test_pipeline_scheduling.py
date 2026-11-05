from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from monitor_lab.acquire import Coordinator
from monitor_lab.pipeline import run
from monitor_lab.safety import allowed_root, digest, read, write
from monitor_lab.stores import BACKENDS, SQLite


class Clock:
    def __init__(self, now=1791000000.0):
        self.now = now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def make_inputs(root, *, duplicate=False):
    """Small, explicitly synthetic pinned input; no external requests."""
    pages = []
    queues = {store: {} for store in ('koubou', 'tsukumo', 'ark')}
    for group, jan in (('b550', '0195553309745'), ('capture', '4711289500124')):
        for store, url in (
            ('koubou', 'https://www.pc-koubou.jp/products/detail.php?product_id=' + ('1051336' if group == 'b550' else '931040')),
            ('tsukumo', 'https://shop.tsukumo.co.jp/goods/' + jan + '/'),
            ('ark', 'https://www.ark-pc.co.jp/i/' + ('12201487' if group == 'b550' else '20300386') + '/'),
        ):
            product = {'@context': 'https://schema.org', '@type': 'Product', 'name': 'Synthetic ' + group,
                       'gtin13': jan, 'offers': {'price': 10000, 'priceCurrency': 'JPY', 'availability': 'https://schema.org/InStock'}}
            body = ('<html><h1>Synthetic ' + group + '</h1><script type="application/ld+json">' + json.dumps(product) + '</script></html>').encode()
            name = store + '-' + group
            file = root / 'pages' / (name + '.html')
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(body)
            pages.append({'name': name, 'store': store, 'group': group, 'url': url, 'path': 'pages/' + name + '.html',
                          'body_sha256': hashlib.sha256(body).hexdigest(), 'status': 200,
                          'observed_at': '2026-10-01T20:00:00+00:00'})
    if duplicate:
        for key in ('one', 'two'):
            queues['koubou'][key] = {'url': pages[0]['url'], 'created_at': '2020-01-01T00:00:00+00:00',
                                     'attempts': 7, 'type': 'product', 'lab_selected': True, 'lab_kind': 'product',
                                     'lab_role': 'comparison', 'lab_group': 'b550', 'lab_dependencies': ['prior-candidate']}
    files = []
    documents = {'state/stores/' + store + '.json': {'queue': queue, 'offers': {}} for store, queue in queues.items()}
    documents['public/latest.json'] = {'generated_at': '2026-10-02T00:00:00+00:00'}
    for name, value in documents.items():
        file = root / 'transport_failure' / name
        write(file, value)
        body = file.read_bytes()
        files.append({'path': 'transport_failure/' + name, 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()})
    manifest = {'schema': 1, 'source_base': 'synthetic-fixture', 'pages': pages,
                'snapshots': {'transport_failure': {'data_sha': 'synthetic-fixture', 'files': files}}}
    manifest['input_hash'] = digest(manifest)
    write(root / 'manifest.json', manifest)
    return pages


class PipelineSchedulingTest(unittest.TestCase):
    def setUp(self):
        self.root = allowed_root() / 'tests' / ('pipeline-scheduling-' + uuid.uuid4().hex)
        self.inputs = self.root / 'inputs'

    def test_a_resume_does_not_turn_next_cycle_dependencies_into_same_cycle_work(self):
        make_inputs(self.inputs)
        output = self.root / 'experiment'
        first = run(self.inputs, 'transport_failure', 'A', 'replay', output, backend='sqlite')
        with SQLite(output / 'store') as store:
            before = store.snapshot()
        second = run(self.inputs, 'transport_failure', 'A', 'replay', output, backend='sqlite')
        self.assertEqual(2, first['observations'])
        self.assertEqual(0, second['replayed_pages'])
        with SQLite(output / 'store') as store:
            self.assertEqual(before, store.snapshot())

    def test_duplicate_product_tasks_share_one_acquisition_and_keep_original_history(self):
        pages = make_inputs(self.inputs, duplicate=True)
        output = self.root / 'experiment'
        run(self.inputs, 'transport_failure', 'B', 'replay', output, backend='sqlite')
        receipts = read(output / 'receipts.json')
        self.assertEqual(1, Counter(row['url'] for row in receipts)[pages[0]['url']])
        with SQLite(output / 'store') as store:
            tasks = store.snapshot()['records']['tasks']
        for key in ('koubou:one', 'koubou:two'):
            self.assertEqual('complete', tasks[key]['lab_status'])
            self.assertEqual(7, tasks[key]['attempts'])
            self.assertEqual('2020-01-01T00:00:00+00:00', tasks[key]['created_at'])

    def test_completed_resume_keeps_one_publication_and_state_on_every_backend(self):
        make_inputs(self.inputs)
        for backend, cls in BACKENDS.items():
            output = self.root / backend
            first = run(self.inputs, 'transport_failure', 'B', 'replay', output, backend=backend)
            pointer = read(output / 'publication/current.json')
            with cls(output / 'store') as store:
                before = store.snapshot()
            second = run(self.inputs, 'transport_failure', 'B', 'replay', output, backend=backend)
            with cls(output / 'store') as store:
                self.assertEqual(before, store.snapshot(), backend)
            self.assertEqual(pointer, read(output / 'publication/current.json'))
            self.assertEqual(6, first['observations'])
            self.assertEqual(0, second['replayed_pages'])

    def test_restart_after_deadline_does_not_receive_a_new_budget(self):
        pages = make_inputs(self.inputs)
        clock, calls = Clock(), []
        bodies = {row['url']: (self.inputs / row['path']).read_bytes() for row in pages}

        class FakeTransport:
            method = 'synthetic'

            def get(self, url, timeout):
                calls.append(url)
                clock.sleep(1)
                return 200, {}, bodies[url]

            def close(self):
                pass

        def coordinator(transport, **kwargs):
            return Coordinator(transport, **kwargs, clock=clock.time, monotonic=clock.time, sleep=clock.sleep)

        class PlannedStop(BaseException):
            pass

        normal_commit = SQLite.commit

        def stop_after_first_task(store, txid, changes, *args, **kwargs):
            result = normal_commit(store, txid, changes, *args, **kwargs)
            if txid.startswith('acquire:'):
                raise PlannedStop()
            return result

        output = self.root / 'experiment'
        with patch('monitor_lab.pipeline.time', SimpleNamespace(monotonic=clock.time, perf_counter=clock.time)), \
                patch('monitor_lab.pipeline.Coordinator', coordinator), \
                patch.dict('monitor_lab.pipeline.TRANSPORTS', {'urllib': FakeTransport}):
            with patch.object(SQLite, 'commit', stop_after_first_task), self.assertRaises(PlannedStop):
                run(self.inputs, 'transport_failure', 'B', 'live', output, backend='sqlite', budget=10)
            self.assertEqual(1, len(calls))
            clock.sleep(20)
            calls.clear()
            result = run(self.inputs, 'transport_failure', 'B', 'live', output, backend='sqlite', budget=10)
            self.assertEqual([], calls)
            self.assertEqual(0, result['http_navigation_attempts'])

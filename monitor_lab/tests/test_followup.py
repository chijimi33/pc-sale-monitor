"""Offline follow-up intent checks using small real capture/queue fixtures."""
from copy import deepcopy
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from sale_monitor.models import STORES
from monitor_lab.capture import verify_capture
from monitor_lab.followup import load_followup, prepare_followup
from monitor_lab.pipeline import run
from monitor_lab.safety import allowed_root, atomic_bytes, digest, read, write
from monitor_lab.study import study
from monitor_lab.tests.test_capture_pipeline import BODY
from monitor_lab.tests.test_pipeline_scheduling import make_inputs
from monitor_lab.tests.test_request_plan_capture import (
    bundle_hashes, copy_published_capture, fake_study, make_plan, no_network, rehash_manifest,
)

HOME = 'https://www.sofmap.com/contents/?id=2959&sid=1'
PRODUCT = 'https://www.ark-pc.co.jp/i/12201487/'
OUTSIDE = 'https://www.sofmap.com/product_detail.aspx?sku=999'
JAN = '0195553309745'
SEARCH = 'https://www.sofmap.com/contents/?keyword=' + JAN
CREATED = '2026-10-04T12:34:56+09:00'
BACKLOG = 'a' * 40


class FollowupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = allowed_root() / 'tests' / ('followup-' + uuid.uuid4().hex)
        cls.inputs, cls.capture, cls.experiment = [cls.root / name for name in ('inputs', 'capture', 'experiment')]
        make_inputs(cls.inputs)
        cls.original = {'type': 'product', 'url': OUTSIDE, 'created_at': '2000-01-01T00:00:00Z', 'attempts': 9}
        path = cls.inputs / 'transport_failure/state/stores/sofmap.json'
        write(path, {'queue': {'old-product': cls.original}, 'offers': {}})
        manifest = read(cls.inputs / 'manifest.json')
        snapshot = manifest['snapshots']['transport_failure']
        snapshot['data_sha'] = BACKLOG
        snapshot['files'].append({'path': path.relative_to(cls.inputs).as_posix(), 'bytes': path.stat().st_size,
                                 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        manifest.pop('input_hash'); manifest['input_hash'] = digest(manifest)
        write(cls.inputs / 'manifest.json', manifest)
        cls.plan = make_plan([{'store': 'sofmap', 'url': HOME, 'kind': 'home'},
                              {'store': 'ark', 'url': PRODUCT, 'kind': 'product'}])
        write(cls.root / 'plan.json', cls.plan)
        home = (f'<html><a href="{SEARCH}">SALE SSD</a>'
                f'<li>SALE SSD<a href="{OUTSIDE}">SALE motherboard</a></li>'
                '<form action="/contents/" method="get"><input name="keyword"></form></html>').encode()
        with no_network(), fake_study({'urllib': {HOME: (200, {}, home), PRODUCT: (200, {}, BODY)}}):
            study(cls.capture, methods=['urllib'], plan=cls.root / 'plan.json', budget=120)
            run(cls.inputs, 'transport_failure', 'B', 'replay', cls.experiment,
                capture_input=cls.capture, candidate_urls=[PRODUCT], budget=120)
        cls.state = read(cls.experiment / 'state-export.json')
        cls.meta = read(cls.experiment / 'experiment.json')
        cls.verified = verify_capture(cls.capture)
        cls.config = cls.verified['settings']['stores']
        tasks = cls.state['records']['tasks']
        cls.search_id = next(key for key, task in tasks.items()
                             if task.get('url') == SEARCH and task.get('query') == JAN and task['lab_kind'] == 'list')
        cls.campaign_id = next(key for key, task in tasks.items()
                               if task.get('url') == SEARCH and task.get('kind') == 'sale')
        cls.product_id = 'sofmap:old-product'
        cls.source_hashes = [bundle_hashes(p) for p in (cls.inputs, cls.capture, cls.experiment)]

    def setUp(self):
        self.enterContext(no_network())
        self.case = self.root / uuid.uuid4().hex

    def copy_experiment(self):
        destination = self.case / 'experiment'
        for name in ('experiment.json', 'state-export.json'):
            atomic_bytes(destination / name, (self.experiment / name).read_bytes())
        return destination

    def prepare(self, *, experiment=None, capture=None, ids=None, output=None):
        return prepare_followup(experiment or self.experiment, capture or self.capture,
                                output or self.case / 'intent', [self.search_id] if ids is None else ids,
                                created_at=CREATED)

    def test_late_form_lineage_and_original_product_history_are_retained_without_mutation(self):
        bundle = self.prepare(ids=[self.search_id, self.product_id])
        self.assertEqual({'format', 'created_at', 'source', 'request_plan', 'tasks', 'inherited_host_gates', 'bundle_hash'}, set(bundle))
        self.assertEqual({'experiment_id', 'experiment_path', 'capture_path', 'experiment_sha256',
                          'state_export_sha256', 'capture_manifest_sha256', 'backlog_data_sha', 'capture_source_method'}, set(bundle['source']))
        self.assertEqual('pc-sale-monitor-followup-v1', bundle['format'])
        self.assertEqual(CREATED, bundle['created_at'])
        self.assertEqual(BACKLOG, bundle['source']['backlog_data_sha'])
        self.assertEqual(str(self.experiment.resolve()), bundle['source']['experiment_path'])
        self.assertEqual(str(self.capture.resolve()), bundle['source']['capture_path'])
        self.assertEqual(self.plan['source_data_sha'], bundle['request_plan']['source_data_sha'])
        self.assertEqual(CREATED, bundle['request_plan']['created_at'])
        self.assertEqual([{'store': 'sofmap', 'url': SEARCH, 'kind': 'list'},
                          {'store': 'sofmap', 'url': OUTSIDE, 'kind': 'product'}], bundle['request_plan']['resources'])
        self.assertEqual(set(STORES) - {'sofmap'}, {r['store'] for r in bundle['request_plan']['not_requested']})
        self.assertEqual(self.verified['hosts'], bundle['inherited_host_gates'])
        for row in bundle['tasks']:
            original = self.state['records']['tasks'][row['task_id']]
            self.assertEqual(original, row['task'])
            self.assertEqual({key: self.state['records']['tasks'][key] for key in original['lab_parent_keys']}, row['parents'])
            self.assertEqual('evidence_wait', row['task']['lab_status'])
            for proof in row['source_records']:
                self.assertEqual(self.verified['receipts'][proof['dispatch']['receipt']['source_receipt_index']], proof['receipt'])
                self.assertTrue(proof['dispatch_id'].startswith(self.meta['experiment_id'] + ':'))
        late, product = bundle['tasks']
        self.assertTrue(late['source_records'][0]['dispatch']['receipt']['analysis_reuse'])
        self.assertFalse(late['source_records'][0]['original_dispatch']['receipt']['analysis_reuse'])
        self.assertLess(late['source_records'][0]['original_dispatch']['slot'], late['source_records'][0]['dispatch']['slot'])
        self.assertEqual(self.original, {k: product['task'][k] for k in self.original})
        self.assertEqual(bundle, load_followup(self.case / 'intent', self.config))
        self.assertEqual(['followup.json'], [p.name for p in (self.case / 'intent').iterdir()])
        self.assertEqual(self.source_hashes, [bundle_hashes(p) for p in (self.inputs, self.capture, self.experiment)])

    def test_same_resource_coalesces_but_keeps_explicit_task_ids_and_scopes(self):
        bundle = self.prepare(ids=[self.search_id, self.campaign_id])
        self.assertEqual([self.search_id, self.campaign_id], [r['task_id'] for r in bundle['tasks']])
        self.assertEqual(1, len(bundle['request_plan']['resources']))
        self.assertEqual({'comparison', 'sale'}, {r['task']['kind'] for r in bundle['tasks']})

    def test_explicit_selection_limits_and_pending_discovery_requirement(self):
        parent = self.state['records']['tasks'][self.search_id]['lab_parent_keys'][0]
        for ids in (None, [], self.search_id, [self.search_id] * 2, ['absent'], [parent], list(map(str, range(21)))):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                prepare_followup(self.experiment, self.capture, self.case / 'rejected', ids, created_at=CREATED)
            self.assertFalse((self.case / 'rejected').exists())

    def test_edited_task_url_scope_and_discovery_records_fail_before_output(self):
        mutations = {
            'url': lambda t: t.update(url=SEARCH + '1', lab_resource_url=SEARCH + '1'),
            'resource_url': lambda t: t.update(lab_resource_url=SEARCH + '1'),
            'kind': lambda t: t.update(lab_kind='product'),
            'query': lambda t: t.update(query='4711289500124'),
            'completed': lambda t: t.update(lab_status='complete'),
            'selected': lambda t: t.update(lab_selected=True),
            'parents': lambda t: t.update(lab_parent_keys=[]),
            'dependencies': lambda t: t.update(lab_dependencies=[]),
            'record_hash': lambda t: t['lab_discovery_evidence'][0].update(body_sha256='f' * 64),
            'record_time': lambda t: t['lab_discovery_evidence'][0].update(observed_at=CREATED),
            'record_method': lambda t: t['lab_discovery_evidence'][0].update(method='pooled_http11'),
            'record_index': lambda t: t['lab_discovery_evidence'][0].update(source_receipt_index=1),
            'record_capture': lambda t: t['lab_discovery_evidence'][0].update(source_capture_manifest_sha256='f' * 64),
            'record_metadata': lambda t: t['lab_discovery_evidence'][0]['discovered'].update(title='invented'),
            'duplicate': lambda t: t['lab_discovery_evidence'].append(deepcopy(t['lab_discovery_evidence'][0])),
        }
        experiment = self.copy_experiment()
        for label, mutate in mutations.items():
            state = deepcopy(self.state)
            mutate(state['records']['tasks'][self.search_id])
            write(experiment / 'state-export.json', state)
            output = self.case / label
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.prepare(experiment=experiment, output=output)
            self.assertFalse(output.exists())

    def test_changed_dispatch_receipt_and_uncommitted_or_cross_run_reference_rejected(self):
        experiment = self.copy_experiment()
        parent = self.state['records']['tasks'][self.search_id]['lab_parent_keys'][0]
        identity = next(key for key, d in self.state['records']['dispatches'].items() if parent in d['task_ids'])
        mutations = {
            'uncommitted': lambda d: d.update(state='reserved'),
            'processing': lambda d: d.update(processing_error='ValueError'),
            'environment': lambda d: d['receipt']['environment'].update(invented=True),
            'attempt': lambda d: d['receipt'].update(attempts=[{'sequence': 123}]),
            'foreign': lambda d: d['receipt'].update(derived_from_dispatch='lab-other:1'),
            'missing_original': lambda d: d['receipt'].update(derived_from_dispatch=self.meta['experiment_id'] + ':999'),
        }
        for label, mutate in mutations.items():
            state = deepcopy(self.state)
            mutate(state['records']['dispatches'][identity])
            write(experiment / 'state-export.json', state)
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.prepare(experiment=experiment, output=self.case / label)
            self.assertFalse((self.case / label).exists())

    def test_source_metadata_and_complete_capture_are_required(self):
        experiment = self.copy_experiment()
        for field, value in (('capture_manifest_sha256', 'f' * 64), ('capture_source_method', 'pooled_http11'),
                             ('capture_plan_hash', 'f' * 64)):
            meta = deepcopy(self.meta); meta['conditions'][field] = value
            write(experiment / 'experiment.json', meta)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.prepare(experiment=experiment, output=self.case / field)
            self.assertFalse((self.case / field).exists())
        capture = self.case / 'capture'
        copy_published_capture(self.capture, capture)
        manifest = read(capture / 'capture-manifest.json'); manifest['complete'] = False
        write(capture / 'capture-manifest.json', manifest)
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.prepare(capture=capture)
        self.assertFalse((self.case / 'intent').exists())

    def test_changed_body_and_rehashed_scope_are_rejected(self):
        capture = self.case / 'capture'
        copy_published_capture(self.capture, capture)
        file = capture / self.verified['receipts'][0]['body_file']
        atomic_bytes(file, b'invented page')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.prepare(capture=capture)
        atomic_bytes(file, (self.capture / self.verified['receipts'][0]['body_file']).read_bytes())
        metadata = read(capture / 'study.json')
        metadata['scope_plan']['resources'][0]['kind'] = 'product'
        scope = metadata['scope_plan']; scope['plan_hash'] = digest({k: v for k, v in scope.items() if k != 'plan_hash'})
        metadata['plan_hash'] = scope['plan_hash']
        write(capture / 'study.json', metadata)
        rehash_manifest(capture)
        with self.assertRaises(ValueError):
            self.prepare(capture=capture)
        self.assertFalse((self.case / 'intent').exists())

    def test_loading_rechecks_config_sources_and_rehashed_bundle_content(self):
        experiment = self.copy_experiment()
        original = self.prepare(experiment=experiment)
        directory = self.case / 'intent'
        config = deepcopy(self.config); config['sofmap']['seed_urls'] = ['https://example.org/']
        with self.assertRaisesRegex(ValueError, 'configuration'):
            load_followup(directory, config)
        for name in ('experiment.json', 'state-export.json'):
            path = experiment / name
            body = path.read_bytes()
            atomic_bytes(path, body + b'\n')
            with self.subTest(source=name), self.assertRaisesRegex(ValueError, 'changed'):
                load_followup(directory, self.config)
            atomic_bytes(path, body)
        for field in ('request_plan', 'tasks', 'inherited_host_gates'):
            value = deepcopy(original)
            if field == 'request_plan':
                value[field]['resources'][0]['url'] += '1'
                value[field]['plan_hash'] = digest({k: v for k, v in value[field].items() if k != 'plan_hash'})
            elif field == 'tasks':
                value[field][0]['task']['attempts'] = 0
            else:
                value[field]['www.sofmap.com'] = {'blocked': True, 'reason': 'invented'}
            value['bundle_hash'] = digest({k: v for k, v in value.items() if k != 'bundle_hash'})
            write(directory / 'followup.json', value)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'changed'):
                load_followup(directory, self.config)

    def test_new_local_host_holds_cannot_be_dropped(self):
        experiment = self.copy_experiment()
        for gate in ({'blocked': True, 'reason': 'new local block'},
                     {'until': 1799999999, 'reason': 'new local wait'},
                     {'until': 1799999999, 'source_until': 1799999998,
                      'simulation_translation': True, 'reason': 'invented translation'}):
            state = deepcopy(self.state); state['records'].setdefault('hosts', {})['www.sofmap.com'] = gate
            write(experiment / 'state-export.json', state)
            with self.subTest(gate=gate), self.assertRaisesRegex(ValueError, 'hold'):
                self.prepare(experiment=experiment)
            self.assertFalse((self.case / 'intent').exists())

    def test_existing_output_or_nested_source_output_is_never_modified(self):
        output = self.case / 'existing'
        output.mkdir(parents=True)
        for target in (output, self.capture / 'new-followup', self.experiment / 'new-followup'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.prepare(output=target)
        self.assertEqual([], list(output.iterdir()))
        self.assertEqual(self.source_hashes, [bundle_hashes(p) for p in (self.inputs, self.capture, self.experiment)])

    def test_primary_source_json_is_decoded_and_hashed_from_one_read(self):
        paths = {self.experiment / 'experiment.json', self.experiment / 'state-export.json'}
        counts = {p: 0 for p in paths}
        original = Path.read_bytes

        def read_once(path):
            if path in paths:
                counts[path] += 1
                if counts[path] > 1:
                    raise AssertionError('Source metadata read more than once')
            return original(path)

        with patch.object(Path, 'read_bytes', read_once):
            self.prepare()
        self.assertEqual({p: 1 for p in paths}, counts)


if __name__ == '__main__':
    unittest.main()

"""Schedule archived outcomes without inventing responses for unrequested URLs.

Changing queue order is a simulation. Source observation times, request records
and bytes stay unchanged; the simulation's clock is recorded separately.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, fields
import hashlib
from pathlib import Path
from urllib.parse import urlsplit

from sale_monitor.models import timestamp
from .acquire import Receipt
from .capture import captured_page, verify_capture
from .evidence import normalize
from .inputs import import_state, verify
from .pipeline import existing_task_id, make_task
from .safety import environment, experiment_id, guard, implementation_hash, read, write
from .scheduler import collect
from .stores import SQLite


@dataclass
class TraceReceipt(Receipt):
    source_capture_manifest_sha256: str = ''
    source_receipt_index: int | None = None
    source_evidence_mode: str | None = None
    analysis_reuse: bool = False
    derived_from_dispatch: str | None = None


class CapturedClient:
    def __init__(self, root, verified, method, *, discovery_reuse=False):
        self.root = Path(root)
        self.verified = verified
        self.checksum = hashlib.sha256((self.root / 'capture-manifest.json').read_bytes()).hexdigest()
        self.rows = [(i, row) for i, row in enumerate(verified['receipts']) if row['method'] == method and row['attempts']]
        if not self.rows:
            raise ValueError('No actual recorded attempts for the selected method')
        self.now = min(attempt['started_at'] for _, row in self.rows for attempt in row['attempts'])
        self.start = self.now
        self.hosts, self.last, self.sequence = {}, {}, []
        self.requests = 0  # Replaying evidence never makes an HTTP request.
        self.used = set()
        self.discovery_resources = {r['url']: r for r in verified.get('metadata', {}).get('scope_plan', {}).get('resources', [])
                                    if discovery_reuse and r['kind'] in {'home', 'list'}}
        self.gates = {}
        for row in verified['receipts']:
            if row['error'] == 'shared_host_wait':
                host = urlsplit(row['url']).hostname
                self.gates.setdefault(host, []).extend(deepcopy(row['waits']))
        for host, gate in verified.get('hosts', {}).items():
            self.gates.setdefault(host, []).append(deepcopy(gate))

    def clock(self):
        return self.now

    monotonic = clock

    def sleep(self, seconds):
        self.now += seconds

    def restore(self, records, *, run_id=None):
        for identity, row in records.get('dispatches', {}).items():
            if run_id is not None and not identity.startswith(run_id + ':'):
                continue
            receipt = row.get('receipt', {})
            if receipt and receipt.get('source_capture_manifest_sha256') != self.checksum:
                continue
            index = receipt.get('source_receipt_index')
            if index is not None:
                self.used.add(index)
            self.now = max(self.now, row.get('finished_at_epoch', row['started_at_epoch']))
        for identity, row in records.get('scheduler_waits', {}).items():
            if run_id is not None and not identity.startswith(run_id + ':'):
                continue
            self.now = max(self.now, row.get('finished_at_epoch', row['started_at_epoch']))

    def trace_receipt(self, index, row):
        names = {f.name for f in fields(Receipt)}
        receipt = TraceReceipt(**{key: deepcopy(value) for key, value in row.items() if key in names},
                               source_capture_manifest_sha256=self.checksum, source_receipt_index=index,
                               source_evidence_mode=row['evidence_mode'])
        receipt.evidence_mode = 'captured_queue_simulation'
        return receipt

    def reuse_discovery(self, url, selected_tasks, records, run_id):
        """Derive new discovery work from a committed same-run parser input.

        This cannot supply an acquisition retry or a product observation. The
        scheduler still applies host gates, the persisted deadline and work cap.
        """
        resource = self.discovery_resources.get(url)
        if (not resource or not selected_tasks or any(
                t.get('lab_kind') not in {'list', 'search'} or t.get('lab_store') != resource['store']
                for t in selected_tasks.values())):
            return None
        # Preserve recorded request order when another actual outcome exists.
        if any(i not in self.used and row['url'] == url for i, row in self.rows):
            return None
        prior = [(key, d) for key, d in records.get('dispatches', {}).items()
                 if key.startswith(run_id + ':') and d['url'] == url and d['state'] != 'reserved']
        if any(set(selected_tasks) & set(d['task_ids']) for _, d in prior):
            return None
        outcomes = [(key, d) for key, d in prior if not d.get('receipt', {}).get('analysis_reuse')]
        if not outcomes:
            return None
        identity, dispatch = max(outcomes, key=lambda item: item[1]['slot'])
        receipt = dispatch.get('receipt', {})
        index = receipt.get('source_receipt_index')
        source = next((row for i, row in self.rows if i == index and row['url'] == url), None)
        if (dispatch['state'] != 'committed' or dispatch.get('processing_error') or source is None
                or index not in self.used
                or source.get('resource_kind') not in {'home', 'list'}
                or source['store'] != resource['store'] or source['status'] != 200 or source['error']
                or source.get('body_incomplete') or source.get('body_unavailable')):
            return None
        derived = self.trace_receipt(index, source)
        # A restored reference must match the verified immutable source, not
        # merely name an index that happens to contain a successful response.
        if any(receipt.get(key) != value for key, value in asdict(derived).items()):
            raise ValueError('Committed discovery receipt differs from captured source')
        page = captured_page(self.root, source, method='captured_discovery_reuse')
        if page.url != url:
            return None
        derived.analysis_reuse = True
        derived.derived_from_dispatch = identity
        derived.attempts, derived.waits, derived.elapsed_seconds = [], [], 0.0
        derived.browser_requests = []
        return page, derived

    def fetch(self, url):
        selected = next(((i, row) for i, row in self.rows if i not in self.used and row['url'] == url), None)
        if selected is None:
            # A skipped source task is not a failed request, nor a successful
            # retry. Missing later responses stay explicitly unobserved.
            return None, TraceReceipt(url, None, '', None, 'captured_trace', {}, error='evidence_exhausted',
                                      evidence_mode='captured_queue_simulation', source_capture_manifest_sha256=self.checksum)
        index, row = selected
        self.used.add(index)
        self.now += row['elapsed_seconds']
        receipt = self.trace_receipt(index, row)
        host = urlsplit(url).hostname
        if receipt.error:
            observed = timestamp(receipt.observed_at).timestamp()
            gates = [gate for gate in self.gates.get(host, []) if gate.get('reason') == receipt.error
                     and (gate.get('blocked') or gate.get('until', 0) >= observed)]
            if gates:
                gate = min(gates, key=lambda g: g.get('until', float('inf')))
                copied = deepcopy(gate)
                if 'until' in gate:
                    copied.update(until=self.now + max(0, gate['until'] - observed), source_until=gate['until'],
                                  simulation_translation=True)
                self.hosts[host] = copied
            elif receipt.error == 'authentication_or_challenge':
                self.hosts[host] = {'blocked': True, 'reason': receipt.error, 'status': receipt.status}
        if receipt.error or receipt.status != 200 or receipt.body_incomplete:
            return None, receipt
        return captured_page(self.root, row, method='captured_queue_simulation'), receipt


def queue_study(inputs, label, capture, output, *, architecture='B', method='urllib', budget=120, max_tasks=20):
    inputs, capture, output = Path(inputs), Path(capture), guard(Path(output))
    if architecture == 'C' and environment()['system'] != 'Windows':
        raise ValueError('C requires a real Windows process')
    manifest = verify(inputs)
    if label not in manifest['snapshots']:
        raise ValueError('Unknown pinned input snapshot')
    verified = verify_capture(capture)
    method = {'urllib': 'urllib', 'pooled': 'pooled_http11'}[method]
    client = CapturedClient(capture, verified, method)
    pages = manifest['pages']
    conditions = {'mode': 'captured_queue_simulation', 'input_hash': manifest['input_hash'], 'snapshot': label,
                  'capture_manifest_sha256': client.checksum, 'source_method': method, 'architecture': architecture,
                  'budget': budget, 'max_tasks': max_tasks, 'implementation_hash': implementation_hash()}
    meta_path = output / 'experiment.json'
    if meta_path.exists():
        meta = read(meta_path)
        if meta['conditions'] != conditions:
            raise ValueError('Cannot resume with changed experimental conditions')
    else:
        if output.exists() and any(output.iterdir()):
            raise ValueError('Use an empty experiment directory')
        meta = {'experiment_id': experiment_id(), 'conditions': conditions, 'environment': environment(),
                'production_run_id': None}
        write(meta_path, meta)
    run_id = meta['experiment_id']
    with SQLite(output / 'store') as store:
        if not store.snapshot()['transactions']:
            files, originals, _ = import_state(inputs, label)
            changes = [('source_files', key, row) for key, row in files.items()]
            changes += [('tasks', key, row) for key, row in originals.items()]
            for page in pages:
                key = existing_task_id(originals, page['store'], page['url'])
                original = originals.get(key)
                task = make_task(page['store'], page['url'], verified['receipts'][0]['observed_at'],
                                 group=page['group'], original=original)
                if original is None:
                    task['requested'] = True
                changes.append(('tasks', key, task))
            store.commit('import:' + manifest['snapshots'][label]['data_sha'], changes)
        state = store.snapshot()
        client.restore(state['records'])

        def on_page(task, page, receipt, tasks, cycle, records):
            observation = normalize(task['lab_store'], page, verified['settings']['stores'][task['lab_store']], run_id, receipt)
            return [('observations', observation.offer.key, observation.to_dict())]

        collected = collect(store, client, run_id, {p['url'] for p in pages}, on_page,
                            architecture=architecture, cycles=1, max_tasks=max_tasks, budget=budget)
        state = store.snapshot()
        dispatches = list(state['records'].get('dispatches', {}).values())
        all_receipts = [row['receipt'] for row in dispatches if row['state'] == 'committed']
        result = {'experiment_id': run_id, **conditions, 'http_requests': 0, 'formal_audits_added': 0,
                  'scheduled_stability_samples': 0, 'production_prices_added': 0,
                  'source_experiment_id': verified['metadata']['experiment_id'],
                  'source_actual_attempts': sum(len(row['attempts']) for _, row in client.rows),
                  'replayed_actual_attempts': sum(len(row['attempts']) for row in all_receipts),
                  'replayed_failed_requests': sum(bool(row['error']) and bool(row['attempts']) for row in all_receipts),
                  'evidence_gaps': sum(row['error'] == 'evidence_exhausted' for row in all_receipts),
                  'observations': len(state['records'].get('observations', {})), 'receipts': all_receipts,
                  'scheduling': collected['scheduling'], 'simulated_elapsed_seconds': client.clock() - client.start,
                  'original_pending_retained': sum('lab_key' in t for t in state['records']['tasks'].values()),
                  'limits': ['Queue-order simulation using recorded per-URL outcomes, not new site behavior or real elapsed time',
                             'Skipped source tasks supply no HTTP outcome; no post-failure success is invented',
                             'Source observation times and request sequence stay unchanged; simulation time is separate',
                             'Only the six selected product URLs are eligible; whole-store coverage is not measured']}
        write(output / 'result.json', result)
        store.export(output / 'state-export.json')
        return result

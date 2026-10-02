"""Schedule archived outcomes without inventing responses for unrequested URLs.

Changing queue order is a simulation. Source observation times, request records
and bytes stay unchanged; the simulation's clock is recorded separately.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, fields
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


class CapturedClient:
    def __init__(self, root, verified, method):
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

    def restore(self, records):
        for row in records.get('dispatches', {}).values():
            index = row.get('receipt', {}).get('source_receipt_index')
            if index is not None:
                self.used.add(index)
            self.now = max(self.now, row.get('finished_at_epoch', row['started_at_epoch']))
        for row in records.get('scheduler_waits', {}).values():
            self.now = max(self.now, row.get('finished_at_epoch', row['started_at_epoch']))

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
        names = {f.name for f in fields(Receipt)}
        receipt = TraceReceipt(**{key: deepcopy(value) for key, value in row.items() if key in names},
                               source_capture_manifest_sha256=self.checksum, source_receipt_index=index,
                               source_evidence_mode=row['evidence_mode'])
        receipt.evidence_mode = 'captured_queue_simulation'
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

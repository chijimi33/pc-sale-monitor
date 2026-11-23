"""Apply a verified follow-up capture to a new, complete copy of its queue.

Only selected tasks run. Prior collection budgets, prices, decisions and events
remain historical; newly parsed products have their own phase observations.
"""
from copy import deepcopy
import math
from pathlib import Path

from .capture import verify_capture
from .discovery import Dispatcher, kind
from .evidence import normalize
from .followup import _pinned_json, load_followup
from .followup_provenance import retain_followup
from .queue_study import CapturedClient
from .safety import environment, experiment_id, guard, implementation_hash, read, write
from .scheduler import collect
from .stores import FullJSON, validate


def apply_followup(intent, capture, output, *, method='urllib', budget=120, max_tasks=20):
    output, capture = guard(Path(output)), Path(capture).resolve()
    if (method not in {'urllib', 'pooled', 'browser'} or type(max_tasks) is not int
            or not 1 <= max_tasks <= 20 or isinstance(budget, bool)
            or not isinstance(budget, (int, float)) or not math.isfinite(budget) or not 0 < budget <= 2100):
        raise ValueError('Invalid follow-up application limits')
    cfg = read(Path(__file__).resolve().parents[1] / 'config/sources.json')['stores']
    bundle = load_followup(intent, cfg)
    source = Path(bundle['source']['experiment_path'])
    for path in (source, capture, Path(intent).resolve(), Path(bundle['source']['capture_path'])):
        if output.is_relative_to(path) or path.is_relative_to(output):
            raise ValueError('Follow-up application must be separate from its sources')
    verified = verify_capture(capture)
    if (verified['settings']['stores'] != cfg
            or verified['metadata'].get('followup') != retain_followup(bundle)
            or verified['metadata'].get('scope_plan') != bundle['request_plan']):
        raise ValueError('Capture does not match the complete prepared follow-up intent')
    state, checksum = _pinned_json(source / 'state-export.json')
    source_meta, meta_checksum = _pinned_json(source / 'experiment.json')
    if checksum != bundle['source']['state_export_sha256'] or meta_checksum != bundle['source']['experiment_sha256']:
        raise ValueError('Follow-up source changed while loading')
    validate(state)
    source_method = {'urllib': 'urllib', 'pooled': 'pooled_http11', 'browser': 'browser'}[method]
    client = CapturedClient(capture, verified, source_method)
    selected = [row['task_id'] for row in bundle['tasks']]
    conditions = {'acquisition_mode': 'captured_queue_simulation', 'mode': 'replay', 'backend': 'json',
                  'application': 'verified_followup', 'architecture': 'B',
                  'capture_manifest_sha256': client.checksum, 'capture_source_method': source_method,
                  'capture_plan_hash': bundle['request_plan']['plan_hash'],
                  'source_bundle_hash': bundle['bundle_hash'], 'source_state_sha256': checksum,
                  'selected_task_ids': selected, 'budget': budget, 'max_tasks': max_tasks,
                  'implementation_hash': implementation_hash()}
    meta_path = output / 'experiment.json'
    if meta_path.exists():
        meta = read(meta_path)
        if meta['conditions'] != conditions:
            raise ValueError('Cannot resume with changed follow-up conditions')
    else:
        if output.exists() and any(output.iterdir()):
            raise ValueError('Use a new empty follow-up application directory')
        meta = {'experiment_id': experiment_id(), 'conditions': conditions,
                'environment': environment(), 'production_run_id': None,
                'source_experiment_id': bundle['source']['experiment_id']}
        write(meta_path, meta)
    run_id = meta['experiment_id']
    # The full snapshot (including transaction IDs) is atomically installed.
    # A crash before activation can resume without resetting the source queue.
    store_path = output / 'store/state.json'
    if not store_path.exists():
        write(store_path, state)
    with FullJSON(output / 'store') as store:
        activation = 'followup:activate:' + run_id
        current = store.snapshot()
        if activation not in current['transactions']:
            if current != state:
                raise ValueError('Unactivated follow-up copy differs from its source')
            changes = []
            for row in bundle['tasks']:
                task = deepcopy(row['task'])
                if current['records']['tasks'][row['task_id']] != task:
                    raise ValueError('Selected task differs from the pinned intent')
                task.update(lab_status='pending', lab_selected=True, lab_ready_cycle=0)
                task.pop('lab_reason', None)
                changes.append(('tasks', row['task_id'], task))
            changes.append(('followup_phases', run_id, {'intent': retain_followup(bundle),
                            'source_bundle': deepcopy(bundle),
                            'source_experiment': source_meta,
                            'conditions': conditions}))
            store.commit(activation, changes)
        client.restore(store.snapshot()['records'], run_id=run_id)
        allowed = {r['url'] for r in bundle['request_plan']['resources']}
        dispatcher = Dispatcher(cfg, allowed)

        def on_page(task, page, receipt, tasks, cycle, records):
            if kind(task) == 'list':
                return dispatcher.expand(task, page, receipt, tasks)
            if kind(task) != 'product':
                raise ValueError('Unsupported follow-up task kind')
            observation = normalize(task['lab_store'], page, cfg[task['lab_store']], run_id, receipt)
            key = run_id + ':' + observation.offer.key
            task['lab_phase_observation'] = key
            return [('phase_observations', key, observation.to_dict())]

        try:
            collection = collect(store, client, run_id, allowed, on_page,
                                 architecture='B', cycles=1, max_tasks=max_tasks, budget=budget,
                                 collection_key='followup:' + run_id, task_ids=selected)
        finally:
            store.export(output / 'state-export.json')
        final = store.snapshot()
        records = final['records']
        # No inherited budget or evidence may be relabeled as this phase.
        for namespace in ('scheduler', 'observations', 'decisions', 'events', 'event_state', 'source_files'):
            if any(records.get(namespace, {}).get(k) != v for k, v in state['records'].get(namespace, {}).items()):
                raise ValueError('Follow-up changed inherited ' + namespace)
        if any(final['transactions'].get(k) != v for k, v in state['transactions'].items()):
            raise ValueError('Follow-up changed original transaction history')
        dispatches = [d for k, d in records.get('dispatches', {}).items() if k.startswith(run_id + ':')]
        result = {'experiment_id': run_id, 'mode': 'captured_queue_simulation',
                  'selected_task_ids': selected,
                  'selected_statuses': {k: records['tasks'][k]['lab_status'] for k in selected},
                  'source_task_count': len(state['records']['tasks']),
                  'retained_source_task_count': len(set(state['records']['tasks']) & set(records['tasks'])),
                  'new_task_ids': sorted(set(records['tasks']) - set(state['records']['tasks'])),
                  'new_phase_observations': sum(k.startswith(run_id + ':') for k in records.get('phase_observations', {})),
                  'replayed_actual_attempts': sum(len(d.get('receipt', {}).get('attempts', [])) for d in dispatches),
                  'http_requests': 0, 'production_prices_added': 0, 'formal_audits_added': 0,
                  'scheduled_stability_samples': 0, 'notifications': 0,
                  'collection': collection['scheduling'],
                  'limits': ['Captured follow-up applied to a separate complete queue copy',
                             'Prior observations and decisions are historical and are not reevaluated',
                             'Product observations require a separate comparison phase before any decision']}
        write(output / 'result.json', result)
        return result

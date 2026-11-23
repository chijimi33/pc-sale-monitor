"""Evaluate a fresh, bounded capture without modifying inherited observations."""
from copy import deepcopy
import math
from pathlib import Path

from sale_monitor.models import Offer, iso, timestamp
from .capture import verify_capture
from .comparison import load_comparison
from .comparison_provenance import retain_comparison
from .evidence import decide, normalize, reconcile_discovery
from .followup import _pinned_json
from .history import HistoryIndex
from .queue_study import CapturedClient
from .safety import digest, environment, experiment_id, guard, implementation_hash, read, write
from .scheduler import collect
from .stores import FullJSON, validate


def _decisions(bundle, records, run_id, now, historical):
    observations = {key: value for key, value in records.get('phase_observations', {}).items()
                    if key.startswith(run_id + ':') and value['offer'].get('observed_run_id') == run_id}
    by_task = {}
    for row in bundle['tasks']:
        value = observations.get(records['tasks'][row['task_id']].get('lab_phase_observation'))
        if value is not None:
            by_task[row['task_id']] = Offer.from_dict(value['offer'])
    result = []
    for row in bundle['candidates']:
        key = row['task_id']
        original = next(t['task'] for t in bundle['tasks'] if t['task_id'] == key)
        candidate = by_task.get(key)
        known = row['product_task_ids']
        missing = [k for k in known if k not in by_task]
        fetched = {(o.store, o.url) for o in by_task.values()}
        missing_catalog = {k: v for k, v in row['known_catalog'].items() if (v['store'], v['url']) not in fetched}
        holds = []
        if missing or missing_catalog:
            holds.append('known_comparator_not_verified_this_run')
        if row['unresolved_discovery']:
            holds.append('comparison_discovery_incomplete')
        if candidate and candidate.identity != row['expected_identity']:
            holds.append('candidate_identity_changed_or_unverified')
        mismatched = [k for k in known if k in by_task and by_task[k].identity != row['expected_identity']]
        if mismatched:
            holds.append('known_comparator_identity_changed_or_unverified')
        pair = {}
        for points in (False, True):
            basis = 'points' if points else 'payment'
            if candidate is None:
                decision = {'offer_key': None, 'basis': basis, 'status': 'insufficient', 'rule': None,
                            'reasons': ['candidate_not_observed_in_run'], 'comparisons': []}
                evidence = {'scope': 'pinned_history_for_B_only', 'basis': basis, 'sources': [],
                            'reason': 'Current candidate unavailable; historical candidate price was not substituted'}
            else:
                history, evidence = historical.select(candidate, now, run_id, points=points)
                decision = decide(candidate, [by_task[k] for k in known if k in by_task], history, now, run_id, points=points)
            if holds:
                decision.update(status='insufficient', rule=None, reasons=sorted(set(decision['reasons'] + holds)))
            decision.update(candidate_url=original['url'], history_scope='pinned_history_B_only_current_comparisons_same_run',
                            history_evidence=evidence)
            pair[basis] = decision
        chosen = 'payment' if pair['payment']['status'] == 'accepted' or pair['points']['status'] != 'accepted' else 'points'
        result.append({'candidate_task_id': key, 'candidate_url': original['url'], 'experiment_id': run_id,
                       'decision_time': iso(now), 'selected_basis': chosen, **pair,
                       'selected_decision': deepcopy(pair[chosen]), 'missing_product_task_ids': missing,
                       'missing_catalog': missing_catalog, 'identity_unverified_task_ids': mismatched,
                       'unresolved_discovery': deepcopy(row['unresolved_discovery']),
                       'observation_keys': sorted(k for k, v in observations.items()
                           if (v['offer']['store'], v['offer']['url']) in {
                               (by_task[t].store, by_task[t].url) for t in [key, *known] if t in by_task}),
                       'notification_status': 'not_sent_lab_only'})
    return result


def _reconcile_products(bundle, records, run_id):
    """One shared response retains every alias's listing evidence, in any order."""
    groups = {}
    for row in bundle['tasks']:
        task = records['tasks'][row['task_id']]
        key = task.get('lab_phase_observation')
        if isinstance(key, str) and key.startswith(run_id + ':'):
            groups.setdefault(key, []).append(task)
    changes = []
    for key, tasks in sorted(groups.items()):
        observation = records['phase_observations'][key]
        sources = {digest(source): source for task in tasks for source in task.get('lab_discovery_evidence', [])}
        if not sources:
            continue
        role = 'candidate' if any(t['lab_role'] == 'candidate' for t in tasks) else 'comparison'
        value = reconcile_discovery(observation, [sources[k] for k in sorted(sources)], role)
        if value != observation:
            changes.append(('phase_observations', key, value))
    return changes


def apply_comparison(intent, capture, output, *, method='urllib', budget=120, max_tasks=20):
    output, capture = guard(Path(output)), Path(capture).resolve()
    if (method not in {'urllib', 'pooled', 'browser'} or type(max_tasks) is not int or not 1 <= max_tasks <= 20
            or type(budget) not in (int, float) or not math.isfinite(budget) or not 0 < budget <= 2100):
        raise ValueError('Invalid comparison application limits')
    config = read(Path(__file__).resolve().parents[1] / 'config/sources.json')['stores']
    bundle = load_comparison(intent, config)
    source = Path(bundle['source']['experiment_path'])
    for path in (source, capture, Path(intent).resolve(), Path(bundle['source']['capture_path'])):
        if output.is_relative_to(path) or path.is_relative_to(output):
            raise ValueError('Comparison output must be separate from its sources')
    verified = verify_capture(capture)
    if (verified['settings']['stores'] != config
            or verified['metadata'].get('comparison') != retain_comparison(bundle)
            or verified['metadata'].get('scope_plan') != bundle['request_plan']):
        raise ValueError('Capture does not match the complete prepared comparison intent')
    state, checksum = _pinned_json(source / 'state-export.json')
    source_meta, meta_checksum = _pinned_json(source / 'experiment.json')
    if checksum != bundle['source']['state_export_sha256'] or meta_checksum != bundle['source']['experiment_sha256']:
        raise ValueError('Comparison source changed while loading')
    validate(state)
    source_method = {'urllib': 'urllib', 'pooled': 'pooled_http11', 'browser': 'browser'}[method]
    client = CapturedClient(capture, verified, source_method)
    selected = [r['task_id'] for r in bundle['tasks']]
    conditions = {'acquisition_mode': 'captured_queue_simulation', 'mode': 'replay', 'backend': 'json',
        'application': 'verified_comparison', 'architecture': 'B', 'capture_manifest_sha256': client.checksum,
        'capture_source_method': source_method, 'capture_plan_hash': bundle['request_plan']['plan_hash'],
        'source_bundle_hash': bundle['bundle_hash'], 'source_state_sha256': checksum,
        'selected_task_ids': selected, 'budget': budget, 'max_tasks': max_tasks, 'implementation_hash': implementation_hash()}
    times = [timestamp(r['observed_at']) for r in verified['receipts'] if r['method'] == source_method and r.get('attempts')]
    if not times or any(t is None for t in times):
        raise ValueError('Comparison needs dated actual attempts')
    now = max(times)
    historical = HistoryIndex(state['records'].get('source_files', {}), now)
    meta_path = output / 'experiment.json'
    if meta_path.exists():
        meta = read(meta_path)
        if meta['conditions'] != conditions:
            raise ValueError('Cannot resume with changed comparison conditions')
    else:
        if output.exists() and any(output.iterdir()):
            raise ValueError('Use a new empty comparison output')
        meta = {'experiment_id': experiment_id(), 'conditions': conditions, 'environment': environment(),
                'production_run_id': None, 'source_experiment_id': bundle['source']['experiment_id']}
        write(meta_path, meta)
    run_id = meta['experiment_id']
    if not (output / 'store/state.json').exists():
        write(output / 'store/state.json', state)
    with FullJSON(output / 'store') as store:
        activation = 'comparison:activate:' + run_id
        current = store.snapshot()
        if activation not in current['transactions']:
            if current != state:
                raise ValueError('Unactivated comparison copy differs from its source')
            changes = []
            for row in bundle['tasks']:
                task = deepcopy(row['task'])
                if current['records']['tasks'][row['task_id']] != task:
                    raise ValueError('Selected product differs from pinned intent')
                task.update(lab_status='pending', lab_selected=True, lab_ready_cycle=0)
                task.pop('lab_reason', None)
                changes.append(('tasks', row['task_id'], task))
            changes.append(('comparison_phases', run_id, {'intent': retain_comparison(bundle),
                            'source_bundle': deepcopy(bundle), 'source_experiment': source_meta, 'conditions': conditions}))
            store.commit(activation, changes)
        client.restore(store.snapshot()['records'], run_id=run_id)

        def on_page(task, page, receipt, tasks, cycle, records):
            if page.url != task['url']:
                raise ValueError('Redirected comparison input requires explicit review')
            observation = normalize(task['lab_store'], page, config[task['lab_store']], run_id, receipt).to_dict()
            key = run_id + ':' + Offer.from_dict(observation['offer']).key
            task['lab_phase_observation'] = key
            return [('phase_observations', key, observation)]

        try:
            collection = collect(store, client, run_id, {r['url'] for r in bundle['request_plan']['resources']}, on_page,
                architecture='B', cycles=1, max_tasks=max_tasks, budget=budget,
                collection_key='comparison:' + run_id, task_ids=selected)
            changes = _reconcile_products(bundle, store.snapshot()['records'], run_id)
            if changes:
                store.commit('comparison:discovery-evidence:' + run_id, changes)
            decisions = _decisions(bundle, store.snapshot()['records'], run_id, now, historical)
            store.commit('comparison:decide:' + run_id,
                         [('phase_decisions', run_id + ':' + d['candidate_task_id'], d) for d in decisions])
        finally:
            store.export(output / 'state-export.json')
        final = store.snapshot()
        records = final['records']
        for namespace, rows in state['records'].items():
            for key, value in rows.items():
                if namespace == 'hosts' or namespace == 'tasks' and key in selected:
                    continue
                if records.get(namespace, {}).get(key) != value:
                    raise ValueError('Comparison changed inherited ' + namespace)
        if any(final['transactions'].get(k) != v for k, v in state['transactions'].items()):
            raise ValueError('Comparison changed original transaction history')
        dispatches = [d for k, d in records.get('dispatches', {}).items() if k.startswith(run_id + ':')]
        result = {'experiment_id': run_id, 'mode': 'captured_queue_simulation',
            'selected_task_ids': selected, 'selected_statuses': {k: records['tasks'][k]['lab_status'] for k in selected},
            'source_task_count': len(state['records']['tasks']), 'retained_source_task_count': len(set(state['records']['tasks']) & set(records['tasks'])),
            'new_phase_observations': sum(k.startswith(run_id + ':') for k in records.get('phase_observations', {})),
            'decisions': decisions, 'decision_time': iso(now),
            'replayed_actual_attempts': sum(len(d.get('receipt', {}).get('attempts', [])) for d in dispatches),
            'http_requests': 0, 'production_prices_added': 0, 'formal_audits_added': 0,
            'scheduled_stability_samples': 0, 'notifications': 0, 'collection': collection['scheduling'],
            'limits': ['Known product refresh only; unresolved store discovery remains held',
                       'Current comparisons use this capture only; history is used only for rule B',
                       'Windows experiment does not authorize production adoption or notification']}
        write(output / 'result.json', result)
        return result

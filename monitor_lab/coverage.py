"""Store-level scope and evidence accounting for bounded experiments.

Counts come from committed records, so resume does not erase prior acquisition.
One observed product never implies that a store's whole catalog was covered.
"""
from collections import Counter

from sale_monitor.models import STORES
from .capture import parser_representation
from .evidence import FIELDS


def coverage_report(resources, records, original_tasks, config, mode, run_id):
    if mode not in {'live', 'replay', 'captured_queue_simulation'} or not set(STORES) <= set(config):
        raise ValueError('Coverage requires the fixed ten stores and a known experiment mode')
    resources = list(resources.values())
    tasks = records.get('tasks', {})
    observations = list(records.get('observations', {}).values())
    dispatches = list(records.get('dispatches', {}).values())
    stores = ({r['store'] for r in resources} | {t['lab_store'] for t in tasks.values()}
              | {t['lab_store'] for t in original_tasks.values()}
              | {o['offer']['store'] for o in observations})
    if stores - set(STORES) or any(r['kind'] not in {'product', 'home', 'list'} for r in resources):
        raise ValueError('Coverage contains an unsupported store or resource kind')
    if len({r['url'] for r in resources}) != len(resources):
        raise ValueError('Coverage resource ownership is ambiguous')
    by_url = {r['url']: r for r in resources}
    rows = {}
    for store in STORES:
        permitted = {kind: sorted(r['url'] for r in resources if r['store'] == store and r['kind'] == kind)
                     for kind in ('product', 'home', 'list')}
        originals = {key for key, task in original_tasks.items() if task['lab_store'] == store}
        retained = originals & tasks.keys()
        complete = sum(tasks[key].get('lab_status') == 'complete' for key in retained)
        store_tasks = [task for task in tasks.values() if task['lab_store'] == store]
        pending = [task for task in store_tasks if task.get('lab_status') != 'complete']
        store_dispatches = [d for d in dispatches if by_url.get(d['url'], {}).get('store') == store]
        receipts = [d['receipt'] for d in store_dispatches if d.get('receipt') is not None]
        current, excluded = [], Counter()
        for observation in observations:
            offer = observation['offer']
            if offer['store'] != store:
                continue
            if offer.get('observed_run_id') != run_id:
                excluded['other_run'] += 1
                continue
            if offer['url'] not in permitted['product']:
                excluded['outside_permitted_product_resources'] += 1
                continue
            receipt = observation.get('receipt', {})
            try:
                representation = parser_representation(receipt)
            except ValueError:
                representation = {}
            if (receipt.get('url') != offer['url'] or receipt.get('status') != 200
                    or receipt.get('error') or receipt.get('body_incomplete') or receipt.get('body_unavailable')
                    or representation.get('url', offer['url']) != offer['url']
                    or not representation.get('body_sha256') or representation.get('body_incomplete')
                    or representation.get('body_unavailable')):
                excluded['receipt_not_usable'] += 1
                continue
            current.append(observation)
        observed = sorted({o['offer']['url'] for o in current})
        field_evidence = {}
        for name in FIELDS:
            field_rows = [o.get('fields', {}).get(name, {}) for o in current]
            values = [r.get('selected_value', r.get('value')) for r in field_rows]
            field_evidence[name] = {
                'status_counts': dict(sorted(Counter(r.get('status', 'missing_record') for r in field_rows).items())),
                'unknown_values': sum(v is None or v == '' or name == 'stock' and v == 'unknown' for v in values),
                'with_sources': sum(bool(r.get('sources')) for r in field_rows)}
        rows[store] = {
            'adapter': config[store].get('adapter', 'unknown'), 'permitted_resources': permitted,
            'product_resources_observed': observed,
            'product_resources_missing': sorted(set(permitted['product']) - set(observed)),
            'original_tasks': {'expected': len(originals), 'retained': len(retained),
                               'missing': len(originals - tasks.keys()), 'complete_in_lab': complete,
                               'pending_in_lab': len(retained) - complete},
            'selected_tasks': sum(bool(t.get('lab_selected')) for t in store_tasks),
            'task_status_counts': dict(sorted(Counter(t.get('lab_status', 'unknown') for t in store_tasks).items())),
            'pending_reason_counts': dict(sorted(Counter(t.get('lab_last_error') or t.get('lab_reason') or
                ('not_selected_for_this_experiment' if not t.get('lab_selected') else 'pending') for t in pending).items())),
            'dispatch_state_counts': dict(sorted(Counter(d['state'] for d in store_dispatches).items())),
            'confirmed_http_attempts': sum(len(r.get('attempts', [])) for r in receipts
                                           if r.get('evidence_mode') != 'captured_queue_simulation'),
            'recorded_source_http_attempts': sum(len(r.get('attempts', [])) for r in receipts
                                                if r.get('evidence_mode') == 'captured_queue_simulation'),
            'replay_receipts': sum(r.get('evidence_mode') in {'replay', 'fixture_replay', 'captured_queue_simulation'} for r in receipts),
            'discovery_analysis_reuses': sum(bool(r.get('analysis_reuse')) for r in receipts),
            'receipt_error_counts': dict(sorted(Counter(r['error'] for r in receipts if r.get('error')).items())),
            'current_run_observations': len(current), 'other_run_observations': excluded['other_run'],
            'excluded_observation_counts': dict(sorted(excluded.items())), 'field_evidence': field_evidence}
    return {
        'schema': 1, 'monitored_store_count': len(STORES), 'excluded_stores': ['rakuten'], 'mode': mode,
        'scope': 'cumulative_committed_experiment_records', 'full_store_coverage_proven': False,
        'stores_with_current_run_observations': sum(bool(r['current_run_observations']) for r in rows.values()),
        'totals': {'permitted_resources': len(resources),
                   'permitted_product_resources': sum(len(r['permitted_resources']['product']) for r in rows.values()),
                   'product_resources_observed': sum(len(r['product_resources_observed']) for r in rows.values()),
                   'current_run_observations': sum(r['current_run_observations'] for r in rows.values()),
                   'recorded_dispatches': len(dispatches),
                   'confirmed_http_attempts': sum(r['confirmed_http_attempts'] for r in rows.values()),
                   'recorded_source_http_attempts': sum(r['recorded_source_http_attempts'] for r in rows.values()),
                   'discovery_analysis_reuses': sum(r['discovery_analysis_reuses'] for r in rows.values())},
        'unattributed_dispatches': sum(d['url'] not in by_url for d in dispatches),
        'stores': rows,
        'limits': ['A parsed product is not proof that its price or every field is verified.',
                   'Field statuses and unknown values are reported separately; neither is an acceptance rate.',
                   'Permitted resources bound this experiment, not a store catalog or production coverage.',
                   'Replay observations and retained historical source files are not current live prices.',
                   'Reserved or interrupted dispatches do not prove a completed HTTP attempt.']}

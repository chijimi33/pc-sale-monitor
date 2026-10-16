"""Explicit acquisition intent derived from verified, still-pending discoveries.

Preparing or loading intent is read-only with respect to its source experiment;
it neither acquires pages nor completes or resets any queue task.
"""
from copy import deepcopy
from dataclasses import asdict, fields
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from sale_monitor.models import STORES, iso, timestamp
from .acquire import Receipt
from .capture import captured_page, parser_representation, verify_capture
from .discovery import kind
from .followup_provenance import retain_followup
from .queue_study import TraceReceipt
from .queueing import expand_shared_list, expand_unknown_search, resource_url, task_age
from .request_plan import FORMAT as PLAN_FORMAT, validate_plan
from .safety import digest, guard, write
from .stores import validate as validate_state

FORMAT = 'pc-sale-monitor-followup-v1'


def _pinned_json(path):
    # Hash and decode the same bytes, never a second read of mutable metadata.
    body = Path(path).read_bytes()
    return json.loads(body.decode('utf-8-sig')), hashlib.sha256(body).hexdigest()


def _ids(values, label, *, maximum=None):
    if (not isinstance(values, (list, tuple)) or not values
            or any(not isinstance(v, str) or not v for v in values)
            or len(set(values)) != len(values) or maximum and len(values) > maximum):
        raise ValueError('Expected explicit unique ' + label)
    return list(values)


def _ancestor_contexts(current, config):
    """Load hash-bound ancestral snapshots; never drop their discovery evidence."""
    contexts, seen = [current], {current['run_id']}
    while contexts[-1]['meta']['conditions'].get('application') == 'verified_followup':
        context = contexts[-1]
        if len(contexts) >= 32:
            raise ValueError('Follow-up ancestry exceeds the supported depth')
        phase = context['records']['followup_phases'][context['run_id']]
        bundle = phase['source_bundle']
        unsigned = {k: v for k, v in bundle.items() if k != 'bundle_hash'}
        retained = retain_followup(bundle)
        if (digest(unsigned) != bundle['bundle_hash'] or phase['intent'] != retained
                or context['verified']['metadata'].get('followup') != retained
                or context['meta']['conditions'].get('source_bundle_hash') != bundle['bundle_hash']
                or phase['conditions'] != context['meta']['conditions']):
            raise ValueError('Follow-up ancestry differs from retained capture provenance')
        source = bundle['source']
        root, capture = Path(source['experiment_path']), Path(source['capture_path'])
        meta, meta_sha = _pinned_json(root / 'experiment.json')
        state, state_sha = _pinned_json(root / 'state-export.json')
        manifest, capture_sha = _pinned_json(capture / 'capture-manifest.json')
        verified = verify_capture(capture)
        run_id, conditions = meta['experiment_id'], meta['conditions']
        method = conditions['capture_source_method']
        if (run_id in seen or source['experiment_id'] != run_id or phase['source_experiment'] != meta
                or meta_sha != source['experiment_sha256'] or state_sha != source['state_export_sha256']
                or capture_sha != source['capture_manifest_sha256'] or manifest != verified['manifest']
                or verified['settings']['stores'] != config or method != source['capture_source_method']
                or conditions.get('capture_manifest_sha256') != capture_sha
                or conditions.get('capture_plan_hash') != verified['metadata']['scope_plan']['plan_hash']
                or conditions.get('acquisition_mode') != 'captured_queue_simulation'):
            raise ValueError('Follow-up ancestral source changed or has incompatible provenance')
        validate_state(state)
        if any(context['transactions'].get(k) != v for k, v in state['transactions'].items()):
            raise ValueError('Follow-up dropped ancestral transactions')
        for namespace, rows in state['records'].items():
            inherited = context['records'].get(namespace, {})
            if not set(rows) <= set(inherited):
                raise ValueError('Follow-up dropped ancestral records')
            if namespace not in {'tasks', 'hosts'} and any(inherited[k] != v for k, v in rows.items()):
                raise ValueError('Follow-up rewrote ancestral evidence')
        changes = []
        for row in bundle['tasks']:
            if state['records']['tasks'][row['task_id']] != row['task']:
                raise ValueError('Follow-up changed its original selected task')
            task = deepcopy(row['task'])
            task.update(lab_status='pending', lab_selected=True, lab_ready_cycle=0)
            task.pop('lab_reason', None)
            changes.append(('tasks', row['task_id'], task))
        changes.append(('followup_phases', context['run_id'], phase))
        if context['transactions'].get('followup:activate:' + context['run_id']) != digest(changes):
            raise ValueError('Follow-up ancestry has no matching activation transaction')
        contexts.append({'run_id': run_id, 'records': state['records'], 'transactions': state['transactions'],
                         'verified': verified, 'capture': capture, 'checksum': capture_sha, 'method': method,
                         'meta': meta, 'state_sha': state_sha, 'meta_sha': meta_sha})
        seen.add(run_id)
    return contexts


def _dispatch(identity, records, run_id, verified, checksum, method):
    """Authenticate every trace-receipt field, including its original reuse."""
    if not isinstance(identity, str) or not identity.startswith(run_id + ':'):
        raise ValueError('Discovery dispatch must belong to the same experiment')
    dispatch = records['dispatches'][identity]
    receipt = dispatch['receipt']
    index = receipt.get('source_receipt_index')
    if (dispatch['state'] != 'committed' or dispatch.get('processing_error')
            or type(index) is not int or not 0 <= index < len(verified['receipts'])
            or type(dispatch.get('slot')) is not int or identity != f"{run_id}:{dispatch['slot']}"):
        raise ValueError('Discovery needs a committed successful parent dispatch')
    ids = _ids(dispatch['task_ids'], 'dispatch task IDs')
    source = verified['receipts'][index]
    if (source['method'] != method or source.get('resource_kind') not in {'home', 'list'}
            or source['status'] != 200 or source.get('error') or not source.get('attempts')
            or source.get('body_incomplete') or source.get('body_unavailable')
            or timestamp(source.get('observed_at')) is None or dispatch['url'] != source['url']):
        raise ValueError('Discovery source is not a successful captured home/list response')
    names = {field.name for field in fields(Receipt)}
    expected = asdict(TraceReceipt(**{k: deepcopy(v) for k, v in source.items() if k in names},
                     source_capture_manifest_sha256=checksum, source_receipt_index=index,
                     source_evidence_mode=source['evidence_mode']))
    expected.update(evidence_mode='captured_queue_simulation', task_ids=ids)
    original_id, original = identity, dispatch
    if receipt.get('analysis_reuse'):
        original_id = receipt.get('derived_from_dispatch')
        if (not isinstance(original_id, str) or not original_id.startswith(run_id + ':')
                or original_id == identity):
            raise ValueError('Invalid original discovery dispatch reference')
        original = records['dispatches'][original_id]
        if (original.get('receipt', {}).get('analysis_reuse')
                or original.get('slot', dispatch['slot']) >= dispatch['slot']):
            raise ValueError('Unsupported chained or forward discovery reuse')
        _, original_source, _, _ = _dispatch(original_id, records, run_id, verified, checksum, method)
        if original_source != source:
            raise ValueError('Discovery reuse points to a different source receipt')
        expected.update(analysis_reuse=True, derived_from_dispatch=original_id,
                        attempts=[], waits=[], elapsed_seconds=0.0, browser_requests=[])
    if receipt != expected:
        raise ValueError('Committed discovery receipt differs from captured source')
    for key in ids:
        parent = records['tasks'][key]
        history = 'lab_analysis_reuses' if receipt['analysis_reuse'] else 'lab_attempts'
        if (parent.get('lab_store') != source['store'] or resource_url(parent) != source['url']
                or receipt not in parent.get(history, [])):
            raise ValueError('Parent task does not retain its committed discovery receipt')
    return dispatch, source, original_id, original


def _lineage(key, task, records, run_id, verified, capture, checksum, method, config, transactions, contexts=None):
    parents = _ids(task.get('lab_parent_keys'), 'discovery parent IDs')
    tasks = records['tasks']
    for parent_id in parents:
        parent = tasks[parent_id]
        if (parent.get('lab_store') != task['lab_store'] or parent.get('lab_status') != 'complete'
                or key not in parent.get('lab_child_keys', [])):
            raise ValueError('Discovery parent/child references disagree')
    evidence = task.get('lab_discovery_evidence')
    if (not isinstance(evidence, list) or not evidence or not all(isinstance(e, dict) for e in evidence)
            or len({digest(e) for e in evidence}) != len(evidence)):
        raise ValueError('Missing or duplicated discovery source records')
    proven, sources = set(), []
    contexts = contexts or [{'run_id': run_id, 'records': records, 'transactions': transactions,
                            'verified': verified, 'capture': capture, 'checksum': checksum, 'method': method}]
    for record in evidence:
        matches = []
        candidates = [(ctx, ref, dispatch) for ctx in contexts
                      for ref, dispatch in ctx['records'].get('dispatches', {}).items()]
        for context, dispatch_id, candidate in candidates:
            context_id = context['run_id']
            if (not dispatch_id.startswith(context_id + ':') or candidate.get('state') != 'committed'
                    or not set(parents).intersection(candidate.get('task_ids', []))):
                continue
            receipt = candidate.get('receipt', {})
            source_fields = ('method', 'source_capture_manifest_sha256', 'source_receipt_index',
                             'source_evidence_mode', 'analysis_reuse', 'derived_from_dispatch')
            if any(record.get(field) != receipt.get(field) for field in source_fields):
                continue
            dispatch, source, original_id, original = _dispatch(
                dispatch_id, context['records'], context_id, context['verified'], context['checksum'], context['method'])
            if any(not re.fullmatch('[0-9a-f]{64}', context['transactions'].get('acquire:' + ref, ''))
                   for ref in {dispatch_id, original_id}):
                raise ValueError('Discovery dispatch has no committed transaction')
            page = captured_page(context['capture'], source, method='captured_discovery_reuse')
            representation = parser_representation(source)
            expected_source = {'url': page.url, 'observed_at': page.observed_at, 'http_status': page.status,
                               'body_sha256': representation['body_sha256'],
                               'body_kind': representation.get('body_kind', 'http_response'),
                               'evidence_mode': receipt['evidence_mode'],
                               **{field: receipt[field] for field in source_fields}}
            if page.url != dispatch['url']:
                raise ValueError('Unsupported redirected discovery parser input')
            if {k: v for k, v in record.items() if k != 'discovered'} != expected_source:
                raise ValueError('Discovery source record differs from captured source')
            for parent_id in sorted(set(parents).intersection(dispatch['task_ids'])):
                source_tasks = context['records']['tasks']
                parent = source_tasks[parent_id]
                if parent.get('lab_status') != 'complete' or key not in parent.get('lab_child_keys', []):
                    raise ValueError('Ancestral discovery parent did not retain its child')
                if kind(parent) == 'search':
                    parent_ids = [k for k in dispatch['task_ids'] if source_tasks[k].get('query') == parent['query']]
                    child = expand_unknown_search(parent, page, config[task['lab_store']])
                    child.update(lab_resource_url=child['url'], created_at=task_age(parent), lab_role='comparison')
                    children = [child]
                elif kind(parent) == 'list':
                    scope = lambda t: (t['lab_store'], kind(t), t.get('kind', 'comparison'), t.get('sale_page', False))
                    parent_ids = [k for k in dispatch['task_ids'] if scope(source_tasks[k]) == scope(parent)]
                    children, _ = expand_shared_list([source_tasks[k] for k in parent_ids], page, config[task['lab_store']])
                else:
                    raise ValueError('Unsupported discovery parent kind')
                derived = [child for child in children if resource_url(child) == resource_url(task)
                           and kind(child) == kind(task)
                           and {k: child.get(k) for k in ('title', 'expires_at', 'kind', 'sale_page')} == record['discovered']
                           and (kind(child) == 'product' or child.get('query') == task.get('query'))]
                if not derived:
                    raise ValueError('Selected URL/kind is not derived from the captured source HTML')
                if not set(parent_ids) <= set(parents):
                    raise ValueError('Selected task dropped shared discovery parents')
                if any(source_tasks[k].get('lab_resolution', {}).get(field) != value
                       for k in parent_ids for field, value in expected_source.items()):
                    raise ValueError('Parent resolution differs from discovery evidence')
                if not set().union(*(set(source_tasks[k].get('lab_dependencies', [])) for k in parent_ids)) <= set(task.get('lab_dependencies', [])):
                    raise ValueError('Selected task dropped inherited dependencies')
                proof = {'parent_keys': parent_ids, 'evidence': deepcopy(record),
                         'dispatch_id': dispatch_id, 'dispatch': deepcopy(dispatch),
                         'original_dispatch_id': original_id, 'original_dispatch': deepcopy(original),
                         'receipt': deepcopy(source)}
                if context_id != run_id:
                    proof['source_context'] = {'experiment_id': context_id,
                        'experiment_sha256': context['meta_sha'], 'state_export_sha256': context['state_sha'],
                        'capture_manifest_sha256': context['checksum']}
                if proof not in matches:
                    matches.append(proof)
                proven.update(parent_ids)
        if not matches:
            raise ValueError('Discovery record has no authenticated committed parent dispatch')
        sources.extend(matches)
    if proven != set(parents):
        raise ValueError('Not every discovery parent has verified source evidence')
    return {'task_id': key, 'task': deepcopy(task),
            'parents': {k: deepcopy(tasks[k]) for k in parents}, 'source_records': sources}


def _check_gates(resources, records, captured, run_id, contexts=None):
    """Never replace a new local hold with an older capture's host state."""
    for host in {urlsplit(r['url']).hostname for r in resources}:
        local, original = records.get('hosts', {}).get(host, {}), captured.get(host, {})
        if not local or local == original:
            continue
        if local.get('simulation_translation') is True and 'until' in original:
            restored = {k: v for k, v in local.items() if k not in {'source_until', 'simulation_translation'}}
            restored['until'] = local.get('source_until')
            if restored == original:
                contexts = contexts or [{'run_id': run_id, 'records': records}]
                candidates = [(ctx, identity, dispatch) for ctx in contexts
                              for identity, dispatch in ctx['records'].get('dispatches', {}).items()]
                for context, identity, dispatch in candidates:
                    receipt = dispatch.get('receipt', {})
                    observed = timestamp(receipt.get('observed_at'))
                    if (identity.startswith(context['run_id'] + ':') and dispatch.get('state') == 'committed'
                            and dispatch.get('hosts_after', {}).get(host) == local
                            and urlsplit(dispatch['url']).hostname == host and observed
                            and receipt.get('error') == original.get('reason')
                            and local['until'] == dispatch['finished_at_epoch'] + max(0, original['until'] - observed.timestamp())):
                        break
                else:
                    raise ValueError('Unsupported translated host hold without its source dispatch')
                continue
        raise ValueError('Selected host has a new or inconsistent local hold')


def _build(experiment, capture, task_ids, created_at, config):
    task_ids = _ids(task_ids, 'task IDs (1..20)', maximum=20)
    if not isinstance(created_at, str) or timestamp(created_at) is None:
        raise ValueError('Follow-up needs a valid created_at')
    experiment, capture = Path(experiment).resolve(), Path(capture).resolve()
    meta, meta_sha = _pinned_json(experiment / 'experiment.json')
    state, state_sha = _pinned_json(experiment / 'state-export.json')
    manifest, capture_sha = _pinned_json(capture / 'capture-manifest.json')
    verified = verify_capture(capture)  # Complete authoritative snapshot, never aliases/partial files.
    if manifest != verified['manifest']:
        raise ValueError('Capture manifest changed during verification')
    if config != verified['settings'].get('stores'):
        raise ValueError('Follow-up store configuration differs from captured settings')
    conditions, run_id = meta['conditions'], meta['experiment_id']
    scope = verified['metadata'].get('scope_plan')
    if (not isinstance(run_id, str) or not run_id.startswith('lab-') or not scope
            or conditions.get('capture_manifest_sha256') != capture_sha
            or conditions.get('capture_plan_hash') != scope['plan_hash']
            or conditions.get('acquisition_mode') != 'captured_queue_simulation'):
        raise ValueError('Experiment does not reference this scoped capture')
    method = conditions['capture_source_method']
    if method not in {row['method'] for row in verified['receipts']}:
        raise ValueError('Experiment capture source method is unavailable')
    validate_state(state)
    imports = [key[len('import:'):] for key in state['transactions'] if key.startswith('import:')]
    if len(imports) != 1 or not re.fullmatch('[0-9a-f]{40}', imports[0]):
        raise ValueError('Unsupported backlog identity: one pinned data commit is required')
    records, resources, selected = state['records'], [], []
    contexts = _ancestor_contexts({'run_id': run_id, 'records': records, 'transactions': state['transactions'],
                                  'verified': verified, 'capture': capture, 'checksum': capture_sha,
                                  'method': method, 'meta': meta, 'state_sha': state_sha, 'meta_sha': meta_sha}, config)
    for key in task_ids:
        task = records['tasks'][key]
        if (task.get('lab_status') != 'evidence_wait'
                or task.get('lab_reason') != 'discovered_url_outside_selected_resources'
                or task.get('lab_selected') is not False or kind(task) not in {'list', 'product'}
                or task.get('lab_role') not in {'candidate', 'comparison', 'discovery'}
                or not isinstance(task.get('lab_attempts'), list)):
            raise ValueError('Select only pending out-of-scope list/product discoveries')
        task_age(task)  # Validate without replacing either original age string.
        url = resource_url(task)
        if url != task.get('url'):
            raise ValueError('Selected task resource URL differs from its discovered URL')
        resource = {'store': task['lab_store'], 'url': url, 'kind': kind(task)}
        if any(row['url'] == url for row in scope['resources']):
            raise ValueError('Selected discovery is already in the captured scope')
        if resource not in resources:
            resources.append(resource)
        selected.append(_lineage(key, task, records, run_id, verified, capture, capture_sha,
                                 method, config, state['transactions'], contexts))
    chosen = {row['store'] for row in resources}
    plan = {'format': PLAN_FORMAT, 'created_at': created_at, 'source_data_sha': scope['source_data_sha'],
            'resources': resources, 'not_requested': [
                {'store': store, 'reason': 'No explicit follow-up task selected for this store'}
                for store in STORES if store not in chosen]}
    plan['plan_hash'] = digest(plan)
    validate_plan(plan, config)
    _check_gates(resources, records, verified['hosts'], run_id, contexts)
    bundle = {'format': FORMAT, 'created_at': created_at,
              'source': {'experiment_id': run_id, 'experiment_path': str(experiment), 'capture_path': str(capture),
                         'experiment_sha256': meta_sha, 'state_export_sha256': state_sha,
                         'capture_manifest_sha256': capture_sha, 'backlog_data_sha': imports[0],
                         'capture_source_method': method},
              'request_plan': plan, 'tasks': selected, 'inherited_host_gates': deepcopy(verified['hosts'])}
    bundle['bundle_hash'] = digest(bundle)
    return bundle


def _validated_build(*args):
    try:
        return _build(*args)
    except (KeyError, TypeError, AttributeError, IndexError, OSError) as error:
        raise ValueError('Invalid or unavailable follow-up source: ' + str(error)) from error


def prepare_followup(experiment, capture, output, task_ids, *, created_at=None):
    """Write followup.json into a NEW directory only after all validation passes.

    Return the same bundle dict as load_followup. Each tasks row contains
    task_id, the unchanged task, unchanged parents, and verified source_records.
    """
    output = guard(Path(output))
    for source in (Path(experiment).resolve(), Path(capture).resolve()):
        if output.is_relative_to(source) or source.is_relative_to(output):
            raise ValueError('Follow-up output must be separate from both source directories')
    if output.exists():
        raise ValueError('Use a new empty follow-up directory')
    settings, _ = _pinned_json(Path(__file__).resolve().parents[1] / 'config/sources.json')
    bundle = _validated_build(experiment, capture, task_ids, iso() if created_at is None else created_at, settings['stores'])
    output.mkdir(parents=True, exist_ok=False)
    write(output / 'followup.json', bundle)
    return bundle


def load_followup(path, config):
    """Rebuild from pinned source paths and compare the entire retained intent."""
    try:
        bundle, _ = _pinned_json(Path(path) / 'followup.json')
        source = bundle['source']
        rebuilt = _validated_build(source['experiment_path'], source['capture_path'],
                                   [row['task_id'] for row in bundle['tasks']], bundle['created_at'], config)
        if bundle != rebuilt:
            raise ValueError('Follow-up bundle or its source files changed')
        return rebuilt
    except (KeyError, TypeError, AttributeError, IndexError, OSError) as error:
        raise ValueError('Invalid or unavailable follow-up bundle: ' + str(error)) from error

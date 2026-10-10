"""Explicit refresh of candidates and all known product dependencies.

Old observations identify URLs to request; only the new capture supplies prices.
The prepared intent pins the complete queue and retains unresolved discovery.
"""
from copy import deepcopy
from dataclasses import asdict, fields
from pathlib import Path
import re

from sale_monitor.models import STORES, iso, timestamp
from .acquire import Receipt
from .capture import verify_capture
from .discovery import kind
from .followup import _ancestor_contexts, _check_gates, _ids, _lineage, _pinned_json
from .queue_study import TraceReceipt
from .queueing import resource_url, task_age
from .request_plan import FORMAT as PLAN_FORMAT, validate_plan
from .safety import digest, guard, write
from .stores import validate

FORMAT = 'pc-sale-monitor-comparison-refresh-v1'


def _product_proof(key, task, contexts):
    for ctx in contexts:
        for identity, dispatch in ctx['records'].get('dispatches', {}).items():
            if (not identity.startswith(ctx['run_id'] + ':')
                    or dispatch.get('state') != 'committed' or dispatch.get('processing_error')):
                continue
            receipt = dispatch.get('receipt', {})
            index = receipt.get('source_receipt_index')
            if type(index) is not int or not 0 <= index < len(ctx['verified']['receipts']):
                continue
            source = ctx['verified']['receipts'][index]
            if (source.get('resource_kind') != 'product' or source['method'] != ctx['method']
                    or source['url'] != task['url'] or source['store'] != task['lab_store']
                    or source['status'] != 200 or source.get('error') or not source.get('attempts')
                    or source.get('body_incomplete') or source.get('body_unavailable')):
                continue
            names = {f.name for f in fields(Receipt)}
            expected = asdict(TraceReceipt(**{k: deepcopy(v) for k, v in source.items() if k in names},
                source_capture_manifest_sha256=ctx['checksum'], source_receipt_index=index,
                source_evidence_mode=source['evidence_mode']))
            expected.update(evidence_mode='captured_queue_simulation', task_ids=dispatch['task_ids'])
            matching = [(k, ctx['records']['tasks'][k]) for k in dispatch['task_ids']
                        if ctx['records']['tasks'][k].get('url') == task['url']
                        and ctx['records']['tasks'][k].get('lab_store') == task['lab_store']
                        and kind(ctx['records']['tasks'][k]) == 'product']
            if not matching:
                continue
            original_key, original = matching[0]
            if (receipt != expected or receipt not in original.get('lab_attempts', [])
                    or dispatch['url'] != task['url'] or original['url'] != task['url']
                    or original['lab_store'] != task['lab_store']
                    or identity != f"{ctx['run_id']}:{dispatch['slot']}"
                    or not re.fullmatch('[0-9a-f]{64}', ctx['transactions'].get('acquire:' + identity, ''))):
                raise ValueError('Product task differs from its authenticated captured dispatch')
            return {'source_task_id': original_key, 'source_task': deepcopy(original),
                    'scope': 'same_product_resource_only_not_alias_completion',
                    'experiment_id': ctx['run_id'], 'experiment_sha256': ctx['meta_sha'],
                    'state_export_sha256': ctx['state_sha'], 'capture_manifest_sha256': ctx['checksum'],
                    'dispatch_id': identity, 'dispatch': deepcopy(dispatch), 'receipt': deepcopy(source)}
    raise ValueError('Product task has no authenticated captured response or discovery')


def _build(experiment, capture, candidate_ids, created_at, config):
    candidate_ids = _ids(candidate_ids, 'candidate IDs (1..20)', maximum=20)
    if not isinstance(created_at, str) or timestamp(created_at) is None:
        raise ValueError('Comparison refresh needs a valid creation time')
    experiment, capture = Path(experiment).resolve(), Path(capture).resolve()
    meta, meta_sha = _pinned_json(experiment / 'experiment.json')
    state, state_sha = _pinned_json(experiment / 'state-export.json')
    manifest, capture_sha = _pinned_json(capture / 'capture-manifest.json')
    verified = verify_capture(capture)
    conditions, run_id = meta['conditions'], meta['experiment_id']
    scope = verified['metadata'].get('scope_plan')
    if (manifest != verified['manifest'] or verified['settings']['stores'] != config or not scope
            or conditions.get('capture_manifest_sha256') != capture_sha
            or conditions.get('capture_plan_hash') != scope['plan_hash']
            or conditions.get('acquisition_mode') != 'captured_queue_simulation'
            or conditions.get('application') not in (None, 'verified_followup')):
        raise ValueError('Refresh source must be a verified scoped collection or follow-up')
    method = conditions['capture_source_method']
    if method not in {r['method'] for r in verified['receipts']}:
        raise ValueError('Source capture method is unavailable')
    validate(state)
    imports = [k[7:] for k in state['transactions'] if k.startswith('import:')]
    if len(imports) != 1 or not re.fullmatch('[0-9a-f]{40}', imports[0]):
        raise ValueError('One pinned backlog data commit is required')
    records, tasks = state['records'], state['records']['tasks']
    contexts = _ancestor_contexts({'run_id': run_id, 'records': records, 'transactions': state['transactions'],
        'verified': verified, 'capture': capture, 'checksum': capture_sha, 'method': method,
        'meta': meta, 'state_sha': state_sha, 'meta_sha': meta_sha}, config)
    selected, candidates = set(candidate_ids), []
    for key in candidate_ids:
        task = tasks[key]
        if kind(task) != 'product' or task.get('lab_role') != 'candidate' or not task.get('lab_identity'):
            raise ValueError('Select product candidates with an existing identity for refresh planning')
        known = {k: deepcopy(row) for k, row in records.get('identity_catalog', {}).items()
                 if row.get('identity') == task['lab_identity'] and row.get('url') != task['url']}
        related = {k: t for k, t in tasks.items() if k != key and (
            task['url'] in t.get('lab_dependencies', []) or
            kind(t) == 'product' and any(t.get('url') == r.get('url') and t.get('lab_store') == r.get('store')
                                       for r in known.values()))}
        products = sorted(k for k, t in related.items() if kind(t) == 'product')
        selected.update(products)
        unresolved = {k: deepcopy(t) for k, t in related.items()
                      if kind(t) in {'search', 'list'} and t.get('lab_status') != 'complete'}
        candidates.append({'task_id': key, 'expected_identity': task['lab_identity'],
                           'product_task_ids': products, 'known_catalog': known,
                           'unresolved_discovery': unresolved})
    selected = _ids(sorted(selected), 'derived product IDs (1..20)', maximum=20)
    rows, resources = [], []
    for key in selected:
        task = tasks[key]
        if (kind(task) != 'product' or resource_url(task) != task.get('url')
                or task.get('lab_status') in {'external_wait', 'waiting'}):
            raise ValueError('Cannot activate a held or unsupported product task')
        task_age(task)
        if task.get('lab_parent_keys'):
            proof = _lineage(key, task, records, run_id, verified, capture, capture_sha,
                             method, config, state['transactions'], contexts)
        else:
            proof = {'task_id': key, 'task': deepcopy(task), 'product_source': _product_proof(key, task, contexts)}
        rows.append(proof)
        resource = {'store': task['lab_store'], 'url': task['url'], 'kind': 'product'}
        if resource not in resources:
            resources.append(resource)
    chosen = {r['store'] for r in resources}
    plan = {'format': PLAN_FORMAT, 'created_at': created_at, 'source_data_sha': scope['source_data_sha'],
            'resources': resources, 'not_requested': [
                {'store': s, 'reason': 'No known product URL selected; unresolved discovery remains held'}
                for s in STORES if s not in chosen]}
    plan['plan_hash'] = digest(plan)
    validate_plan(plan, config)
    _check_gates(resources, records, verified['hosts'], run_id, contexts)
    bundle = {'format': FORMAT, 'created_at': created_at,
        'source': {'experiment_path': str(experiment), 'capture_path': str(capture),
                   'experiment_id': run_id, 'experiment_sha256': meta_sha, 'state_export_sha256': state_sha,
                   'capture_manifest_sha256': capture_sha, 'capture_source_method': method, 'backlog_data_sha': imports[0]},
        'request_plan': plan, 'tasks': rows, 'candidates': candidates,
        'inherited_host_gates': deepcopy(verified['hosts'])}
    bundle['bundle_hash'] = digest(bundle)
    return bundle


def _validated_build(*args):
    try:
        return _build(*args)
    except (KeyError, TypeError, AttributeError, IndexError, OSError) as error:
        raise ValueError('Invalid or unavailable comparison source: ' + str(error)) from error


def prepare_comparison(experiment, capture, output, candidate_ids, *, created_at=None):
    output = guard(Path(output))
    for path in (Path(experiment).resolve(), Path(capture).resolve()):
        if output.is_relative_to(path) or path.is_relative_to(output):
            raise ValueError('Comparison intent must be separate from its sources')
    if output.exists():
        raise ValueError('Use a new comparison intent directory')
    config, _ = _pinned_json(Path(__file__).resolve().parents[1] / 'config/sources.json')
    bundle = _validated_build(experiment, capture, candidate_ids, created_at or iso(), config['stores'])
    write(output / 'comparison.json', bundle)
    return bundle


def load_comparison(path, config):
    try:
        bundle, _ = _pinned_json(Path(path) / 'comparison.json')
        source = bundle['source']
        rebuilt = _validated_build(source['experiment_path'], source['capture_path'],
            [r['task_id'] for r in bundle['candidates']], bundle['created_at'], config)
        if rebuilt != bundle:
            raise ValueError('Comparison bundle or its source files changed')
        return rebuilt
    except (KeyError, TypeError, AttributeError, IndexError, OSError) as error:
        raise ValueError('Invalid or unavailable comparison intent: ' + str(error)) from error

"""Portable binding of a fresh comparison capture to its pinned queue intent."""
from .followup_provenance import _validate, retain_followup, verify_inherited_gates

FORMAT = 'pc-sale-monitor-comparison-provenance-v1'


def retain_comparison(bundle):
    return retain_followup(bundle, format=FORMAT)


def validate_comparison(value, config, scope):
    result = _validate(value, config, scope, FORMAT, 'pc-sale-monitor-comparison-refresh-v1', {'candidates'})
    intent = result['intent']
    try:
        if any(not isinstance(r.get('task_id'), str) or not r['task_id'] or not isinstance(r.get('task'), dict)
               for r in intent['tasks']):
            raise ValueError('Malformed retained product task')
        tasks = {r['task_id']: r['task'] for r in intent['tasks']}
        candidates = intent['candidates']
        if (len(tasks) != len(intent['tasks']) or not isinstance(candidates, list)
                or not 1 <= len(candidates) <= 20 or not all(isinstance(c, dict) for c in candidates)):
            raise ValueError('Duplicate refresh tasks or invalid candidate count')
        if len({r['task_id'] for r in candidates}) != len(candidates):
            raise ValueError('Duplicate refresh candidates')
        resources = {(r['store'], r['url'], r['kind']) for r in scope['resources']}
        if resources != {(t['lab_store'], t['url'], t['lab_kind']) for t in tasks.values()}:
            raise ValueError('Refresh tasks differ from request scope')
        selected = set()
        for row in candidates:
            if (set(row) != {'task_id', 'expected_identity', 'product_task_ids', 'known_catalog', 'unresolved_discovery'}
                    or not isinstance(row['product_task_ids'], list)
                    or any(not isinstance(k, str) or k not in tasks for k in row['product_task_ids'])
                    or len(set(row['product_task_ids'])) != len(row['product_task_ids'])
                    or row['task_id'] in row['product_task_ids']):
                raise ValueError('Invalid retained product dependency IDs')
            candidate = tasks[row['task_id']]
            if (candidate.get('lab_role') != 'candidate' or not row['expected_identity']
                    or candidate.get('lab_identity') != row['expected_identity']
                    or not isinstance(row['unresolved_discovery'], dict)
                    or not isinstance(row['known_catalog'], dict)):
                raise ValueError('Invalid retained comparison candidate')
            for identity, catalog in row['known_catalog'].items():
                if (not isinstance(identity, str) or not isinstance(catalog, dict)
                        or catalog.get('identity') != row['expected_identity']
                        or catalog.get('store') not in config or not isinstance(catalog.get('url'), str)
                        or not catalog['url'] or catalog['url'] == candidate['url']):
                    raise ValueError('Malformed retained known comparator')
            for identity, task in row['unresolved_discovery'].items():
                if (not isinstance(identity, str) or not isinstance(task, dict)
                        or task.get('lab_kind') not in {'search', 'list'}
                        or task.get('lab_status') == 'complete' or task.get('lab_store') not in config
                        or candidate['url'] not in task.get('lab_dependencies', [])):
                    raise ValueError('Malformed retained unresolved discovery')
            selected.update([row['task_id'], *row['product_task_ids']])
        if selected != set(tasks) or any(t['lab_kind'] != 'product' for t in tasks.values()):
            raise ValueError('Comparison refresh must retain every selected product dependency')
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError('Invalid retained comparison scope') from error
    return result

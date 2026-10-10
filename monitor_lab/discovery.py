"""Verified discovery inputs and queue expansion used by the experiment runner.

An input bounds acquisition, not discovery: every discovered dependency is kept,
including URLs for which this experiment has no permitted response fixture.
"""
from copy import deepcopy
import hashlib
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from sale_monitor.models import allowed_url, timestamp
from .acquire import MAX_BODY
from .capture import parser_representation
from .queueing import earliest_age, expand_shared_list, expand_unknown_search, resource_url, task_age, task_age_key
from .safety import digest, read

FORMAT = 'pc-sale-monitor-discovery-input-v1'
KINDS = {'product', 'list', 'search'}


def load_bundle(root, input_hash, config):
    root = Path(root).resolve()
    manifest = read(root / 'manifest.json')
    actual = dict(manifest)
    checksum = actual.pop('input_hash', None)
    if digest(actual) != checksum or manifest.get('format') != FORMAT:
        raise ValueError('Discovery input manifest changed or unsupported')
    if manifest.get('source_input_hash') != input_hash:
        raise ValueError('Discovery input belongs to a different pinned snapshot bundle')
    resources, roots = manifest.get('resources'), manifest.get('roots')
    if not isinstance(resources, list) or not 1 <= len(resources) <= 128 or not isinstance(roots, list) or not 1 <= len(roots) <= 20:
        raise ValueError('Discovery input limit exceeded')
    urls, total = set(), 0
    for row in resources:
        if (row.get('store') not in config or row.get('kind') not in {'home', 'list', 'product'}
                or row.get('evidence_mode') not in {'saved_http', 'fixture'} or not timestamp(row.get('observed_at'))
                or not isinstance(row.get('status'), int) or not 100 <= row['status'] <= 599):
            raise ValueError('Invalid discovery response metadata')
        url = row['url']
        if (not allowed_url(url) or urlsplit(url).scheme != 'https' or urlsplit(url).username or urlsplit(url).password
                or url in urls or urlsplit(url).hostname not in {urlsplit(s).hostname for s in config[row['store']].get('seed_urls', [])}):
            raise ValueError('Invalid or duplicated discovery resource URL')
        urls.add(url)
        relative = row['path']
        portable = PurePosixPath(relative)
        path = (root / relative).resolve()
        if ('\\' in relative or ':' in relative or portable.is_absolute() or '..' in portable.parts
                or portable.as_posix() != relative or not path.is_relative_to(root)):
            raise ValueError('Discovery evidence path escapes its bundle')
        size = path.stat().st_size
        total += size
        if size > MAX_BODY or total > 192 * 1024 * 1024:
            raise ValueError('Discovery evidence exceeds the body limit')
        if hashlib.sha256(path.read_bytes()).hexdigest() != row.get('body_sha256'):
            raise ValueError('Discovery response body changed')
    identities = set()
    by_url = {row['url']: row for row in resources}
    for row in roots:
        if (not isinstance(row.get('id'), str) or not row['id'] or row['id'] in identities
                or row.get('store') not in config or row.get('kind') not in KINDS
                or row.get('role', 'comparison') not in {'candidate', 'comparison', 'discovery'}
                or not timestamp(row.get('created_at'))):
            raise ValueError('Invalid discovery root task')
        identities.add(row['id'])
        if row['kind'] == 'search':
            if not isinstance(row.get('query'), str) or not row['query'].strip():
                raise ValueError('Search roots require a query')
        elif row.get('url') not in urls:
            raise ValueError('Discovery root has no verified response fixture')
        elif (by_url[row['url']]['store'] != row['store'] or
              by_url[row['url']]['kind'] != row['kind']):
            raise ValueError('Discovery root disagrees with its response store or kind')
    return manifest


def kind(task):
    return task.get('lab_kind') or task.get('type', 'product')


def activate_search(task, config, allowed_urls):
    """Attach a home-page resource without replacing original queued fields."""
    cfg = config[task['lab_store']]
    if task['lab_store'] == 'yahoo':
        task.update(lab_status='external_wait', lab_selected=False, lab_reason='yahoo_client_id_unconfigured')
    elif cfg.get('adapter') != 'html' or not cfg.get('seed_urls'):
        task.update(lab_status='external_wait', lab_selected=False, lab_reason='store_adapter_not_integrated_in_lab')
    else:
        home = cfg['seed_urls'][0]
        task['lab_resource_url'] = home
        task['lab_selected'] = home in allowed_urls
        if not task['lab_selected']:
            task.update(lab_status='evidence_wait', lab_reason='home_outside_selected_resources')
        else:
            task.pop('lab_reason', None)
    return task


def merge_child(child, tasks, allowed_urls, parent_keys=(), source=None):
    """Reuse a task without rewriting its original age, attempts, or request."""
    resource = resource_url(child)
    parse_scope = lambda task: (task.get('kind', 'comparison'), task.get('sale_page', False)) if kind(task) == 'list' else None
    matches = [key for key, task in tasks.items() if task.get('lab_store') == child['lab_store']
               and parse_scope(task) == parse_scope(child)
               and kind(task) == kind(child) and (resource_url(task) == resource if kind(child) != 'search'
                                                else task.get('query') == child.get('query'))]
    key = min(matches, key=lambda k: (task_age_key(tasks[k]), k)) if matches else 'discovery:' + digest(
        [child['lab_store'], kind(child), resource, parse_scope(child), child.get('query') if kind(child) == 'search' else None])[:24]
    original = tasks.get(key)
    task = deepcopy(original) if original is not None else {
        field: deepcopy(value) for field, value in child.items()
        if field in {'lab_store', 'url', 'type', 'kind', 'source', 'title', 'expires_at', 'sale_page', 'query',
                     'created_at', 'lab_kind', 'lab_role', 'lab_group', 'lab_resource_url'}}
    task.setdefault('lab_attempts', [])
    task.setdefault('lab_status', 'pending')
    task['lab_kind'] = kind(child)
    task['lab_role'] = 'candidate' if 'candidate' in {task.get('lab_role'), child.get('lab_role')} else child.get('lab_role', 'comparison')
    if resource:
        task['lab_resource_url'] = resource
    task['lab_origin_created_at'] = earliest_age((task_age(task), task_age(child)))
    task['lab_dependencies'] = sorted(set(task.get('lab_dependencies', []) + child.get('lab_dependencies', [])))
    task['lab_parent_keys'] = sorted(set(task.get('lab_parent_keys', []) + list(parent_keys)))
    task['lab_groups'] = sorted(set(task.get('lab_groups', []) + ([child['lab_group']] if child.get('lab_group') else [])))
    task.setdefault('lab_group', child.get('lab_group') or 'discovered:' + digest([child['lab_store'], resource])[:24])
    task['lab_selected'] = resource in allowed_urls
    if not task['lab_selected'] and task['lab_status'] != 'complete':
        task.update(lab_status='evidence_wait', lab_reason='discovered_url_outside_selected_resources')
    if source is not None:
        evidence = {**source, 'discovered': {k: child.get(k) for k in ('title', 'expires_at', 'kind', 'sale_page')}}
        history = task.setdefault('lab_discovery_evidence', [])
        if evidence not in history:
            history.append(evidence)
    return key, task


def root_changes(bundle, tasks, config, allowed_urls):
    changes = []
    for row in bundle['roots']:
        task = {'lab_store': row['store'], 'url': row.get('url', ''), 'created_at': row['created_at'],
                'type': row['kind'], 'lab_kind': row['kind'], 'lab_role': row.get('role', 'comparison'),
                'lab_group': row.get('group') or 'root:' + row['id'], 'kind': 'comparison' if row.get('role', 'comparison') == 'comparison' else 'sale',
                'sale_page': row.get('sale_page', False), 'lab_status': 'pending', 'lab_attempts': [],
                'lab_dependencies': list(row.get('dependencies', []))}
        if row.get('query'):
            task['query'] = row['query']
        if row['kind'] == 'search':
            activate_search(task, config, allowed_urls)
        key = 'discovery-root:' + digest(row['id'])[:24]
        if key in tasks:
            raise ValueError('Discovery root identity already exists')
        task['lab_root_ids'] = [row['id']]
        if row['kind'] != 'search':
            task['lab_selected'] = resource_url(task) in allowed_urls
        changes.append(('tasks', key, task))
    return changes


def inherit_descendants(key, staged):
    """Late parents must reach pending children even after a page was expanded."""
    pending, seen, changed = [key], {}, {}
    while pending:
        parent_key = pending.pop(0)
        parent = staged[parent_key]
        signature = (tuple(parent.get('lab_dependencies', [])), tuple(parent.get('lab_groups', [])), task_age(parent))
        if seen.get(parent_key) == signature:
            continue
        seen[parent_key] = signature
        for child_key in parent.get('lab_child_keys', []):
            if child_key not in staged:
                raise ValueError('Persisted discovery dependency is missing')
            child = deepcopy(staged[child_key])
            child['lab_dependencies'] = sorted(set(child.get('lab_dependencies', []) + parent.get('lab_dependencies', [])))
            child['lab_groups'] = sorted(set(child.get('lab_groups', []) + parent.get('lab_groups', [])))
            child['lab_origin_created_at'] = earliest_age((task_age(child), task_age(parent)))
            if child != staged[child_key]:
                staged[child_key] = child
                changed[child_key] = child
                pending.append(child_key)
    return [('tasks', child_key, value) for child_key, value in changed.items()]


class Dispatcher:
    def __init__(self, config, allowed_urls):
        self.config, self.allowed_urls, self.cache = config, set(allowed_urls), {}

    def expand(self, task, page, receipt, tasks):
        scope = (task['lab_store'], kind(task), task.get('kind', 'comparison'), task.get('sale_page', False))
        shared = [tasks[key] for key in receipt['task_ids'] if
                  (tasks[key]['lab_store'], kind(tasks[key]), tasks[key].get('kind', 'comparison'), tasks[key].get('sale_page', False)) == scope]
        parent_keys = [key for key in receipt['task_ids'] if tasks[key] in shared]
        representation = parser_representation(receipt)
        source = {'url': page.url, 'observed_at': page.observed_at, 'http_status': page.status,
                  'body_sha256': representation.get('body_sha256'), 'body_kind': representation.get('body_kind', 'http_response'),
                  'evidence_mode': receipt.get('evidence_mode')}
        source.update({key: receipt[key] for key in ('method', 'source_capture_manifest_sha256',
                      'source_receipt_index', 'source_evidence_mode', 'analysis_reuse', 'derived_from_dispatch')
                       if key in receipt})
        if kind(task) == 'search':
            # One home response can serve different queries, so each query has
            # its own child even though the network resource is shared.
            child = expand_unknown_search(task, page, self.config[task['lab_store']])
            child.update(lab_resource_url=child['url'], created_at=task_age(task), lab_role='comparison')
            children, result = [child], {'result': 'search_form_verified', 'query': task['query']}
            parent_keys = [key for key in receipt['task_ids'] if tasks[key].get('query') == task['query']]
        elif kind(task) == 'list':
            cache_key = (page.url, representation.get('body_sha256'), page.observed_at, scope,
                         tuple(parent_keys), tuple(task_age(t) for t in shared))
            if cache_key not in self.cache:
                self.cache[cache_key] = expand_shared_list(shared, page, self.config[task['lab_store']])
            children, detail = deepcopy(self.cache[cache_key])
            result = {'result': 'confirmed_empty' if detail.get('confirmed_empty') else 'dependencies_discovered', **detail}
        else:
            raise ValueError('Unsupported discovery task')
        changes, keys, staged = [], [], dict(tasks)
        for child in children:
            key, value = merge_child(child, staged, self.allowed_urls, parent_keys, source)
            # Pagination cycles refer back to the current task; its original
            # fields and the scheduler's completion must win over a fresh copy.
            if key in receipt['task_ids']:
                keys.append(key)
                continue
            changes.append(('tasks', key, value))
            staged[key] = value
            changes.extend(inherit_descendants(key, staged))
            keys.append(key)
        task['lab_child_keys'] = sorted(set(task.get('lab_child_keys', []) + keys))
        task['lab_resolution'] = {**source, **result, 'child_count': len(set(keys)),
                                  'unavailable_children': sum(staged[k].get('lab_status') == 'evidence_wait' for k in set(keys))}
        return changes

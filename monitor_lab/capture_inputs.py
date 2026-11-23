"""Read-only, scoped capture inputs for the experiment runner."""
from copy import deepcopy
import hashlib
from pathlib import Path

from sale_monitor.models import timestamp
from .acquire import TRANSPORTS
from .capture import verify_capture
from .safety import digest


def load_capture_inputs(capture, method, primary_pages, config, candidate_urls=None):
    """Retain every planned outcome; primary pages supply only names and groups.

    Receipt indices address the complete verified receipt list, not the selected
    method's subset. Body metadata describes that receipt's response; browser
    parser representations remain available in ``verified`` for the caller.
    No transport is constructed and no response is parsed or written here.
    """
    if not isinstance(method, str) or method not in TRANSPORTS:
        raise ValueError('Unknown capture method; use urllib, pooled or browser')
    candidates = [] if candidate_urls is None else candidate_urls
    if not isinstance(candidates, list) or any(not isinstance(url, str) for url in candidates):
        raise ValueError('candidate_urls must be a list of product URL strings')
    if len(set(candidates)) != len(candidates):
        raise ValueError('Duplicate candidate URLs')
    if not isinstance(primary_pages, list) or any(not isinstance(page, dict) for page in primary_pages):
        raise ValueError('primary_pages must be a list of page descriptors')

    capture = Path(capture)
    manifest_path = capture / 'capture-manifest.json'
    manifest_bytes = manifest_path.read_bytes()
    verified = verify_capture(capture)
    if manifest_path.read_bytes() != manifest_bytes:
        raise ValueError('Capture manifest changed during input verification')
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    metadata = verified['metadata']
    plan = metadata.get('scope_plan')
    if plan is None:
        raise ValueError('Capture inputs require a verified scope_plan')
    if not isinstance(config, dict) or config != verified['settings'].get('stores'):
        raise ValueError('Capture store configuration differs from retained settings')
    if method not in metadata['methods']:
        raise ValueError('Selected capture method was not requested in metadata.methods')
    source_method = TRANSPORTS[method].method
    products = {row['url'] for row in plan['resources'] if row['kind'] == 'product'}
    if set(candidates) - products:
        raise ValueError('Candidate URLs must be planned product resources')

    selected = [(index, row) for index, row in enumerate(verified['receipts'])
                if row['method'] == source_method]
    times = []
    for _, row in selected:
        original = row.get('observed_at')
        parsed = timestamp(original) if isinstance(original, str) else None
        if parsed is None:
            raise ValueError('Every selected capture receipt requires a valid observed_at timestamp')
        times.append((parsed, original))
    if not times:
        raise ValueError('Selected capture method has no observed_at timestamps')
    observed_at = max(times, key=lambda item: item[0])[1]

    resources, pages, roots, root_holds = {}, [], [], {}
    for index, row in selected:
        store, url, kind = row['store'], row['url'], row['resource_kind']
        resource = {'store': store, 'url': url, 'kind': kind,
                    **{field: row.get(field) for field in
                       ('observed_at', 'content_type', 'status', 'body_sha256')},
                    'source_receipt_index': index}
        resources[url] = resource
        root_id = 'capture-root:' + digest([manifest_sha256, store, url])[:24]
        root = {'id': root_id, 'store': store, 'url': url,
                'kind': 'product' if kind == 'product' else 'list',
                'role': 'candidate' if url in candidates else 'comparison' if kind == 'product' else 'discovery',
                'group': root_id, 'created_at': plan['created_at'], 'sale_page': False}
        if kind == 'product':
            primary = next((page for page in primary_pages
                            if page.get('store') == store and page.get('url') == url), {})
            fallback = 'captured:' + digest([store, url])[:24]
            page = {**resource, 'name': deepcopy(primary.get('name', fallback)),
                    'group': deepcopy(primary.get('group', fallback))}
            pages.append(page)
            root['group'] = deepcopy(page['group'])
        elif config[store].get('adapter') != 'html':
            root_holds[root_id] = 'store_adapter_not_integrated_in_lab'
        roots.append(root)

    root_bundle = {'input_hash': digest({'manifest_sha256': manifest_sha256,
                                         'source_method': source_method, 'roots': roots}),
                   'roots': roots}
    return {'verified': verified, 'manifest_sha256': manifest_sha256,
            'source_method': source_method, 'plan_hash': plan['plan_hash'],
            'resources': resources, 'pages': pages, 'roots': root_bundle,
            'root_holds': root_holds, 'observed_at': observed_at,
            'candidate_urls': sorted(candidates)}

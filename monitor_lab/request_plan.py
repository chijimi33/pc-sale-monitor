"""Explicit, bounded URL plans are acquisition intent, never price evidence."""
from copy import deepcopy
from pathlib import Path
import re
from urllib.parse import urlsplit

from sale_monitor.models import STORES, allowed_url, timestamp
from .safety import digest, read

FORMAT = 'pc-sale-monitor-request-plan-v1'
KINDS = {'home', 'list', 'product'}
MAX_RESOURCES = 20


def validate_plan(plan, config):
    """Validate before transport construction; keep all ten stores explicit."""
    if not isinstance(plan, dict) or set(plan) != {
            'format', 'created_at', 'source_data_sha', 'resources', 'not_requested', 'plan_hash'}:
        raise ValueError('Invalid request plan fields')
    unsigned = {key: value for key, value in plan.items() if key != 'plan_hash'}
    if plan['format'] != FORMAT or digest(unsigned) != plan['plan_hash']:
        raise ValueError('Request plan checksum or format mismatch')
    if (not isinstance(plan['created_at'], str) or timestamp(plan['created_at']) is None
            or not isinstance(plan['source_data_sha'], str)
            or re.fullmatch('[0-9a-f]{40}', plan['source_data_sha']) is None):
        raise ValueError('Request plan needs an explicit time and pinned source commit')
    if not isinstance(config, dict) or not set(STORES) <= set(config):
        raise ValueError('Request plan requires the fixed ten-store configuration')
    resources, omitted = plan['resources'], plan['not_requested']
    if not isinstance(resources, list) or not 1 <= len(resources) <= MAX_RESOURCES or not isinstance(omitted, list):
        raise ValueError('Request plan resource limit exceeded')
    seen, selected = set(), set()
    for row in resources:
        if (not isinstance(row, dict) or set(row) != {'store', 'url', 'kind'}
                or not isinstance(row['store'], str) or row['store'] not in STORES
                or not isinstance(row['kind'], str) or row['kind'] not in KINDS
                or not isinstance(row['url'], str)):
            raise ValueError('Invalid request plan resource')
        url = row['url']
        try:
            parts = urlsplit(url)
            permitted_hosts = {urlsplit(seed).hostname for seed in config[row['store']].get('seed_urls', [])}
            valid = (allowed_url(url) and parts.scheme == 'https' and parts.hostname in permitted_hosts
                     and parts.port in (None, 443) and not parts.username and not parts.password
                     and not parts.fragment and not re.search(r'\s|[\x00-\x1f\x7f]', url))
            identity = (parts.hostname, parts.path or '/', parts.query)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError('Request URL is outside the configured store or uses unsupported credentials/port')
        if identity in seen:
            raise ValueError('Duplicate request resource')
        seen.add(identity)
        selected.add(row['store'])
    unselected = set()
    for row in omitted:
        if (not isinstance(row, dict) or set(row) != {'store', 'reason'}
                or not isinstance(row['store'], str) or row['store'] not in STORES or row['store'] in unselected
                or row['store'] in selected or not isinstance(row['reason'], str)
                or not row['reason'].strip() or len(row['reason']) > 1000):
            raise ValueError('Invalid or overlapping not-requested store')
        unselected.add(row['store'])
    if selected | unselected != set(STORES):
        raise ValueError('Every monitored store must be selected or explicitly not requested')
    return deepcopy(plan)


def load_plan(path, config):
    return validate_plan(read(Path(path)), config)


def scope_report(plan, receipts):
    """Count requests/responses separately from unique URLs and product parsing."""
    reasons = {row['store']: row['reason'] for row in plan['not_requested']}
    stores = {}
    for store in STORES:
        resources = [row for row in plan['resources'] if row['store'] == store]
        rows = [row for row in receipts if row['store'] == store]
        successful = [row for row in rows if row['status'] == 200 and not row.get('error')]
        stores[store] = {
            'permitted_resources': {kind: sorted(row['url'] for row in resources if row['kind'] == kind)
                                    for kind in sorted(KINDS)},
            'confirmed_http_attempts': sum(len(row.get('attempts', [])) for row in rows),
            'successful_responses': len(successful),
            'successful_product_responses': sum(row['resource_kind'] == 'product' for row in successful),
            'successful_product_urls': sorted({row['url'] for row in successful if row['resource_kind'] == 'product'}),
            'not_requested_reason': reasons.get(store)}
    return {'schema': 1, 'plan_hash': plan['plan_hash'], 'monitored_store_count': len(STORES),
            'excluded_stores': ['rakuten'], 'full_store_coverage_proven': False, 'stores': stores,
            'limits': ['A successful HTTP response is not proof of product identity, fields or price eligibility.',
                       'Multiple methods can yield multiple responses for one URL; unique URLs are counted separately.',
                       'A URL plan labels intended resource kinds; it does not authenticate page content.',
                       'Unrequested stores remain in the denominator; no store catalog is claimed complete.']}

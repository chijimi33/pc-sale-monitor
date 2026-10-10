"""Portable follow-up intent retained with a new capture, not a price claim."""
from copy import deepcopy
import math
import re
from urllib.parse import urlsplit

from .acquire import TRANSPORTS
from .request_plan import validate_plan
from .safety import digest

FORMAT = 'pc-sale-monitor-followup-provenance-v1'


def retain_followup(bundle, *, format=FORMAT):
    retained = deepcopy(bundle)
    checksum = retained.pop('bundle_hash')
    retained['source'].pop('experiment_path')
    retained['source'].pop('capture_path')
    unsigned = {'format': format, 'source_bundle_hash': checksum, 'intent': retained}
    return {**unsigned, 'provenance_hash': digest(unsigned)}


def validate_provenance(value, config, scope):
    return _validate(value, config, scope, FORMAT, 'pc-sale-monitor-followup-v1')


def _validate(value, config, scope, format, intent_format, extra_fields=frozenset()):
    if not isinstance(value, dict) or set(value) != {'format', 'source_bundle_hash', 'intent', 'provenance_hash'}:
        raise ValueError('Invalid retained follow-up provenance')
    unsigned = {k: v for k, v in value.items() if k != 'provenance_hash'}
    hex_digest = lambda item: isinstance(item, str) and re.fullmatch('[0-9a-f]{64}', item)
    if value['format'] != format or digest(unsigned) != value['provenance_hash'] or not hex_digest(value['source_bundle_hash']):
        raise ValueError('Follow-up provenance checksum mismatch')
    intent = value['intent']
    if (not isinstance(intent, dict) or set(intent) != {'format', 'created_at', 'source', 'request_plan', 'tasks', 'inherited_host_gates'} | set(extra_fields)
            or intent['format'] != intent_format
            or not isinstance(intent['tasks'], list) or not 1 <= len(intent['tasks']) <= 20
            or not all(isinstance(t, dict) for t in intent['tasks'])):
        raise ValueError('Invalid retained follow-up intent')
    source = intent['source']
    if (not isinstance(source, dict) or set(source) != {'experiment_id', 'experiment_sha256', 'state_export_sha256',
            'capture_manifest_sha256', 'backlog_data_sha', 'capture_source_method'}
            or not isinstance(source['experiment_id'], str) or not source['experiment_id'].startswith('lab-')
            or not all(hex_digest(source[k]) for k in ('experiment_sha256', 'state_export_sha256', 'capture_manifest_sha256'))
            or not isinstance(source['backlog_data_sha'], str) or not re.fullmatch('[0-9a-f]{40}', source['backlog_data_sha'])
            or source['capture_source_method'] not in {transport.method for transport in TRANSPORTS.values()}):
        raise ValueError('Invalid follow-up source identity')
    plan = validate_plan(intent['request_plan'], config)
    if plan != scope or intent['created_at'] != plan['created_at']:
        raise ValueError('Follow-up request scope differs from retained intent')
    gates = intent['inherited_host_gates']
    allowed_hosts = {urlsplit(url).hostname for cfg in config.values() for url in cfg.get('seed_urls', [])}
    if not isinstance(gates, dict) or set(gates) - allowed_hosts:
        raise ValueError('Follow-up host gates contain an unconfigured host')
    for host, gate in gates.items():
        if (not isinstance(gate, dict) or not isinstance(gate.get('reason'), str)
                or not gate['reason'] or 'simulation_translation' in gate or 'source_until' in gate
                or ('blocked' in gate and type(gate['blocked']) is not bool)
                or ('until' in gate and (type(gate['until']) not in (int, float)
                    or not math.isfinite(gate['until']) or gate['until'] < 0))
                or not (gate.get('blocked') or 'until' in gate)):
            raise ValueError('Invalid original follow-up host gate')
        if gate.get('source_url') and urlsplit(gate['source_url']).hostname != host:
            raise ValueError('Follow-up host gate source disagrees')
    return deepcopy(value)


def verify_inherited_gates(value, receipts):
    """A new study must not erase a retained block or start before its wait."""
    gates = value['intent']['inherited_host_gates']
    for receipt in receipts:
        gate = gates.get(urlsplit(receipt['url']).hostname, {})
        for attempt in receipt.get('attempts', []):
            if (gate.get('blocked') or type(attempt.get('started_at')) not in (int, float)
                    or not math.isfinite(attempt['started_at']) or attempt['started_at'] < gate.get('until', 0)):
                raise ValueError('Follow-up attempted acquisition inside an inherited host wait')

"""One post-collection comparison on the same runner; never an offer source."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re
import shutil
import subprocess
import time

from .http_evidence import retry_after_evidence
from .models import iso


MARKER = b'\nPCSM_DIAGNOSTIC:'
HEADERS = {'User-Agent': 'PCSaleMonitor/0.1 (+https://github.com/chijimi33/pc-sale-monitor)',
           'Accept-Language': 'ja,en;q=0.5', 'Cache-Control': 'no-cache'}


def split_headers(data):
    headers = {}
    while re.match(rb'HTTP/\S+ \d{3}(?: |\r?\n)', data):
        block, separator, body = data.partition(b'\r\n\r\n')
        if not separator:
            raise ValueError('incomplete_headers')
        headers = {}
        for line in block.split(b'\r\n')[1:]:
            name, sep, value = line.partition(b':')
            if sep:
                headers[name.decode('ascii', 'ignore').lower()] = value.strip().decode('latin-1')
        data = body
    return headers, data


def compare_after_collection(state, before_request=None):
    """Return independent evidence without changing state or accepting its body."""
    run_id = state.get('run_id')
    prior = state.get('transport_diagnostic')
    if prior and prior.get('run_id') == run_id:
        return deepcopy(prior)
    result = {'run_id': run_id, 'checked_at': iso(), 'method': 'system_curl_default',
              'scope': 'same_runner_after_collection; not_price_observation; redirects_not_followed',
              'diagnostic_request_count': 0, 'status': 'skipped'}
    if state.get('store') != 'tsukumo':
        return {**result, 'reason': 'unsupported_store'}
    evidence = state.get('http_evidence') or {}
    if evidence.get('run_id') != run_id or state.get('status') == 'job_missing':
        return {**result, 'reason': 'no_current_transport_evidence'}
    if state.get('access_block') or any(t > time.time() for t in state.get('retry_after', {}).values()):
        return {**result, 'reason': 'access_block_or_server_wait'}
    # Only the latest recorded failure qualifies. A completed task or a stale
    # failure must not be used as the baseline for a new transport comparison.
    samples = evidence.get('failure_samples', [])
    if not samples or samples[-1].get('method') != 'http' or samples[-1].get('exception_type') != 'RemoteDisconnected':
        return {**result, 'reason': 'last_failure_not_remote_disconnected'}
    baseline = samples[-1]
    selected = None
    for task_id, task in state.get('queue', {}).items():
        url = task.get('url', '')
        if (task.get('type') not in ('product', 'list') or task.get('last_attempt_run_id') != run_id or task.get('last_error') != 'RemoteDisconnected'
                or hashlib.sha256(url.encode()).hexdigest() != baseline.get('url_sha256')):
            continue
        # No queries, credentials, custom ports, POST bodies or external hosts.
        if not re.fullmatch(r'https://shop\.tsukumo\.co\.jp/(?:goods/\d+/?|features/[A-Za-z0-9_-]+/?)?', url):
            continue
        selected = (task_id, url)
        break
    if selected is None:
        return {**result, 'reason': 'no_matching_safe_pending_url'}
    result.update(url=selected[1], task_id=selected[0], baseline=deepcopy(baseline))
    curl = shutil.which('curl.exe') or shutil.which('curl')
    if not curl:
        return {**result, 'reason': 'curl_unavailable'}
    args = [curl, '--disable', '--silent', '--show-error', '--dump-header', '-',
            '--proto', '=https', '--connect-timeout', '10', '--max-time', '25',
            '--max-filesize', '2097152', '--write-out', '\nPCSM_DIAGNOSTIC:%{http_code}\t%{http_version}']
    for name, value in HEADERS.items():
        args += ['--header', name + ': ' + value]
    result['request_started_at'] = iso()
    if before_request is not None:
        before_request({**result, 'status': 'started', 'diagnostic_request_count': None})
    started = time.monotonic()
    try:
        completed = subprocess.run(args + ['--url', selected[1]], capture_output=True, timeout=30)
        result.update(status='completed', diagnostic_request_count=1, exit_code=completed.returncode)
        if MARKER in completed.stdout:
            response, metadata = completed.stdout.rsplit(MARKER, 1)
            code, version = metadata.decode('ascii').split('\t', 1)
            headers, body = split_headers(response)
            status = int(code)
            result.update(http_status=status if 100 <= status <= 599 else None,
                          http_version=version, bytes=len(body), body_sha256=hashlib.sha256(body).hexdigest(),
                          retry_after=retry_after_evidence(headers.get('retry-after')))
        else:
            result.update(status='diagnostic_error', reason='missing_response_metadata')
    except subprocess.TimeoutExpired:
        result.update(status='diagnostic_error', diagnostic_request_count=1, reason='diagnostic_timeout')
    except OSError:
        result.update(status='diagnostic_error', reason='diagnostic_launch_failed')
    except (ValueError, UnicodeError):
        result.update(status='diagnostic_error', reason='invalid_response_metadata')
    result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    return result


def save_after_collection(collector):
    def reserve(record):
        # If interrupted during curl, do not issue it again on a same-run
        # checkpoint restart. The request count stays unknown until completion.
        collector.state['transport_diagnostic'] = record
        collector.save()
    result = compare_after_collection(collector.state, reserve)
    retry = result.get('retry_after', {})
    status = result.get('http_status')
    until = None
    if status in (429, 500, 502, 503, 504) and 'applied_server_retry_at_epoch_seconds' not in result:
        if retry.get('kind') == 'seconds':
            until = time.time() + max(0, retry['seconds'])
        elif retry.get('kind') == 'http_date':
            until = datetime.fromisoformat(retry['at']).timestamp()
        if status == 429 and (until is None or until <= time.time()):
            until = time.time() + 300
    if until is not None and until > time.time():
        host = 'shop.tsukumo.co.jp'
        collector.client.retry_after[host] = max(collector.client.retry_after.get(host, 0), until)
        result['applied_server_retry_at_epoch_seconds'] = collector.client.retry_after[host]
    collector.state['transport_diagnostic'] = result
    collector.save()
    print('PCSM_TRANSPORT_DIAGNOSTIC ' + json.dumps(result, ensure_ascii=False), flush=True)
    return result

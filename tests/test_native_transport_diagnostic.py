from copy import deepcopy
from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sale_monitor.cli import main
from sale_monitor.http import Client
from sale_monitor.models import Offer
from sale_monitor.reporting import aggregate
from sale_monitor.runner import Collector
from sale_monitor.storage import Store
from sale_monitor.transport_diagnostic import compare_after_collection, save_after_collection, split_headers


URL = 'https://shop.tsukumo.co.jp/goods/4957180130910/'
NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)
CFG = {'stores': {'tsukumo': {'adapter': 'html', 'seed_urls': []}}}


def state(url=URL):
    old = Offer(store='tsukumo', product_id='old', url=URL, price_yen=12345,
                observed_at='2026-10-10T00:00:00+00:00', observed_run_id='old').to_dict()
    return {'store': 'tsukumo', 'run_id': 'r1', 'status': 'partial', 'request_count': 63,
            'offers': {'old':old}, 'journal': [{'observation_id':'old', 'offer':old}], 'done': [], 'cycle_complete': False,
            'queue': {'task': {'type': 'product', 'url': url, 'created_at': '2026-10-01T00:00:00+00:00',
                               'attempts': 7, 'last_attempt_run_id': 'r1', 'last_error': 'RemoteDisconnected'}},
            'errors': [{'url': url, 'reason': 'RemoteDisconnected'}],
            'http_evidence': {'run_id': 'r1', 'failure_samples': [{'observed_at': '2026-10-11T00:00:00+00:00',
                              'method': 'http', 'exception_type': 'RemoteDisconnected',
                              'url_sha256': hashlib.sha256(url.encode()).hexdigest()}]}}


def completed(code=200, body=b'<h1>not an offer</h1>', header=b''):
    return SimpleNamespace(returncode=0, stdout=b'HTTP/1.1 '+str(code).encode()+b' Status\r\n'+header+b'\r\n'+body+
                           b'\nPCSM_DIAGNOSTIC:'+str(code).encode()+b'\t1.1', stderr=b'private')


class NativeTransportDiagnosticTests(unittest.TestCase):
    def test_same_url_headers_no_redirect_retry_or_curlrc_and_state_is_unchanged(self):
        original = state(); before = deepcopy(original)
        with patch('sale_monitor.transport_diagnostic.shutil.which', return_value='/usr/bin/curl'), \
             patch('sale_monitor.transport_diagnostic.subprocess.run', return_value=completed()) as run:
            result = compare_after_collection(original)
        self.assertEqual(original, before)
        self.assertEqual(result['diagnostic_request_count'], 1)
        self.assertEqual(result['http_status'], 200)
        self.assertNotIn('offers', result)
        self.assertNotIn('private', json.dumps(result))
        args = run.call_args.args[0]
        self.assertEqual(args[1], '--disable')
        self.assertEqual(args[-2:], ['--url', URL])
        for forbidden in ['--location', '-L', '--retry', '--insecure', '-k']:
            self.assertNotIn(forbidden, args)
        self.assertIn('--max-filesize', args)
        self.assertEqual(run.call_args.kwargs['timeout'], 30)
        self.assertIn('User-Agent: PCSaleMonitor/0.1 (+https://github.com/chijimi33/pc-sale-monitor)', args)

    def test_no_probe_after_denial_stale_data_server_wait_or_completed_task(self):
        cases = []
        v=state(); v['store']='ark'; cases.append(v)
        v=state(); v['http_evidence']['run_id']='old'; cases.append(v)
        v=state(); v['status']='job_missing'; cases.append(v)
        v=state(); v['http_evidence']['failure_samples'][-1]['method']='browser'; cases.append(v)
        v=state(); v['access_block']={'reason':'http_403'}; cases.append(v)
        v=state(); v['retry_after']={'shop.tsukumo.co.jp':9999999999}; cases.append(v)
        v=state(); v['http_evidence']['failure_samples'][-1]={'status':429}; cases.append(v)
        v=state(); v['queue']['task']['last_attempt_run_id']='old'; cases.append(v)
        v=state(); v['queue']['task']['last_error']='http_403'; cases.append(v)
        v=state(); v['queue']['task']['type']='search'; cases.append(v)
        v=state(); v['queue']={}; cases.append(v)
        with patch('sale_monitor.transport_diagnostic.subprocess.run') as run:
            for v in cases:
                self.assertEqual(compare_after_collection(v)['diagnostic_request_count'], 0)
        run.assert_not_called()

    def test_only_canonical_public_get_urls_are_allowed(self):
        with patch('sale_monitor.transport_diagnostic.subprocess.run') as run:
            for url in [URL+'?secret=x', URL+'#secret', 'https://shop.tsukumo.co.jp.evil/goods/1/',
                        'https://user:password@shop.tsukumo.co.jp/goods/1/',
                        'http://shop.tsukumo.co.jp/goods/1/', 'https://shop.tsukumo.co.jp:443/goods/1/']:
                self.assertEqual(compare_after_collection(state(url))['reason'], 'no_matching_safe_pending_url')
        run.assert_not_called()

    def test_denial_is_recorded_once_without_followup_and_header_secrets_are_omitted(self):
        for code in [302, 403, 429, 503]:
            with patch('sale_monitor.transport_diagnostic.shutil.which', return_value='curl'), \
                 patch('sale_monitor.transport_diagnostic.subprocess.run', return_value=completed(code, header=b'Retry-After: 900\r\nSet-Cookie: private\r\n')) as run:
                result=compare_after_collection(state())
            run.assert_called_once()
            self.assertEqual(result['http_status'],code)
            self.assertEqual(result['retry_after'], {'kind':'seconds','seconds':900})
            self.assertNotIn('private',json.dumps(result))

    def test_missing_curl_timeout_launch_error_and_malformed_output_preserve_failure(self):
        with patch('sale_monitor.transport_diagnostic.shutil.which', return_value=None):
            self.assertEqual(compare_after_collection(state())['reason'],'curl_unavailable')
        cases=[(subprocess.TimeoutExpired(['curl'],30),1,'diagnostic_timeout'),
               (OSError('private'),0,'diagnostic_launch_failed')]
        for error,count,reason in cases:
            with patch('sale_monitor.transport_diagnostic.shutil.which', return_value='curl'), \
                 patch('sale_monitor.transport_diagnostic.subprocess.run',side_effect=error):
                result=compare_after_collection(state())
            self.assertEqual((result['diagnostic_request_count'],result['reason']),(count,reason))
        with patch('sale_monitor.transport_diagnostic.shutil.which', return_value='curl'), \
             patch('sale_monitor.transport_diagnostic.subprocess.run',return_value=SimpleNamespace(returncode=7,stdout=b'bad',stderr=b'private')):
            self.assertEqual(compare_after_collection(state())['reason'],'missing_response_metadata')

    def test_connect_headers_are_removed_before_body_hash(self):
        headers, body=split_headers(b'HTTP/1.1 200 Connection established\r\n\r\nHTTP/2 429 Slow\r\nRetry-After: 900\r\n\r\nlimited')
        self.assertEqual(body,b'limited')
        self.assertEqual(headers,{'retry-after':'900'})

    def test_checkpoint_reservation_prevents_duplicate_request_after_interruption(self):
        original=state(); saved=[]
        def reserve(record):
            saved.append(deepcopy(record))
        with patch('sale_monitor.transport_diagnostic.shutil.which', return_value='curl'), \
             patch('sale_monitor.transport_diagnostic.subprocess.run',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):compare_after_collection(original,reserve)
        self.assertEqual(saved[0]['status'],'started')
        self.assertIsNone(saved[0]['diagnostic_request_count'])
        original['transport_diagnostic']=saved[0]
        with patch('sale_monitor.transport_diagnostic.subprocess.run') as run:
            self.assertEqual(compare_after_collection(original),saved[0])
        run.assert_not_called()

    def test_successful_diagnostic_does_not_complete_queue_or_publish_current_price(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); disk=Store(root/'state'); disk.save('stores/tsukumo.json',state())
            client=Client(); client.count=63
            collector=Collector(root/'state','tsukumo',CFG,'r1',client)
            collector.state['http_evidence']=state()['http_evidence']
            before=deepcopy(collector.state)
            with patch('sale_monitor.transport_diagnostic.shutil.which',return_value='curl'), \
                 patch('sale_monitor.transport_diagnostic.subprocess.run',return_value=completed()) as run, redirect_stdout(StringIO()):
                save_after_collection(collector)
                save_after_collection(collector)
            run.assert_called_once()
            for key in ['queue','offers','journal','errors']:
                self.assertEqual(collector.state[key],before[key])
            self.assertEqual(client.count,63)
            result=aggregate(root/'state',root/'public','r1',NOW)
            self.assertEqual(result['stores']['tsukumo']['current_offers'],0)
            self.assertEqual(result['stores']['tsukumo']['pending_count'],1)
            self.assertEqual(result['stores']['tsukumo']['diagnostic_request_count'],1)
            details=json.loads((root/'public/collection_errors.json').read_text())
            self.assertEqual(details['stores']['tsukumo']['transport_diagnostic']['http_status'],200)
            stale=aggregate(root/'state',root/'public','r2',NOW)
            self.assertIsNone(stale['stores']['tsukumo']['diagnostic_request_count'])

    def test_server_wait_from_diagnostic_is_preserved_for_next_collection(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); disk=Store(root); disk.save('stores/tsukumo.json',state())
            client=Client(); collector=Collector(root,'tsukumo',CFG,'r1',client)
            collector.state['http_evidence']=state()['http_evidence']
            with patch('sale_monitor.transport_diagnostic.shutil.which',return_value='curl'), \
                 patch('sale_monitor.transport_diagnostic.subprocess.run',return_value=completed(429,header=b'Retry-After: 900\r\n')), \
                 patch('sale_monitor.transport_diagnostic.time.time',return_value=1000), redirect_stdout(StringIO()):
                save_after_collection(collector)
                self.assertEqual(disk.load('stores/tsukumo.json',{})['retry_after']['shop.tsukumo.co.jp'],1900)
            with patch('sale_monitor.transport_diagnostic.time.time',return_value=1100), redirect_stdout(StringIO()):
                save_after_collection(collector)
                self.assertEqual(disk.load('stores/tsukumo.json',{})['retry_after']['shop.tsukumo.co.jp'],1900)

    def test_cli_rejects_unrelated_commands(self):
        for argv in [['collect','--store','ark'], ['aggregate','--store','tsukumo']]:
            with patch('sys.stderr',new=StringIO()), self.assertRaises(SystemExit):
                main(argv+['--transport-diagnostic'])


if __name__ == '__main__':
    unittest.main()

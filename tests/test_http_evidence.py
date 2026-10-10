from contextlib import ExitStack
from datetime import datetime, timezone
from http.client import RemoteDisconnected
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from sale_monitor.http import Client, FetchError
from sale_monitor.http_evidence import ResponseEvidence, retry_after_evidence, summary
from sale_monitor.reporting import aggregate
from sale_monitor.runner import Collector
from sale_monitor.storage import Store


URL = 'https://www.pc-koubou.jp/products/detail.php?product_id=1&secret=private'
NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
CFG = {'stores': {'ark': {'adapter': 'html', 'seed_urls': []}}}


class HttpEvidenceTests(unittest.TestCase):
    def test_unknown_response_is_not_declared_an_http_failure(self):
        evidence = ResponseEvidence()
        evidence.record(URL, 'browser')
        snapshot = evidence.snapshot()
        self.assertEqual(snapshot['response_status_counts']['browser'], {'unknown': 1})
        self.assertEqual(snapshot['failure_observations'], 0)

    def test_server_wait_records_actual_status_and_zero_extra_requests(self):
        client = Client(delay=0, browser=True)
        with patch('sale_monitor.http.time.time', return_value=1000), patch.object(client.opener, 'open',
                side_effect=HTTPError(URL, 503, 'private message', {'Retry-After': '900', 'Set-Cookie': 'secret'}, None)) as op:
            with self.assertRaises(FetchError): client.get(URL)
            with self.assertRaises(FetchError): client.get(URL + 'other')
            with self.assertRaises(FetchError): client.rendered(URL)
        data = client.evidence.snapshot()
        self.assertEqual(op.call_count, 1)
        self.assertEqual(client.count, 1)
        self.assertEqual(data['response_status_counts']['http'], {'503': 1})
        self.assertEqual(data['failure_observations'], 1)
        sample = data['failure_samples'][0]
        self.assertEqual(sample['retry_after'], {'kind': 'seconds', 'seconds': 900})
        self.assertEqual(sample['host_retry_at_epoch_seconds'], 1900)
        self.assertNotIn('secret', json.dumps(data))
        self.assertNotIn('private', json.dumps(data))

    def test_mixed_retries_and_success_keep_individual_responses(self):
        client = Client(delay=0)
        response = MagicMock(); response.__enter__.return_value = response
        response.url = URL; response.status = 200; response.headers = {}; response.read.return_value = b'ok'
        with patch.object(client.opener, 'open', side_effect=[
                HTTPError(URL, 503, '', {'Retry-After': '0'}, None),
                HTTPError(URL, 429, '', {'Retry-After': '1'}, None), response]), patch('sale_monitor.http.time.sleep'):
            self.assertEqual(client.get(URL).body, b'ok')
        data = client.evidence.snapshot()
        self.assertEqual(client.count, 3)
        self.assertEqual(data['response_status_counts']['http'], {'503': 1, '429': 1, '200': 1})
        self.assertEqual(data['failure_observations'], 2)

    def test_transport_exception_is_not_inferred_http_status(self):
        client = Client(delay=0)
        with patch.object(client.opener, 'open', side_effect=RemoteDisconnected('secret')), patch('sale_monitor.http.time.sleep'):
            with self.assertRaises(FetchError): client.get(URL)
        data = client.evidence.snapshot()
        self.assertEqual(client.count, 3)
        self.assertEqual(data['response_status_counts']['http'], {})
        self.assertEqual(data['exception_counts']['http'], {'RemoteDisconnected': 3})
        self.assertNotIn('secret', json.dumps(data))

    def test_not_found_body_is_still_available(self):
        client = Client(delay=0)
        with patch.object(client.opener, 'open', side_effect=HTTPError(URL, 404, '', {}, BytesIO(b'not found'))):
            with self.assertRaises(FetchError) as raised: client.get(URL)
        self.assertEqual(raised.exception.page.body, b'not found')
        self.assertEqual(client.evidence.snapshot()['response_status_counts']['http'], {'404': 1})

    def test_browser_records_retry_header_without_duplicate_count(self):
        client = Client(browser=True)
        with patch('playwright.sync_api.sync_playwright') as factory, patch('sale_monitor.http.time.time', return_value=1000):
            browser = factory.return_value.__enter__.return_value.chromium.launch.return_value
            response = browser.new_page.return_value.goto.return_value
            response.status = 429; response.url = URL; response.header_value.return_value = '900'
            with self.assertRaises(FetchError): client.rendered(URL)
        data = client.evidence.snapshot()
        self.assertEqual(client.count, 1)
        self.assertEqual(data['response_status_counts']['browser'], {'429': 1})
        self.assertEqual(data['failure_observations'], 1)
        browser.close.assert_called_once()

    def test_browser_exception_is_sanitized(self):
        client = Client(browser=True)
        with patch('playwright.sync_api.sync_playwright') as factory:
            browser = factory.return_value.__enter__.return_value.chromium.launch.return_value
            browser.new_page.return_value.goto.side_effect = TimeoutError('secret')
            with self.assertRaises(TimeoutError): client.rendered(URL)
        data = client.evidence.snapshot()
        self.assertEqual(data['exception_counts']['browser'], {'TimeoutError': 1})
        self.assertNotIn('secret', json.dumps(data))

    def test_retry_header_forms_and_bounded_samples(self):
        self.assertEqual(retry_after_evidence(None), {'kind': 'absent'})
        self.assertEqual(retry_after_evidence('Thu, 01 Jan 1970 00:20:00 GMT'),
                         {'kind': 'http_date', 'at': '1970-01-01T00:20:00+00:00'})
        for raw in ['private', 'NaN', 'Infinity']:
            result = retry_after_evidence(raw)
            self.assertEqual(result['kind'], 'invalid')
            self.assertEqual(len(result['sha256']), 64)
        evidence = ResponseEvidence()
        for _ in range(2000): evidence.record(URL, 'http', status=503)
        snap = evidence.snapshot()
        self.assertEqual(len(snap['failure_samples']), 12)
        self.assertEqual(snap['omitted_failure_samples'], 1988)
        self.assertEqual(snap['response_status_counts']['http'], {'503': 2000})
        snap['failure_samples'].clear()
        self.assertEqual(len(evidence.snapshot()['failure_samples']), 12)

    def test_checkpoint_and_new_instance_scope_do_not_relabel_old_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); client = Client(delay=0)
            c = Collector(root, 'ark', CFG, 'r1', client)
            c.enqueue({'type': 'product', 'url': URL, 'created_at': '2026-10-01T00:00:00+00:00', 'attempts': 4})
            client.evidence.record(URL, 'http', status=503)
            c.save()
            self.assertEqual(Store(root).load('stores/ark.json', {})['http_evidence']['run_id'], 'r1')
            resumed = Collector(root, 'ark', CFG, 'r2', Client())
            self.assertEqual(summary(resumed.state)['status'], 'stale')
            self.assertEqual(next(iter(resumed.state['queue'].values()))['attempts'], 4)
            resumed.save()
            self.assertEqual(summary(resumed.state)['response_status_counts']['http'], {})
            self.assertEqual(summary(resumed.state)['run_id'], 'r2')

    def test_public_details_preserve_evidence_and_missing_job_context(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); disk = Store(root/'state')
            evidence = ResponseEvidence(); evidence.record(URL, 'http', status=503, retry='900', until=1900)
            disk.save('stores/ark.json', {'store': 'ark', 'run_id': 'old', 'offers': {}, 'queue': {},
                                       'http_evidence': {'run_id': 'old', **evidence.snapshot()}})
            result = aggregate(root/'state', root/'public', 'new', NOW)
            details = json.loads((root/'public/collection_errors.json').read_text())
            self.assertEqual(result['stores']['ark']['http_evidence']['status'], 'stale')
            self.assertNotIn('failure_samples', result['stores']['ark']['http_evidence'])
            self.assertEqual(details['stores']['ark']['http_evidence']['failure_observations'], 1)
            self.assertEqual(details['stores']['ark']['status'], 'job_missing')
            self.assertEqual(result['stores']['ark']['current_offers'], 0)


if __name__ == '__main__':
    unittest.main()

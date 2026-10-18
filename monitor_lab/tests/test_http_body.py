import gzip
import hashlib
from http.client import HTTPResponse
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError
import unittest
from unittest.mock import patch

from monitor_lab.acquire import Coordinator, PooledTransport, UrllibTransport
from monitor_lab.tests.test_lab import FakeClock

URL = 'https://www.pc-koubou.jp/products/detail.php?product_id=1'


def response(headers, body=b'', status=200, stream=None):
    wire = (f'HTTP/1.1 {status} fixture\r\n' + headers + '\r\n\r\n').encode() + body
    source = stream(wire) if stream else BytesIO(wire)
    result = HTTPResponse(SimpleNamespace(makefile=lambda *args: source))
    result.begin()
    return result


class Connection:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.sock = None
        self.closed = False

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))

    def getresponse(self):
        return self.responses.pop(0)

    def close(self):
        self.closed = True


class HTTPBodyTest(unittest.TestCase):
    def fetch(self, method, raw, *, capture=None):
        clock = FakeClock()
        transport = UrllibTransport() if method == 'urllib' else PooledTransport()
        connection = Connection([raw])
        if method == 'urllib':
            transport.opener = SimpleNamespace(open=lambda *args, **kwargs: raw)
        client = Coordinator(transport, clock=clock, monotonic=clock, sleep=clock.sleep, delay=0, capture=capture)
        with patch('monitor_lab.acquire.http.client.HTTPSConnection', return_value=connection):
            page, receipt = client.fetch(URL)
        return page, receipt, client, connection

    def test_short_content_length_is_failed_with_status_and_partial_evidence_on_both_transports(self):
        for method in ('urllib', 'pooled'):
            for body in (b'', b'<h1>Apparently complete product</h1>'):
                with self.subTest(method=method, size=len(body)):
                    saved = []
                    page, receipt, client, connection = self.fetch(method, response('Content-Length: 1000', body),
                        capture=lambda row, data: saved.append(data) or 'partial.body')
                    self.assertIsNone(page)
                    self.assertEqual(200, receipt.status)
                    self.assertEqual('transport:IncompleteRead', receipt.error)
                    self.assertEqual(len(body), receipt.body_bytes)
                    self.assertEqual(hashlib.sha256(body).hexdigest(), receipt.body_sha256)
                    self.assertEqual([body], saved)
                    self.assertTrue(receipt.body_incomplete)
                    self.assertEqual(1000, receipt.http_body['expected_bytes'])
                    self.assertFalse(receipt.http_body['complete'])
                    self.assertEqual(1, client.requests)
                    self.assertEqual(client.clock() + 60, client.hosts['www.pc-koubou.jp']['until'])
                    if method == 'pooled':
                        self.assertTrue(connection.closed)
                        self.assertEqual({}, client.transport.connections)

    def test_incomplete_chunk_and_missing_terminator_preserve_every_received_payload_byte(self):
        for method in ('urllib', 'pooled'):
            for wire, expected in ((b'A\r\nabc', b'abc'),
                                   (b'3\r\nabc\r\n5\r\nde', b'abcde'),
                                   (b'3\r\nabc\r\n', b'abc'),
                                   (b'3\r\nabc\r', b'abc'),
                                   (b'3\r\nabc\r\n0\r\n', b'abc'),
                                   (b'3\r\nabc\r\n0\r\nX: y\r\n', b'abc'),
                                   (b'3\r\nabc\r\nnot-a-size\r\n', b'abc')):
                with self.subTest(method=method, wire=wire):
                    saved = []
                    page, receipt, _, _ = self.fetch(method, response('Transfer-Encoding: chunked', wire),
                        capture=lambda row, data: saved.append(data) or 'partial.body')
                    self.assertIsNone(page)
                    self.assertEqual('transport:IncompleteRead', receipt.error)
                    self.assertEqual([expected], saved)
                    self.assertEqual(200, receipt.status)
                    self.assertEqual('chunked', receipt.http_body['framing'])

    def test_complete_fixed_chunked_close_delimited_and_empty_bodies_remain_valid(self):
        cases = [('Content-Length: 3', b'abc', b'abc', 'content_length'),
                 ('Transfer-Encoding: chunked', b'3\r\nabc\r\n0\r\n\r\n', b'abc', 'chunked'),
                 ('Transfer-Encoding: chunked', b'3;extension=value\r\nabc\r\n0\r\nX-Trailer: ok\r\n\r\n', b'abc', 'chunked'),
                 ('Connection: close', b'abc', b'abc', 'close_delimited'),
                 ('Content-Length: 0', b'', b'', 'content_length')]
        for method in ('urllib', 'pooled'):
            for headers, wire, expected, framing in cases:
                with self.subTest(method=method, headers=headers):
                    page, receipt, client, _ = self.fetch(method, response(headers, wire))
                    self.assertIsNotNone(page)
                    self.assertEqual(expected, page.body)
                    self.assertIsNone(receipt.error)
                    self.assertFalse(receipt.body_incomplete)
                    self.assertTrue(receipt.http_body['complete'])
                    self.assertEqual(framing, receipt.http_body['framing'])
                    self.assertEqual({}, client.hosts)

    def test_chunked_precedence_does_not_compare_decoded_payload_with_ignored_content_length(self):
        for method in ('urllib', 'pooled'):
            page, receipt, _, _ = self.fetch(method, response(
                'Transfer-Encoding: Chunked\r\nContent-Length: 9999', b'3\r\nabc\r\n0\r\n\r\n'))
            self.assertEqual(b'abc', page.body)
            self.assertIsNone(receipt.error)
            self.assertIsNone(receipt.http_body['expected_bytes'])

    def test_non_body_status_does_not_require_content_length_bytes(self):
        for method in ('urllib', 'pooled'):
            for status in (204, 304):
                page, receipt, _, _ = self.fetch(method, response('Content-Length: 9999', status=status))
                self.assertIsNone(page)
                self.assertEqual('http_' + str(status), receipt.error)
                self.assertFalse(receipt.body_incomplete)
                self.assertEqual(0, receipt.body_bytes)
                self.assertEqual('no_body', receipt.http_body['framing'])

    def test_invalid_or_conflicting_lengths_do_not_become_complete_observations(self):
        for headers in ('Content-Length: nope', 'Content-Length: -3', 'Content-Length: 3\r\nContent-Length: 9',
                        'Transfer-Encoding: gzip, chunked', 'Content-Length: ' + '9' * 5000):
            page, receipt, _, _ = self.fetch('urllib', response(headers, b'abc'))
            self.assertIsNone(page)
            self.assertEqual('transport:InvalidBodyFraming', receipt.error)
            self.assertTrue(receipt.body_incomplete)

    def test_matching_duplicate_lengths_and_case_variants_are_not_false_failures(self):
        for headers in ('cOnTeNt-LeNgTh: 3', 'Content-Length: 3\r\nContent-Length: 3', 'Content-Length: 3, 3'):
            page, receipt, _, _ = self.fetch('urllib', response(headers, b'abc'))
            self.assertEqual(b'abc', page.body)
            self.assertTrue(receipt.http_body['complete'])

    def test_limit_still_bounds_reading_and_drops_a_pooled_connection_with_unread_bytes(self):
        with patch('monitor_lab.acquire.MAX_BODY', 8):
            page, receipt, _, connection = self.fetch('pooled', response('Content-Length: 1000', b'x' * 1000))
        self.assertIsNone(page)
        self.assertEqual('body_limit_exceeded', receipt.error)
        self.assertEqual(9, receipt.body_bytes)
        self.assertTrue(receipt.body_incomplete)
        self.assertTrue(connection.closed)

    def test_gzip_length_is_checked_against_transport_bytes_without_rewriting_payload(self):
        body = gzip.compress(b'<h1>product</h1>')
        page, receipt, _, _ = self.fetch('urllib', response(f'Content-Encoding: gzip\r\nContent-Length: {len(body)}', body))
        self.assertEqual(body, page.body)
        self.assertEqual(len(body), receipt.http_body['received_bytes'])
        self.assertTrue(receipt.http_body['complete'])

    def test_socket_failure_after_a_partial_read_keeps_prefix_and_response_status(self):
        class Interrupted(BytesIO):
            calls = 0
            def read1(self, size):
                self.calls += 1
                if self.calls > 1:
                    raise TimeoutError('synthetic body timeout')
                return super().read1(min(size, 3))
        saved = []
        page, receipt, _, _ = self.fetch('urllib', response('Content-Length: 20', b'abcdefghijklmnopqrst', stream=Interrupted),
            capture=lambda row, body: saved.append(body) or 'timeout.body')
        self.assertIsNone(page)
        self.assertEqual('transport:TimeoutError', receipt.error)
        self.assertEqual(200, receipt.status)
        self.assertEqual([b'abc'], saved)
        self.assertTrue(receipt.body_incomplete)

    def test_authentication_and_retry_after_keep_precedence_over_body_failure(self):
        for status, header, error in ((403, '', 'authentication_or_challenge'),
                                     (503, 'Retry-After: 120\r\n', 'retry_after')):
            page, receipt, client, _ = self.fetch('urllib', response(header + 'Content-Length: 1000', b'partial', status))
            self.assertIsNone(page)
            self.assertEqual(error, receipt.error)
            self.assertTrue(receipt.body_incomplete)
            self.assertEqual('transport:IncompleteRead', receipt.http_body['error'])
            gate = client.hosts['www.pc-koubou.jp']
            self.assertTrue(gate['blocked'] if status == 403 else gate['until'] == client.clock() + 120)

    def test_successful_keep_alive_connection_is_reused_without_hidden_requests(self):
        transport = PooledTransport()
        connection = Connection([response('Content-Length: 3', b'one'), response('Content-Length: 3', b'two')])
        clock = FakeClock()
        client = Coordinator(transport, delay=0, clock=clock, monotonic=clock, sleep=clock.sleep)
        with patch('monitor_lab.acquire.http.client.HTTPSConnection', return_value=connection) as constructor:
            self.assertEqual(b'one', client.fetch(URL)[0].body)
            self.assertEqual(b'two', client.fetch(URL)[0].body)
        self.assertEqual(1, constructor.call_count)
        self.assertEqual(2, len(connection.requests))
        self.assertFalse(connection.closed)
        transport.close()

    def test_urllib_HTTPError_wrapper_retains_incomplete_404_and_cannot_confirm_empty_search(self):
        body = b'<p>No matches</p>'
        for headers, wire in (('Content-Length: 1000', body),
                              ('Transfer-Encoding: chunked', f'{len(body):X}\r\n'.encode() + body + b'\r\n0\r\n')):
            raw = response(headers, wire, status=404)
            error = HTTPError(URL, 404, 'fixture', raw.headers, raw)
            transport = UrllibTransport()
            transport.opener = SimpleNamespace(open=lambda *args, **kwargs: (_ for _ in ()).throw(error))
            inspect = unittest.mock.Mock(return_value=True)
            client = Coordinator(transport, delay=0, inspect_not_found=inspect)
            page, receipt = client.fetch(URL)
            self.assertIsNone(page)
            self.assertEqual(404, receipt.status)
            self.assertEqual('transport:IncompleteRead', receipt.error)
            self.assertEqual(len(body), receipt.body_bytes)
            inspect.assert_not_called()

    def test_body_failure_gate_is_shared_when_switching_transport(self):
        _, failed, client, _ = self.fetch('urllib', response('Content-Length: 99', b'partial'))
        connection = Connection([response('Content-Length: 2', b'ok')])
        client.transport = PooledTransport()
        with patch('monitor_lab.acquire.http.client.HTTPSConnection', return_value=connection) as constructor:
            page, waiting = client.fetch(URL)
            self.assertIsNone(page)
            self.assertEqual('shared_host_wait', waiting.error)
            self.assertEqual([], connection.requests)
            self.assertEqual(0, constructor.call_count)
            self.assertEqual(1, client.requests)
            client.sleep(60)
            page, recovered = client.fetch(URL)
        self.assertEqual(b'ok', page.body)
        self.assertIsNone(recovered.error)
        self.assertEqual(2, client.requests)
        self.assertEqual(1, len(connection.requests))
        client.transport.close()

    def test_invalid_chunk_sizes_cannot_bypass_the_body_limit_or_add_framing_to_payload(self):
        for method in ('urllib', 'pooled'):
            for invalid in (b'-1', b'+1', b'not-hex'):
                for prefix, expected in ((b'', b''), (b'3\r\nabc\r\n', b'abc')):
                    saved = []
                    with self.subTest(method=method, invalid=invalid, prefix=prefix), \
                         patch('monitor_lab.acquire.MAX_BODY', 8):
                        page, receipt, _, _ = self.fetch(method,
                            response('Transfer-Encoding: chunked', prefix + invalid + b'\r\n' + b'x' * 32),
                            capture=lambda row, data: saved.append(data) or 'invalid.body')
                        self.assertIsNone(page)
                        self.assertLessEqual(receipt.body_bytes, 9)
                        self.assertEqual('transport:IncompleteRead', receipt.error)
                        self.assertEqual([expected], saved)

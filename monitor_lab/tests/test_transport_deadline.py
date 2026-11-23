"""Deterministic socket-boundary tests; no network or wall-clock sleeps."""
from contextlib import contextmanager
import hashlib
import http.client
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler

from monitor_lab.acquire import (BrowserResult, Coordinator, PooledTransport,
                                 UrllibTransport, body_record)
from monitor_lab.tests.test_lab import FakeClock

URL = 'https://www.pc-koubou.jp/products/detail.php?product_id=1'


class Drip(BytesIO):
    def __init__(self, wire, clock, fast_bytes, seconds):
        super().__init__(wire)
        self.clock, self.fast_bytes, self.seconds = clock, fast_bytes, seconds

    def read1(self, size):
        if self.tell() < self.fast_bytes:
            return super().read1(min(size, self.fast_bytes - self.tell()))
        data = super().read1(min(size, 1))
        if data:
            self.clock.sleep(self.seconds)
        return data


class WireSocket:
    def __init__(self, stream):
        self.stream, self.timeouts, self.sent = stream, [], []
        self.closed = False

    def makefile(self, mode):
        assert mode == 'rb'
        return self.stream

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True


NATIVE_CONNECTION = http.client.HTTPConnection


@contextmanager
def native_transport(kind, clock, sockets, connect_seconds=0):
    """Retain stdlib request/getresponse and urllib handlers; replace connect."""
    sockets = list(sockets)
    connections = []

    class Connection(NATIVE_CONNECTION):
        def __init__(self, host, port=None, timeout=None, context=None):
            super().__init__(host, port, timeout)
            connections.append(self)

        def connect(self):
            clock.sleep(connect_seconds)
            self.sock = sockets.pop(0)

    transport = UrllibTransport(clock) if kind == 'urllib' else PooledTransport(clock)
    if kind == 'urllib':
        # Disable only the test machine's proxy configuration, not HTTP logic.
        from urllib.request import build_opener
        transport.opener = build_opener(ProxyHandler({}), *[h for h in transport.opener.handlers
                                                            if not isinstance(h, ProxyHandler)])
    with patch('monitor_lab.acquire.http.client.HTTPSConnection', Connection), \
         patch('monitor_lab.acquire.http.client.HTTPConnection', Connection), \
         patch('socket.create_connection', side_effect=AssertionError('network forbidden')):
        yield transport, connections
    transport.close()


def wire(body, headers=None, status=200):
    headers = headers or f'Content-Length: {len(body)}'
    head = (f'HTTP/1.1 {status} Fixture\r\n{headers}\r\n\r\n').encode()
    return head + body, len(head)


class TransportDeadlineTest(unittest.TestCase):
    def fetch(self, kind, data, fast_bytes, *, budget=2, seconds=1, connect_seconds=0):
        clock, saved = FakeClock(), []
        sock = WireSocket(Drip(data, clock, fast_bytes, seconds))
        with native_transport(kind, clock, [sock], connect_seconds) as (transport, connections):
            client = Coordinator(transport, budget=budget, delay=0, clock=clock, monotonic=clock,
                                 sleep=clock.sleep, capture=lambda receipt, body: saved.append(body) or 'saved.body')
            page, receipt = client.fetch(URL)
            state = (page, receipt, client, saved, sock, connections)
        return state

    def test_slow_body_stops_at_cumulative_deadline_and_keeps_exact_prefix(self):
        data, head = wire(b'abcdef')
        for kind in ('urllib', 'pooled'):
            with self.subTest(kind=kind):
                page, receipt, client, saved, sock, _ = self.fetch(kind, data, head)
                self.assertIsNone(page)
                self.assertEqual('transport:TimeoutError', receipt.error)
                self.assertEqual(2, receipt.elapsed_seconds)
                self.assertEqual(200, receipt.status)
                self.assertEqual([b'ab'], saved)
                self.assertEqual(hashlib.sha256(b'ab').hexdigest(), receipt.body_sha256)
                self.assertFalse(receipt.http_body['complete'])
                self.assertEqual(client.clock() + 60, client.hosts['www.pc-koubou.jp']['until'])
                self.assertTrue(sock.closed)
                self.assertEqual(1, min(sock.timeouts))
                self.assertTrue(all(0 < t <= 2 for t in sock.timeouts))

    def test_slow_initial_header_never_reaches_body_or_becomes_a_page(self):
        data, _ = wire(b'abc')
        for kind in ('urllib', 'pooled'):
            page, receipt, _, saved, sock, _ = self.fetch(kind, data, 0)
            self.assertIsNone(page)
            self.assertEqual('transport:TimeoutError', receipt.error)
            self.assertEqual(2, receipt.elapsed_seconds)
            self.assertIsNone(receipt.status)
            self.assertIsNone(receipt.body_sha256)
            self.assertEqual([], saved)
            self.assertTrue(sock.closed)

    def test_slow_chunk_header_payload_and_trailer_are_all_bounded(self):
        chunked = b'6\r\nabcdef\r\n0\r\nX-Trailer: slow\r\n\r\n'
        data, head = wire(chunked, 'Transfer-Encoding: chunked')
        cases = [(head, b''), (head + 3, b'ab'),
                 (head + len(b'6\r\nabcdef\r\n0\r\n'), b'abcdef')]
        for kind in ('urllib', 'pooled'):
            for fast_bytes, prefix in cases:
                with self.subTest(kind=kind, fast_bytes=fast_bytes):
                    page, receipt, _, saved, _, _ = self.fetch(kind, data, fast_bytes)
                    self.assertIsNone(page)
                    self.assertEqual('transport:TimeoutError', receipt.error)
                    self.assertEqual(2, receipt.elapsed_seconds)
                    self.assertEqual([prefix], saved)
                    self.assertEqual(200, receipt.status)

    def test_connect_time_consumes_the_body_budget_and_late_header_is_rejected(self):
        data, head = wire(b'abcdef')
        for kind in ('urllib', 'pooled'):
            page, receipt, _, saved, _, _ = self.fetch(kind, data, head, connect_seconds=1)
            self.assertIsNone(page)
            self.assertEqual(2, receipt.elapsed_seconds)
            self.assertEqual([b'a'], saved)
            page, receipt, _, saved, _, _ = self.fetch(kind, data, head, connect_seconds=3)
            self.assertIsNone(page)
            self.assertEqual('transport:TimeoutError', receipt.error)
            self.assertEqual([], saved)

    def test_fast_fixed_chunked_and_close_delimited_with_read_ahead_stay_exact(self):
        body = b'abcdef' * 2000
        cases = [(body, None), (body, 'Connection: close'),
                 (f'{len(body):X};x=y\r\n'.encode() + body + b'\r\n0\r\nX: y\r\n\r\n', 'Transfer-Encoding: chunked')]
        for kind in ('urllib', 'pooled'):
            for raw_body, headers in cases:
                data, _ = wire(raw_body, headers)
                page, receipt, _, saved, _, _ = self.fetch(kind, data, len(data))
                self.assertEqual(body, page.body)
                self.assertEqual([body], saved)
                self.assertIsNone(receipt.error)
                self.assertTrue(receipt.http_body['complete'])

    def test_reused_connection_gets_fresh_request_deadline(self):
        # A realistic keep-alive peer releases the next response only after its request.
        clock = FakeClock()
        data, head = wire(b'abc')
        sock = WireSocket(Drip(data, clock, head, .25))
        with native_transport('pooled', clock, [sock]) as (transport, connections):
            client = Coordinator(transport, budget=20, delay=0, clock=clock, monotonic=clock, sleep=clock.sleep)
            self.assertEqual(b'abc', client.fetch(URL)[0].body)
            clock.sleep(4)
            sock.stream = Drip(data, clock, head, .25)
            self.assertEqual(b'abc', client.fetch(URL)[0].body)
            self.assertEqual(1, len(connections))
            self.assertEqual(2, client.requests)

    def test_late_complete_custom_http_and_browser_results_cannot_be_accepted(self):
        for kind in ('custom', 'browser'):
            for budget in (2, 120):
                clock = FakeClock()
                def get(url, timeout):
                    self.assertEqual(min(25, budget), timeout)
                    clock.sleep(timeout + 1)
                    if kind == 'browser':
                        main = body_record(URL, b'wire', kind='browser_response_body')
                        main.update(status=200, headers={})
                        return BrowserResult(main, body_record(URL, b'dom', kind='rendered_dom'), [], [])
                    return 200, {}, b'complete'
                client = Coordinator(SimpleNamespace(method=kind, get=get), budget=budget, delay=0,
                                     clock=clock, monotonic=clock, sleep=clock.sleep)
                page, receipt = client.fetch(URL)
                self.assertIsNone(page)
                self.assertEqual('transport:TimeoutError', receipt.error)
                self.assertEqual(200, receipt.status)
                self.assertFalse(receipt.body_incomplete)

    def test_auth_and_retry_after_remain_stronger_than_elapsed_deadline(self):
        for kind in ('urllib', 'pooled'):
            for status, header, expected in ((403, '', 'authentication_or_challenge'),
                                             (503, 'Retry-After: 120\r\n', 'retry_after')):
                data, head = wire(b'abcdef', header + 'Content-Length: 6', status)
                page, receipt, client, _, _, _ = self.fetch(kind, data, head)
                self.assertIsNone(page)
                self.assertEqual(expected, receipt.error)
                self.assertEqual('transport:TimeoutError', receipt.http_body['error'])
                gate = client.hosts['www.pc-koubou.jp']
                self.assertTrue(gate.get('blocked') if status == 403 else gate['until'] == client.clock() + 120)


if __name__ == '__main__':
    unittest.main()

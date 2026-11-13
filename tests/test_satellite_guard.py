from email.message import Message
from io import BytesIO
from http.client import IncompleteRead
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import HTTPSHandler, build_opener
from urllib.response import addinfourl

from sale_monitor.budget import Robots
from sale_monitor.http import Client, FetchError, SafeRedirect
from sale_monitor.satellite.control import ControlState
from sale_monitor.satellite.deadline import Handshake
from sale_monitor.satellite.guard import CommunicationForbidden, Guard, REFUSAL_MARKERS
from sale_monitor.satellite.protocol import time_string
from test_satellite_deadline import FakeClock
from test_satellite_protocol import COMMIT, NONCE, NOW, URL, refusal_fixture


class MemoryHTTPS(HTTPSHandler):
    def __init__(self, replies):
        super().__init__()
        self.replies = iter(replies)
        self.calls = []

    def https_open(self, request):
        self.calls.append(request.full_url)
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        status, headers, body = reply
        message = Message()
        for key, value in headers.items():
            message[key] = value
        response = addinfourl(BytesIO(body), message, request.full_url, status)
        response.msg = "fixture"
        return response


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.control = ControlState(self.tmp.name)
        self.clock = FakeClock()
        self.handshake = self.clock.handshake()
        self.guard = self.make_guard()
        for name, value in (("sleep", self.clock.advance), ("monotonic", lambda: self.clock.m), ("time", lambda: self.clock.w)):
            p = patch("sale_monitor.http.time." + name, value)
            p.start()
            self.addCleanup(p.stop)

    def make_guard(self, **kwargs):
        return Guard("tsukumo", self.control, self.handshake, NOW + 500,
                     wall_clock=lambda: self.clock.w, sleep=self.clock.advance,
                     refusal_id=lambda: "refusal-1", **kwargs)

    def client(self, replies, **kwargs):
        client = Client(guard=self.guard, delay=0, **kwargs)
        transport = MemoryHTTPS(replies)
        client.opener = build_opener(SafeRedirect(client._before_redirect, self.guard), transport)
        return client, transport

    def test_first_403_persisted_before_exception_and_no_more_calls(self):
        client, transport = self.client([(403, {}, b"no"), (200, {}, b"unused")])
        with self.assertRaisesRegex(CommunicationForbidden, "refused"):
            client.get(URL)
        self.assertEqual(ControlState(self.tmp.name).active_refusals, [refusal_fixture()])
        for url in (URL, URL + "2"):
            try:
                client.get(url)
            except FetchError:
                pass
        self.assertEqual(transport.calls, [URL])
        reloaded = self.make_guard()
        with self.assertRaises(CommunicationForbidden):
            reloaded.before_request(URL)
        cleared = self.make_guard(ticket={"cleared_refusals": ["refusal-1"]})
        cleared.before_request(URL)
        self.control.add_refusal(refusal_fixture())
        self.assertFalse(self.control.active_refusals)

    def test_each_marker_and_401(self):
        for marker in REFUSAL_MARKERS:
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as root:
                control = ControlState(root)
                guard = Guard("tsukumo", control, self.handshake, NOW + 500,
                              wall_clock=lambda: self.clock.w, sleep=self.clock.advance)
                guard.before_request(URL)
                with self.assertRaises(CommunicationForbidden):
                    guard.after_response(URL, 200, {}, marker.upper().encode())
                self.assertEqual(len(ControlState(root).active_refusals), 1)
                with self.assertRaises(CommunicationForbidden):
                    guard.before_request(URL)
                self.assertEqual(guard.counters["requests"], 1)
        client, transport = self.client([(401, {}, b"Unauthorized")])
        with self.assertRaises(CommunicationForbidden):
            client.get(URL)
        self.assertEqual(len(transport.calls), 1)

    def test_large_normal_telemetry_page_is_not_refused(self):
        telemetry = '<!-- recaptcha enterprise telemetry (全ページ読込・スコア取得はログイン時のみ) --><style>.grecaptcha-badge { visibility: hidden; }</style>'
        body = ('<html><title>Tsukumo</title>' + telemetry + 'x' * 120000 + '</html>').encode()
        for status in (200, 404):
            with self.subTest(status=status):
                self.guard.before_request(URL)
                self.guard.after_response(URL, status, {}, body)
                self.assertFalse(self.control.active_refusals)
                self.assertFalse(self.guard.communication_forbidden)

    def test_title_small_challenge_and_error_marker_refuse(self):
        for status, body in ((200, b'<title>Access Denied</title>' + b'x' * 9000),
                             (200, b'<p>CAPTCHA</p>'), (503, b'x' * 9000 + b'captcha'),
                             (404, b'<title>Access Denied</title>' + b'x' * 9000),
                             (200, b'captcha' + b'x' * (8191 - 7))):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as root:
                self.control = ControlState(root)
                self.guard = self.make_guard()
                self.guard.before_request(URL)
                with self.assertRaisesRegex(CommunicationForbidden, 'refused'):
                    self.guard.after_response(URL, status, {}, body)
                self.assertEqual(len(ControlState(root).active_refusals), 1)

    def test_marker_body_boundary_is_in_bytes(self):
        self.guard.before_request(URL)
        self.guard.after_response(URL, 200, {}, b'captcha' + b'x' * (8192 - 7))
        self.assertFalse(self.control.active_refusals)

    def test_refusal_written_before_forbid(self):
        snapshots = []
        original = self.control.write
        def write(path, value):
            snapshots.append((bool(value["active_refusals"]), self.guard.communication_forbidden))
            original(path, value)
        self.control.write = write
        self.guard.before_request(URL)
        with self.assertRaises(CommunicationForbidden):
            self.guard.after_response(URL, 403, {}, b"")
        self.assertEqual(snapshots[-1], (True, False))

    def test_robots_denial_and_swallowed_exception(self):
        self.guard.robots_transport = lambda url: (403, {}, b"no")
        robots = Robots(fetch=self.guard.fetch_robots)
        self.assertFalse(robots.allowed(URL))
        self.assertTrue(ControlState(self.tmp.name).active_refusals)
        with self.assertRaises(CommunicationForbidden):
            self.guard.before_request(URL)
        self.assertEqual(self.guard.counters["ordinary_attempted"], 0)
        self.assertEqual(self.guard.counters["requests"], 1)

    def test_default_robots_transport_is_guarded(self):
        transport = MemoryHTTPS([(403, {}, b"no")])
        from sale_monitor import http
        original = http.build_opener
        with patch("sale_monitor.http.build_opener", side_effect=lambda *args: original(*args, transport)):
            with self.assertRaises(CommunicationForbidden):
                self.guard.fetch_robots("https://shop.tsukumo.co.jp/robots.txt")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(self.guard.counters["ordinary_attempted"], 0)

    def test_client_requires_guarded_robots_and_cannot_bypass_denial(self):
        with self.assertRaisesRegex(ValueError, "guarded_robots_fetch_required"):
            Client(guard=self.guard, robots=Robots(fetch=lambda url: (200, "")))
        calls = []
        def fetch(url):
            calls.append(url)
            return 403, {}, b"no"
        self.guard.robots_transport = fetch
        client, transport = self.client([], robots=Robots(fetch=self.guard.fetch_robots))
        for _ in range(2):
            with self.assertRaises(FetchError):
                client.get(URL)
        self.assertEqual(calls, ["https://shop.tsukumo.co.jp/robots.txt"])
        self.assertFalse(transport.calls)
        self.assertEqual(self.guard.counters["requests"], 1)

    def test_foreign_queued_url_precedes_robots(self):
        for url in ("https://example.com/", "http://shop.tsukumo.co.jp/", "https://user@shop.tsukumo.co.jp/",
                    "https://shop.tsukumo.co.jp:444/", "https://shop.tsukumo.co.jp./"):
            with self.subTest(url=url):
                self.guard = self.make_guard()
                client, transport = self.client([])
                with self.assertRaises(CommunicationForbidden):
                    client.get(url)
                self.assertFalse(transport.calls)
                self.assertTrue(self.guard.communication_forbidden)

    def test_redirect_foreign_host_blocked_before_hop(self):
        client, transport = self.client([(302, {"Location": "https://example.com/"}, b""), (200, {}, b"unused")])
        with self.assertRaises(CommunicationForbidden):
            client.get(URL)
        self.assertEqual(transport.calls, [URL])
        self.assertEqual(self.guard.counters["requests"], 1)

    def test_allowed_redirects_each_go_through_guard(self):
        client, transport = self.client([(302, {"Location": URL + "2"}, b""), (200, {}, b"ok")])
        self.assertEqual(client.get(URL).body, b"ok")
        self.assertEqual(transport.calls, [URL, URL + "2"])
        self.assertEqual(self.guard.counters["requests"], 2)
        self.assertEqual(self.guard.counters["ordinary_succeeded"], 1)
        self.assertEqual(self.clock.m, 5)

    def test_redirect_response_denial_or_retry_blocks_hop(self):
        for headers, body in (({"Location": URL + "2"}, b"CAPTCHA"),
                              ({"Location": URL + "2", "Retry-After": "20"}, b"")):
            with self.subTest(headers=headers), tempfile.TemporaryDirectory() as root:
                self.control = ControlState(root)
                self.guard = self.make_guard()
                client, transport = self.client([(302, headers, body), (200, {}, b"unused")])
                with self.assertRaises(CommunicationForbidden):
                    client.get(URL)
                self.assertEqual(len(transport.calls), 1)

    def test_429_and_retry_after_stop_stage_and_persist(self):
        for status, headers in ((429, {}), (200, {"Retry-After": "30"}),
                                (503, {"Retry-After": "Wed, 15 Jan 2031 00:00:00 GMT"}),
                                (429, {"Retry-After": "not a date"})):
            with self.subTest(status=status, headers=headers), tempfile.TemporaryDirectory() as root:
                self.control = ControlState(root)
                self.guard = self.make_guard()
                client, transport = self.client([(status, headers, b"ok")])
                with self.assertRaisesRegex(CommunicationForbidden, "retry_after"):
                    client.get(URL)
                self.assertFalse(self.control.active_refusals)
                self.assertGreater(ControlState(root).retry_until("shop.tsukumo.co.jp"), self.clock.w)
                guard = self.make_guard()
                with self.assertRaises(CommunicationForbidden):
                    guard.before_request(URL)
                self.assertEqual(guard.counters["requests"], 0)
                self.assertEqual(len(transport.calls), 1)

    def test_five_failures_count_each_internal_retry(self):
        client, transport = self.client([TimeoutError()] * 6)
        with self.assertRaises(FetchError):
            client.get(URL)
        with self.assertRaisesRegex(CommunicationForbidden, "no_response"):
            client.get(URL + "2")
        self.assertEqual(len(transport.calls), 5)
        self.assertEqual(self.guard.counters["no_response"], 5)
        self.assertEqual(self.guard.counters["ordinary_attempted"], 5)
        with self.assertRaises(CommunicationForbidden):
            client.get(URL + "3")
        self.assertEqual(len(transport.calls), 5)

    def test_http_response_resets_failure_streak_and_404_counts(self):
        self.guard.before_request(URL)
        self.guard.after_no_response(URL, TimeoutError())
        self.guard.after_response(URL, 503, {}, b"")
        self.assertEqual(self.guard.consecutive_no_response, 0)
        self.assertEqual(self.guard.counters["ordinary_succeeded"], 0)
        client, _ = self.client([(404, {}, b"missing")])
        with self.assertRaisesRegex(FetchError, "http_404"):
            client.get(URL)
        self.assertEqual(self.guard.counters["ordinary_succeeded"], 1)

    def test_minimum_interval_across_restart_and_larger_value(self):
        self.guard.before_request(URL)
        self.clock.advance(1)
        self.control = ControlState(self.tmp.name)
        restarted = self.make_guard(min_interval_seconds=7)
        restarted.before_request(URL)
        self.assertEqual(self.clock.m, 7)
        self.assertEqual(self.control.data["last_request_at"]["shop.tsukumo.co.jp"], NOW + 7)

    def test_wait_may_not_cross_deadline(self):
        self.guard.before_request(URL)
        self.guard.collect_until = NOW + 4
        with self.assertRaises(CommunicationForbidden):
            self.guard.before_request(URL)
        self.assertEqual(self.guard.counters["requests"], 1)

    def test_checkpoint_delay_cannot_start_late_request(self):
        client, transport = self.client([])
        advanced = False
        def checkpoint(guard):
            nonlocal advanced
            if not advanced:
                advanced = True
                self.clock.advance(501)
        self.guard.progress = checkpoint
        with self.assertRaisesRegex(CommunicationForbidden, "deadline"):
            client.get(URL)
        self.assertFalse(transport.calls)

    def test_unverified_late_wrong_or_skewed_echo_zero_requests(self):
        for kind in ("missing", "late", "nonce", "ticket", "skew"):
            with self.subTest(kind=kind):
                clock = FakeClock(60 if kind == "skew" else 0)
                self.handshake = Handshake(COMMIT, NONCE, deadline_clock=lambda: clock.m, wall_clock=lambda: clock.w)
                self.handshake.begin_ack()
                if kind != "missing":
                    clock.advance(121 if kind == "late" else 1)
                    self.handshake.accept_echo(dict(ticket_commit="b" * 40 if kind == "ticket" else COMMIT,
                        nonce="wrong_nonce_123456" if kind == "nonce" else NONCE, actions_time=time_string(NOW)))
                self.guard = self.make_guard()
                client, transport = self.client([])
                with self.assertRaises(CommunicationForbidden):
                    client.get(URL)
                self.assertFalse(transport.calls)
                self.assertEqual(self.guard.counters["requests"], 0)

    def test_drift_stops_and_browser_is_disabled(self):
        self.clock.w += 5
        with self.assertRaises(CommunicationForbidden):
            self.guard.before_request(URL)
        self.assertEqual(self.guard.counters["requests"], 0)
        self.guard = self.make_guard()
        client = Client(browser=True, guard=self.guard)
        with self.assertRaisesRegex(CommunicationForbidden, "browser_disabled"):
            client.rendered(URL)

    def test_incomplete_response_is_not_no_response_or_success(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.status, response.url, response.headers = 200, URL, {}
        response.read.side_effect = IncompleteRead(b"partial", 10)
        response.__enter__.return_value = response
        client = Client(guard=self.guard, delay=0)
        with patch.object(client.opener, "open", return_value=response):
            with self.assertRaisesRegex(FetchError, "IncompleteRead"):
                client.get(URL)
        self.assertEqual(self.guard.counters["requests"], 1)
        self.assertEqual(self.guard.counters["ordinary_succeeded"], 0)
        self.assertEqual(self.guard.counters["no_response"], 0)
        self.guard.after_no_response(URL, ValueError("parser failure"))
        self.assertEqual(self.guard.counters["no_response"], 0)

    def test_unreadable_401_still_latches(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.status, response.url, response.headers = 401, URL, {}
        response.read.side_effect = IncompleteRead(b"", 10)
        response.__enter__.return_value = response
        client = Client(guard=self.guard, delay=0)
        with patch.object(client.opener, "open", return_value=response):
            with self.assertRaisesRegex(CommunicationForbidden, "refused"):
                client.get(URL)
        self.assertTrue(ControlState(self.tmp.name).active_refusals)
        self.assertEqual(self.guard.counters["no_response"], 0)

    def test_retry_after_uses_longer_input_deadline(self):
        host = "shop.tsukumo.co.jp"
        self.control.set_retry_after(host, NOW + 10)
        guard = self.make_guard(input_retry_after={host: NOW + 30})
        self.clock.advance(11)
        with self.assertRaisesRegex(CommunicationForbidden, "retry_after"):
            guard.before_request(URL)
        self.assertEqual(guard.counters["requests"], 0)

    def test_failed_refusal_write_still_forbids(self):
        self.guard.before_request(URL)
        with patch.object(self.control, "write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.guard.after_response(URL, 403, {}, b"")
        self.assertTrue(self.guard.communication_forbidden)
        with self.assertRaises(CommunicationForbidden):
            self.guard.before_request(URL)

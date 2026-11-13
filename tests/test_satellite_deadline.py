import unittest
from unittest.mock import Mock, patch

from sale_monitor.satellite.deadline import Handshake, deadline_clock
from sale_monitor.satellite.protocol import time_string
from test_satellite_protocol import COMMIT, NONCE, NOW


class FakeClock:
    def __init__(self, offset=0):
        self.m = 0
        self.w = NOW + offset

    def advance(self, seconds):
        self.m += seconds
        self.w += seconds

    def handshake(self, outbound=0, waiting=0, inbound=0):
        clock = Handshake(COMMIT, NONCE, deadline_clock=lambda: self.m, wall_clock=lambda: self.w)
        clock.begin_ack()
        self.advance(outbound + waiting)
        actions_time = NOW + self.m
        self.advance(inbound)
        clock.accept_echo(dict(ticket_commit=COMMIT, nonce=NONCE, actions_time=time_string(actions_time)))
        return clock


class DeadlineTests(unittest.TestCase):
    def test_separate_delays_never_late(self):
        for outbound, waiting, inbound in ((0, 0, 0), (2, 7, 11), (20, 30, 60), (0, 0, 120)):
            with self.subTest(delays=(outbound, waiting, inbound)):
                clock = FakeClock()
                handshake = clock.handshake(outbound, waiting, inbound)
                self.assertIsNone(handshake.reason)
                while handshake.may_request(NOW + 300):
                    self.assertLess(NOW + clock.m, NOW + 300)
                    clock.advance(1)
                early = 300 - clock.m
                self.assertGreaterEqual(early, 0)
                self.assertLessEqual(early, outbound + waiting + inbound)
                self.assertEqual(early, outbound + waiting)
                self.assertFalse(handshake.must_kill(NOW + 300))
                clock.advance(30)
                self.assertTrue(handshake.must_kill(NOW + 300))

    def test_missing_late_wrong_nonce_and_ticket(self):
        for changes, delay in ((None, 121), ({"nonce": "other_nonce_123456"}, 1),
                               ({"ticket_commit": "b" * 40}, 1)):
            clock = FakeClock()
            handshake = Handshake(COMMIT, NONCE, deadline_clock=lambda: clock.m, wall_clock=lambda: clock.w)
            handshake.begin_ack()
            self.assertFalse(handshake.may_request(NOW + 300))
            clock.advance(delay)
            echo = dict(ticket_commit=COMMIT, nonce=NONCE, actions_time=time_string(NOW))
            echo.update(changes or {})
            self.assertFalse(handshake.accept_echo(echo))
            self.assertEqual(handshake.reason, "clock_unverified")
            self.assertFalse(handshake.may_request(NOW + 300))

    def test_wall_change_preserves_upper_and_drift_stops(self):
        for jump in (-100, 100):
            clock = FakeClock()
            handshake = clock.handshake(2, 3, 4)
            upper = handshake.actions_now_upper()
            clock.w += jump
            self.assertEqual(handshake.actions_now_upper(), upper)
            self.assertFalse(handshake.may_request(NOW + 300))
            clock.w -= jump
            self.assertFalse(handshake.may_request(NOW + 300))

    def test_drift_is_cumulative_and_checked_at_echo(self):
        clock = FakeClock()
        handshake = clock.handshake()
        clock.advance(10)
        clock.w += 4.99
        self.assertTrue(handshake.may_request(NOW + 300))
        clock.w += 0.02
        self.assertFalse(handshake.may_request(NOW + 300))
        clock = FakeClock()
        handshake = Handshake(COMMIT, NONCE, deadline_clock=lambda: clock.m, wall_clock=lambda: clock.w)
        handshake.begin_ack()
        clock.m += 6
        self.assertFalse(handshake.accept_echo(dict(ticket_commit=COMMIT, nonce=NONCE, actions_time=time_string(NOW))))

    def test_definite_skew_and_uncertain_interval(self):
        for offset in (-60, 60):
            handshake = FakeClock(offset).handshake(1, 1, 1)
            self.assertTrue(handshake.definitely_skewed())
            self.assertEqual(handshake.reason, "clock_skew")
        handshake = FakeClock(31).handshake(10, 0, 1)
        self.assertFalse(handshake.definitely_skewed())
        self.assertIsNone(handshake.reason)

    def test_platform_selection(self):
        kernel = Mock()
        def frequency(pointer):
            pointer._obj.value = 1000
            return 1
        def counter(pointer):
            pointer._obj.value = 1234567000
            return 1
        kernel.QueryPerformanceFrequency.side_effect = frequency
        kernel.QueryPerformanceCounter.side_effect = counter
        with patch("sale_monitor.satellite.deadline.sys.platform", "win32"), \
                patch("sale_monitor.satellite.deadline.ctypes.WinDLL", return_value=kernel, create=True), \
                patch("sale_monitor.satellite.deadline.time.perf_counter", side_effect=AssertionError):
            self.assertEqual(deadline_clock()(), 1234567)
            kernel.QueryPerformanceCounter.assert_called_once()
        with patch("sale_monitor.satellite.deadline.sys.platform", "darwin"):
            with self.assertRaisesRegex(RuntimeError, "suspend-aware"):
                deadline_clock()

    def test_serialized_clock_has_same_system_reference(self):
        clock = FakeClock()
        clock.m = 1234567
        clock.w += clock.m
        handshake = clock.handshake(1, 2, 3)
        self.assertIsNone(handshake.reason)
        import json
        value = json.loads(json.dumps(handshake.to_dict()))
        restored = Handshake.from_dict(value, deadline_clock=lambda: clock.m, wall_clock=lambda: clock.w)
        self.assertEqual(restored.to_dict(), handshake.to_dict())
        clock.advance(20)
        self.assertEqual(restored.actions_now_upper(), handshake.actions_now_upper())
        collect_until = clock.w + 500
        self.assertTrue(restored.may_request(collect_until))
        clock.w += 5
        self.assertFalse(restored.may_request(collect_until))
        failed = Handshake.from_dict(restored.to_dict(), deadline_clock=lambda: clock.m, wall_clock=lambda: clock.w)
        clock.w -= 5
        self.assertFalse(failed.may_request(collect_until))

    def test_qpc_failures_fail_closed(self):
        kernel = Mock()
        kernel.QueryPerformanceFrequency.return_value = 0
        with patch("sale_monitor.satellite.deadline.sys.platform", "win32"), \
                patch("sale_monitor.satellite.deadline.ctypes.WinDLL", return_value=kernel, create=True):
            with self.assertRaisesRegex(RuntimeError, "Frequency"):
                deadline_clock()

    def test_linux_boottime_and_missing_clock(self):
        with patch("sale_monitor.satellite.deadline.sys.platform", "linux"), \
                patch("sale_monitor.satellite.deadline.time.CLOCK_BOOTTIME", 7, create=True), \
                patch("sale_monitor.satellite.deadline.time.clock_gettime", return_value=123, create=True) as clock:
            self.assertEqual(deadline_clock()(), 123)
            clock.assert_called_once_with(7)

    def test_suspend_during_echo_wait_and_after_echo(self):
        clock = FakeClock()
        handshake = clock.handshake()
        clock.advance(1000)
        self.assertFalse(handshake.may_request(NOW + 500))
        self.assertTrue(handshake.must_kill(NOW + 500))
        clock = FakeClock()
        handshake = Handshake(COMMIT, NONCE, deadline_clock=lambda: clock.m, wall_clock=lambda: clock.w)
        handshake.begin_ack()
        clock.advance(121)
        self.assertFalse(handshake.accept_echo(dict(ticket_commit=COMMIT, nonce=NONCE, actions_time=time_string(NOW + 1))))

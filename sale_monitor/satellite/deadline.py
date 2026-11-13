"""Suspend-aware deadline bounds established by an ack/echo exchange."""
import sys
import time
import ctypes

from .protocol import (DRIFT_LIMIT_SECONDS, ECHO_WAIT_SECONDS, KILL_AFTER_SECONDS,
                       SKEW_LIMIT_SECONDS, ValidationError, epoch, validate_echo)


def deadline_clock():
    if sys.platform == "win32":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        frequency = ctypes.c_longlong()
        for name in ("QueryPerformanceCounter", "QueryPerformanceFrequency"):
            function = getattr(kernel, name)
            function.argtypes = [ctypes.POINTER(ctypes.c_longlong)]
            function.restype = ctypes.c_int
        if not kernel.QueryPerformanceFrequency(ctypes.byref(frequency)) or frequency.value <= 0:
            raise RuntimeError("QueryPerformanceFrequency failed")

        def qpc():
            counter = ctypes.c_longlong()
            if not kernel.QueryPerformanceCounter(ctypes.byref(counter)):
                raise RuntimeError("QueryPerformanceCounter failed")
            return counter.value / frequency.value

        return qpc
    if sys.platform.startswith("linux") and hasattr(time, "CLOCK_BOOTTIME"):
        return lambda: time.clock_gettime(time.CLOCK_BOOTTIME)
    raise RuntimeError("Satellite requires a suspend-aware clock: Windows QPC or Linux CLOCK_BOOTTIME")


class Handshake:
    def __init__(self, ticket_commit, nonce, *, deadline_clock, wall_clock):
        self.ticket_commit = ticket_commit
        self.nonce = nonce
        self.clock = deadline_clock
        self.wall_clock = wall_clock
        self.m1 = self.w1 = self.m2 = self.w2 = self.actions_time = None
        self.reason = "clock_unverified"
        self.failed = False

    def to_dict(self):
        return {name: getattr(self, name) for name in (
            "ticket_commit", "nonce", "m1", "w1", "m2", "w2", "actions_time", "reason", "failed")}

    @classmethod
    def from_dict(cls, value, *, deadline_clock, wall_clock):
        handshake = cls(value["ticket_commit"], value["nonce"],
                        deadline_clock=deadline_clock, wall_clock=wall_clock)
        for name in ("m1", "w1", "m2", "w2", "actions_time", "reason", "failed"):
            setattr(handshake, name, value[name])
        return handshake

    def begin_ack(self):
        if self.m1 is not None:
            raise ValueError("ack_already_started")
        self.m1, self.w1 = self.clock(), self.wall_clock()

    def drifted(self):
        return self.m1 is None or abs((self.wall_clock() - self.w1) - (self.clock() - self.m1)) >= DRIFT_LIMIT_SECONDS

    def accept_echo(self, echo):
        if self.failed or self.actions_time is not None:
            return False
        self.m2, self.w2 = self.clock(), self.wall_clock()
        try:
            validate_echo(echo)
            if (self.m1 is None or not 0 <= self.m2 - self.m1 <= ECHO_WAIT_SECONDS
                    or echo["nonce"] != self.nonce or echo["ticket_commit"] != self.ticket_commit):
                raise ValidationError("clock_unverified")
            self.actions_time = epoch(echo["actions_time"])
            if self.drifted():
                raise ValidationError("clock_unverified")
            if self.definitely_skewed():
                raise ValidationError("clock_skew")
        except ValidationError as exc:
            self.reason = "clock_skew" if exc.reason == "clock_skew" else "clock_unverified"
            self.failed = True
            return False
        self.reason = None
        return True

    def skew_interval(self):
        if self.actions_time is None:
            raise ValueError("clock_unverified")
        return self.w1 - self.actions_time, self.w2 - self.actions_time

    def definitely_skewed(self):
        low, high = self.skew_interval()
        return low > SKEW_LIMIT_SECONDS or high < -SKEW_LIMIT_SECONDS

    def actions_now_upper(self):
        if self.actions_time is None:
            raise ValueError("clock_unverified")
        return self.actions_time + self.clock() - self.m1

    def may_request(self, collect_until):
        if self.failed:
            return False
        if self.actions_time is None:
            if self.m1 is not None and self.clock() - self.m1 > ECHO_WAIT_SECONDS:
                self.failed = True
            return False
        if self.drifted() or self.clock() < self.m1:
            self.reason, self.failed = "clock_unverified", True
            return False
        return self.actions_now_upper() < _seconds(collect_until)

    def must_kill(self, collect_until):
        return self.actions_time is not None and self.actions_now_upper() >= _seconds(collect_until) + KILL_AFTER_SECONDS


def _seconds(value):
    return epoch(value) if isinstance(value, str) else value

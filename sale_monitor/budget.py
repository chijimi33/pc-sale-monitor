from __future__ import annotations

import math
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser


class HostBudget:
    def __init__(self, hosts_cfg: dict, seconds: float, used: dict | None = None,
                 sleep=time.sleep, clock=time.monotonic):
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("invalid_budget_seconds")
        self.hosts_cfg = hosts_cfg
        self.seconds = seconds
        self.used = dict(used or {})
        if any(type(value) is not int or value < 0 for value in self.used.values()):
            raise ValueError("invalid_budget_usage")
        self.sleep = sleep
        self.clock = clock
        self.last_request = {}
        self.crawl_delays = {}

    def interval(self, host):
        default = self.hosts_cfg.get("default", {}).get("min_interval_seconds", 2)
        configured = self.hosts_cfg.get(host, {})
        value = max(float(configured.get("min_interval_seconds", default)), self.crawl_delays.get(host, 0))
        if not math.isfinite(value) or value <= 0:
            raise ValueError("invalid_host_interval")
        return value

    def set_crawl_delay(self, host, seconds):
        if seconds is not None:
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError("invalid_crawl_delay")
            self.crawl_delays[host] = max(self.crawl_delays.get(host, 0), seconds)

    def limit(self, host):
        return math.floor(self.seconds / self.interval(host))

    def spend(self, host):
        from .http import FetchError
        if self.remaining(host) <= 0:
            raise FetchError("host_budget_exhausted")
        if host in self.last_request:
            wait = self.interval(host) - (self.clock() - self.last_request[host])
            if wait > 0:
                self.sleep(wait)
        self.last_request[host] = self.clock()
        self.used[host] = self.used.get(host, 0) + 1

    def remaining(self, host):
        return max(0, self.limit(host) - self.used.get(host, 0))

    def snapshot(self) -> dict:
        return dict(self.used)


class Robots:
    """Cache policies using a raw, single-attempt fetch without robots recursion."""

    def __init__(self, fetch, budget: HostBudget | None = None):
        self.fetch = fetch
        self.budget = budget
        self.cache = {}

    def _load(self, host):
        if host not in self.cache:
            parser = RobotFileParser()
            self.cache[host] = ("unavailable", parser)
            try:
                if self.budget is not None:
                    self.budget.spend(host)
                status, body = self.fetch(f"https://{host}/robots.txt")
                if status in (401, 403) or status >= 500:
                    return self.cache[host]
                if 400 <= status < 500:
                    parser.allow_all = True
                elif 200 <= status < 300:
                    parser.parse(body.splitlines())
                else:
                    return self.cache[host]
                self.cache[host] = ("available", parser)
                if self.budget is not None:
                    self.budget.set_crawl_delay(host, parser.crawl_delay("PCSaleMonitor"))
            except Exception:
                # An unreadable policy denies the entire host for this run.
                pass
        return self.cache[host]

    def allowed(self, url) -> bool:
        status, parser = self._load(urlsplit(url).hostname)
        return status == "available" and parser.can_fetch("PCSaleMonitor", url)

    def crawl_delay(self, host) -> float | None:
        status, parser = self._load(host)
        return parser.crawl_delay("PCSaleMonitor") if status == "available" else None

    def status(self, host):
        return self._load(host)[0]

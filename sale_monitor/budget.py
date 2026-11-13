from __future__ import annotations

import math
import re
import time
from urllib.parse import urlsplit


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


def _normalise_robots_path(value):
    def replace(match):
        text = match.group()
        if text.startswith("%"):
            char = chr(int(text[1:], 16))
            if char in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~":
                return char
            return text.upper()
        return "".join(f"%{byte:02X}" for byte in text.encode("utf-8"))

    return re.sub(r"%[0-9a-fA-F]{2}|[^\x00-\x7f]", replace, value)


class _RobotsRule:
    def __init__(self, pattern, allow):
        pattern = _normalise_robots_path(pattern)
        self.length = len(pattern)
        self.allow = allow
        self.anchored = pattern.endswith("$")
        self.parts = (pattern[:-1] if self.anchored else pattern).split("*")

    def matches(self, path):
        if not path.startswith(self.parts[0]):
            return False
        offset = len(self.parts[0])
        if len(self.parts) == 1:
            return not self.anchored or offset == len(path)
        # Find literal segments without regex backtracking.
        for part in self.parts[1:-1]:
            found = path.find(part, offset)
            if found < 0:
                return False
            offset = found + len(part)
        last = self.parts[-1]
        if self.anchored:
            return path.endswith(last) and len(path) - len(last) >= offset
        return path.find(last, offset) >= 0


class _RobotsPolicy:
    def __init__(self, body=""):
        groups = []
        agents, rules, delays = [], [], []
        has_records = False
        for line in body.lstrip("\ufeff").splitlines():
            key, separator, value = line.split("#", 1)[0].partition(":")
            if not separator:
                continue
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if has_records:
                    groups.append((agents, rules, delays))
                    agents, rules, delays = [], [], []
                    has_records = False
                agents.append(value.lower())
            elif agents and key in ("allow", "disallow", "crawl-delay"):
                has_records = True
                if key == "crawl-delay":
                    try:
                        delay = float(value)
                    except ValueError:
                        continue
                    if math.isfinite(delay) and delay >= 0:
                        delays.append(delay)
                # A pattern may also begin with a wildcard ("*.pdf$").
                elif value.startswith(("/", "*")):
                    rules.append(_RobotsRule(value, key == "allow"))
        groups.append((agents, rules, delays))
        selected = [group for group in groups if "pcsalemonitor" in group[0]]
        if not selected:
            selected = [group for group in groups if "*" in group[0]]
        self.rules = sorted((rule for _, rules, _ in selected for rule in rules),
                            key=lambda rule: (rule.length, rule.allow), reverse=True)
        # Use the most restrictive delay when selected groups are merged.
        self.delay = max((delay for _, _, delays in selected for delay in delays), default=None)

    def allowed(self, url):
        parsed = urlsplit(url)
        path = _normalise_robots_path(parsed.path or "/")
        if path == "/robots.txt":
            return True
        if "?" in url.split("#", 1)[0]:
            path += "?" + _normalise_robots_path(parsed.query)
        for rule in self.rules:
            if rule.matches(path):
                return rule.allow
        return True


class Robots:
    """Cache policies using a raw, single-attempt fetch without robots recursion."""

    def __init__(self, fetch, budget: HostBudget | None = None):
        self.fetch = fetch
        self.budget = budget
        self.cache = {}

    def _load(self, host):
        if host not in self.cache:
            policy = _RobotsPolicy()
            self.cache[host] = ("unavailable", policy)
            try:
                if self.budget is not None:
                    self.budget.spend(host)
                status, body = self.fetch(f"https://{host}/robots.txt")
                if status in (401, 403) or status >= 500:
                    return self.cache[host]
                if 400 <= status < 500:
                    pass
                elif 200 <= status < 300:
                    policy = _RobotsPolicy(body)
                else:
                    return self.cache[host]
                if self.budget is not None:
                    self.budget.set_crawl_delay(host, policy.delay)
                self.cache[host] = ("available", policy)
            except Exception:
                # An unreadable policy denies the entire host for this run.
                pass
        return self.cache[host]

    def allowed(self, url) -> bool:
        status, policy = self._load(urlsplit(url).hostname)
        return status == "available" and policy.allowed(url)

    def crawl_delay(self, host) -> float | None:
        status, policy = self._load(host)
        return policy.delay if status == "available" else None

    def status(self, host):
        return self._load(host)[0]

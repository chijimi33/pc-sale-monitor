from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import http.client
import math
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from sale_monitor.http import Page
from sale_monitor.models import allowed_url, iso
from .safety import environment

HEADERS = {"User-Agent": "PCSaleMonitor/0.1 (+https://github.com/chijimi33/pc-sale-monitor)",
           "Accept-Language": "ja,en;q=0.5", "Cache-Control": "no-cache"}
MAX_BODY = 8 * 1024 * 1024


@dataclass
class Receipt:
    url: str
    status: int | None
    observed_at: str
    body_sha256: str | None
    method: str
    environment: dict
    attempts: list = field(default_factory=list)
    waits: list = field(default_factory=list)
    error: str | None = None
    content_type: str = ""
    elapsed_seconds: float = 0.0
    evidence_mode: str = "live"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class UrllibTransport:
    method = "urllib"

    def __init__(self):
        self.opener = build_opener(NoRedirect())

    def get(self, url, timeout):
        try:
            response = self.opener.open(Request(url, headers=HEADERS), timeout=timeout)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, dict(response.headers), response.read(MAX_BODY + 1)

    def close(self):
        pass


class PooledTransport:
    method = "pooled_http11"

    def __init__(self):
        self.connections = {}

    def get(self, url, timeout):
        p = urlsplit(url)
        key = (p.scheme, p.hostname, p.port)
        connection = self.connections.get(key)
        if connection is None:
            cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
            connection = self.connections[key] = cls(p.hostname, port=p.port, timeout=timeout)
        connection.timeout = timeout
        if connection.sock:
            connection.sock.settimeout(timeout)
        try:
            connection.request("GET", p.path + ("?" + p.query if p.query else ""), headers=HEADERS)
            response = connection.getresponse()
            result = response.status, dict(response.getheaders()), response.read(MAX_BODY + 1)
            if response.will_close:
                connection.close()
                self.connections.pop(key, None)
            return result
        except Exception:
            connection.close()
            self.connections.pop(key, None)
            raise  # No hidden retry on a stale keep-alive connection.

    def close(self):
        for connection in self.connections.values():
            connection.close()


class BrowserTransport:
    method = "browser"

    def __init__(self):
        self.worker = self.browser = None
        self.subrequests = []

    def get(self, url, timeout):
        # Store-specific experiment, no authenticated context or automatic install.
        if urlsplit(url).hostname != "www.pc-koubou.jp":
            raise ValueError("Browser adapter is enabled only for the Koubou experiment")
        from playwright.sync_api import sync_playwright
        if self.worker is None:
            self.worker = sync_playwright().start()
            self.browser = self.worker.chromium.launch(headless=True)
        context = self.browser.new_context(user_agent=HEADERS["User-Agent"],
                                          extra_http_headers={k: v for k, v in HEADERS.items() if k != "User-Agent"})
        seen_navigation = False
        forbidden_response = []

        def route_request(route):
            nonlocal seen_navigation
            request = route.request
            host = urlsplit(request.url).hostname
            allowed = host == urlsplit(url).hostname and allowed_url(request.url) and not forbidden_response
            allowed = allowed and request.method in {"GET", "HEAD"}
            if request.is_navigation_request():
                allowed = allowed and not seen_navigation and request.url == url
                seen_navigation = True
            allowed = allowed and request.resource_type not in {"image", "media", "font"}
            self.subrequests.append({"url": request.url, "resource_type": request.resource_type, "allowed": allowed})
            route.continue_() if allowed else route.abort()

        context.route("**/*", route_request)
        try:
            page = context.new_page()
            def observe_response(response):
                if response.status in (401, 403, 429) or response.headers.get("retry-after"):
                    forbidden_response.append(response)
            page.on("response", observe_response)
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            if response is None:
                raise ValueError("No browser navigation response")
            if forbidden_response:
                blocked = forbidden_response[0]
                return blocked.status, blocked.all_headers(), b"Access denied"
            return response.status, response.all_headers(), page.content().encode()
        finally:
            context.close()

    def close(self):
        if self.browser:
            self.browser.close()
        if self.worker:
            self.worker.stop()


TRANSPORTS = {"urllib": UrllibTransport, "pooled": PooledTransport, "browser": BrowserTransport}


def retry_delay(value, now):
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - now
        except (ValueError, TypeError, OverflowError):
            delay = 60
    return max(0, delay) if math.isfinite(delay) else 60


def challenge(body):
    # Script telemetry that mentions captcha is not a visible challenge.
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", body.decode("utf-8", "replace"), flags=re.I | re.S)
    text = re.sub(r"<[^>]*>", " ", text)
    return bool(re.search(r"verify (?:that )?you are human|checking your browser|access denied|ロボットではないことを|認証が必要", text, re.I))


class Coordinator:
    def __init__(self, transport, *, budget=2100, delay=1, hosts=None,
                 clock=time.time, monotonic=time.monotonic, sleep=time.sleep):
        self.transport, self.delay, self.hosts = transport, delay, hosts if hosts is not None else {}
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep
        self.deadline = monotonic() + budget
        self.last = {}
        self.sequence = []
        self.requests = 0

    def fetch(self, url):
        start = self.monotonic()
        receipt = Receipt(url, None, iso(datetime.fromtimestamp(self.clock(), timezone.utc)), None,
                          self.transport.method, environment())
        if not allowed_url(url) or urlsplit(url).username or urlsplit(url).password:
            receipt.error = "excluded_source"
            return None, receipt
        host = urlsplit(url).hostname
        state = self.hosts.get(host, {})
        if state.get("blocked") or state.get("until", 0) > self.clock():
            receipt.error = "shared_host_wait"
            receipt.waits.append(dict(state))
            return None, receipt
        remaining = max(0, self.last.get(host, 0) + self.delay - self.clock())
        if self.monotonic() + remaining >= self.deadline:
            receipt.error = "shared_budget_exhausted"
            return None, receipt
        if remaining:
            self.sleep(remaining)
            receipt.waits.append({"reason": "host_spacing", "seconds": remaining})
        timeout = min(25, self.deadline - self.monotonic())
        if timeout <= 0:
            receipt.error = "shared_budget_exhausted"
            return None, receipt
        attempt = {"sequence": len(self.sequence) + 1, "url": url, "headers": HEADERS,
                   "started_at": self.clock(), "timeout": timeout,
                   "previous_request": self.sequence[-1] if self.sequence else None}
        # Keep the previous request URL only, avoiding a recursively growing log.
        self.sequence.append(url)
        receipt.attempts.append(attempt)
        self.last[host] = self.clock()
        self.requests += 1
        body = b""
        try:
            status, headers, body = self.transport.get(url, timeout)
            headers = {k.lower(): v for k, v in headers.items()}
            receipt.status, receipt.content_type = status, headers.get("content-type", "")
            receipt.body_sha256 = hashlib.sha256(body).hexdigest()
            retry = headers.get("retry-after")
            if status in (401, 403) or challenge(body):
                receipt.error = "authentication_or_challenge"
                self.hosts[host] = {"blocked": True, "reason": receipt.error, "status": status}
            elif retry or status == 429 or status >= 500:
                receipt.error = "retry_after" if retry or status == 429 else "server_error"
                self.hosts[host] = {"until": self.clock() + retry_delay(retry, self.clock()),
                                    "reason": receipt.error, "status": status, "retry_after": retry}
            elif status != 200:
                receipt.error = "http_" + str(status)
            elif len(body) > MAX_BODY:
                receipt.error = "body_limit_exceeded"
        except Exception as exc:
            receipt.error = "transport:" + type(exc).__name__
            self.hosts[host] = {"until": self.clock() + 60, "reason": receipt.error}
        receipt.elapsed_seconds = self.monotonic() - start
        receipt.observed_at = iso(datetime.fromtimestamp(self.clock(), timezone.utc))
        page = None if receipt.error else Page(url, body, receipt.observed_at, receipt.method, receipt.content_type)
        return page, receipt

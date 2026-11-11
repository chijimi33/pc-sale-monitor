from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
import json
import math
import re
import time
from typing import TYPE_CHECKING
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import allowed_url, iso
from .http_evidence import ResponseEvidence, retry_after_evidence

if TYPE_CHECKING:
    from .budget import HostBudget, Robots


class FetchError(RuntimeError):
    def __init__(self, reason: str, page: Page | None = None):
        super().__init__(reason)
        self.page = page


class SafeRedirect(HTTPRedirectHandler):
    def __init__(self, before_redirect=None):
        self.before_redirect = before_redirect

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl):
            raise FetchError("excluded_redirect_host")
        if getattr(req, "single_request", False):
            raise FetchError("probe_redirect", page=Page(req.full_url, b"", iso(), status=code, headers=dict(headers or {})))
        if self.before_redirect is not None:
            self.before_redirect(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass
class Page:
    url: str
    body: bytes
    observed_at: str
    method: str = "http"
    content_type: str = ""
    status: int = 200
    headers: dict = field(default_factory=dict)

    @property
    def text(self):
        encoding = re.search(r"charset=[\"']?([\w-]+)", self.content_type, re.I)
        if not encoding:
            encoding = re.search(rb"charset=[\"']?([\w-]+)", self.body[:8192], re.I)
        name = encoding.group(1) if encoding else "utf-8"
        if isinstance(name, bytes):
            name = name.decode("ascii", "ignore")
        try:
            return self.body.decode(name, "replace")
        except LookupError:
            return self.body.decode("utf-8", "replace")


class Client:
    def __init__(self, browser: bool = False, delay: float = 1.0, timeout: int = 25,
                 budget: HostBudget | None = None, robots: Robots | None = None):
        self.browser = browser
        self.delay = delay
        self.timeout = timeout
        self.budget = budget
        self.robots = robots
        if robots is not None and budget is not None:
            if robots.budget is not None and robots.budget is not budget:
                raise ValueError("robots_budget_mismatch")
            robots.budget = budget
        self.refused_hosts = set()
        self.last_request = {}
        self.rate_limit_spacing = {}
        self.retry_after = {}
        self.transport_retry_after = {}
        self.transport_failed_urls = {}
        self.access_retry_after = {}
        self.access_failed_urls = {}
        self.count = 0
        self.errors = []
        self.evidence = ResponseEvidence()
        self.opener = build_opener(SafeRedirect(self._before_redirect))

    def _before_redirect(self, url: str):
        # Redirect hops are additional requests under the same host budgets.
        if self.budget is not None or self.robots is not None:
            self._check_robots(url)
            self.wait_for_host(urlsplit(url).hostname)
            self.space_request(urlsplit(url).hostname)
            self.count += 1

    def space_request(self, host: str):
        if self.budget is not None:
            self.budget.spend(host)
            # Only wait for the part of learned spacing not covered by budget.
            if host in self.last_request:
                wait = self.rate_limit_spacing.get(host, 0) - (self.budget.clock() - self.last_request[host])
                if wait > 0:
                    self.budget.sleep(wait)
            self.last_request[host] = self.budget.clock()
            self.budget.last_request[host] = self.last_request[host]
            return
        # A cooldown alone can repeat the same burst immediately afterwards.
        # Keep the lower rate for this collection after a 429, independently
        # of the server's mandatory Retry-After deadline.
        spacing = max(self.delay, self.rate_limit_spacing.get(host, 0))
        elapsed = time.monotonic() - self.last_request.get(host, 0)
        if elapsed < spacing:
            time.sleep(spacing - elapsed)
        self.last_request[host] = time.monotonic()

    def _check_robots(self, url: str):
        if self.robots is not None:
            allowed = self.robots.allowed(url)
            if self.budget is not None:
                self.budget.set_crawl_delay(urlsplit(url).hostname, self.robots.crawl_delay(urlsplit(url).hostname))
            if not allowed:
                raise FetchError("robots_disallowed")

    def wait_for_host(self, host: str):
        access = self.access_retry_after.get(host)
        if access:
            if access["until"] > time.time():
                raise FetchError("access_retry_later:" + access["reason"])
            self.access_retry_after.pop(host, None)
            self.access_failed_urls.pop(host, None)
        failure = self.transport_retry_after.get(host)
        if failure:
            if failure["until"] > time.time():
                raise FetchError("transport_retry_later:" + failure["reason"])
            self.transport_retry_after.pop(host, None)
            self.transport_failed_urls.pop(host, None)
        remaining = self.retry_after.get(host, 0) - time.time()
        if remaining > 60:
            raise FetchError("rate_limited_retry_later")
        if remaining > 0:
            time.sleep(remaining)
        self.retry_after.pop(host, None)

    def access_response(self, url: str, status: int | None, retry: str | None = None):
        host = urlsplit(url).hostname
        if status != 403:
            self.access_failed_urls.pop(host, None)
            return None
        self.refused_hosts.add(host)
        failed = self.access_failed_urls.setdefault(host, set())
        # Bounded distinct denial evidence, never cookies or response bodies.
        if len(failed) < 3:
            failed.add(url)
        details = retry_after_evidence(retry)
        server_until = 0
        if details["kind"] == "seconds":
            server_until = time.time() + max(0, details["seconds"])
        elif details["kind"] == "http_date":
            server_until = datetime.fromisoformat(details["at"]).timestamp()
        if len(failed) >= 3 or server_until > time.time():
            until = max(time.time() + 300, server_until,
                        self.access_retry_after.get(host, {}).get("until", 0))
            self.access_retry_after[host] = {"until": until, "reason": "http_403"}
            return until
        return None

    def defer_host(self, host: str, retry: str | None, attempt: int = 0) -> float:
        try:
            wait = float(retry)
            if not math.isfinite(wait):
                raise ValueError("invalid_retry_after")
            wait = max(0, wait)
        except (ValueError, TypeError):
            try:
                wait = max(0, parsedate_to_datetime(retry).timestamp() - time.time())
            except (ValueError, TypeError):
                wait = 2 ** (attempt + 1)
        self.retry_after[host] = max(self.retry_after.get(host, 0), time.time() + wait)
        return wait

    def check_maintenance(self, requested_url: str, page: Page):
        # Ark redirects every product/list/search entry to this dedicated host
        # during maintenance, with HTTP 200. It is not a product observation.
        if urlsplit(page.url).hostname == "maintenance.ark-pc.co.jp":
            host = urlsplit(requested_url).hostname
            self.access_retry_after[host] = {
                "until": max(time.time() + 300, self.access_retry_after.get(host, {}).get("until", 0)),
                "reason": "store_maintenance",
            }
            raise FetchError("store_maintenance", page=page)

    def defer_response(self, host: str, status: int, retry: str | None, attempt: int = 0) -> float:
        wait = self.defer_host(host, retry, attempt)
        if status == 429:
            previous = max(self.delay, self.rate_limit_spacing.get(host, 0), 1.0)
            self.rate_limit_spacing[host] = min(30.0, previous * 2)
        # A 429 without a usable future deadline still asks us to slow down.
        # Share the collector's five-minute retry interval with every URL on
        # this host, including browser fallback and checkpoint restarts.
        if status == 429 and (retry_after_evidence(retry)["kind"] in ("absent", "invalid") or wait <= 0):
            wait = 300
            self.retry_after[host] = max(self.retry_after.get(host, 0), time.time() + wait)
        return wait

    def get(self, url: str, *, data: bytes | None = None, method: str = "GET", headers: dict | None = None,
            max_attempts: int = 3) -> Page:
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ValueError("invalid_max_attempts")
        if not allowed_url(url):
            raise FetchError("excluded_source")
        host = urlsplit(url).hostname
        self._check_robots(url)
        for attempt in range(max_attempts):
            self.wait_for_host(host)
            self.space_request(host)
            try:
                self.count += 1
                request = Request(url, data=data, method=method, headers={"User-Agent": "PCSaleMonitor/0.1 (+https://github.com/chijimi33/pc-sale-monitor)", "Accept-Language": "ja,en;q=0.5", "Cache-Control": "no-cache", **(headers or {})})
                request.single_request = max_attempts == 1
                with self.opener.open(request, timeout=self.timeout) as result:
                    response_status = getattr(result, "status", None)
                    page = Page(result.url, result.read(), iso(), content_type=result.headers.get("Content-Type", ""),
                                status=response_status if type(response_status) is int else 200, headers=dict(result.headers))
                self.evidence.record(url, "http", status=response_status)
                self.access_response(url, response_status)
                if response_status == 403:
                    self.refused_hosts.add(urlsplit(page.url).hostname)
                    raise FetchError("http_403", page=page)
                self.transport_failed_urls.pop(host, None)
                self.check_maintenance(url, page)
                return page
            except HTTPError as exc:
                self.transport_failed_urls.pop(host, None)
                error_page = None
                if max_attempts == 1:
                    try:
                        body = exc.read() if exc.fp is not None else b""
                    except (OSError, IncompleteRead):
                        body = b""
                    error_page = Page(exc.url, body, iso(), status=exc.code,
                                      content_type=(exc.headers or {}).get("Content-Type", ""),
                                      headers=dict(exc.headers or {}))
                if exc.code == 403:
                    self.refused_hosts.add(urlsplit(exc.url).hostname)
                    retry = (exc.headers or {}).get("Retry-After", "")
                    until = self.access_response(url, exc.code, retry)
                    self.evidence.record(url, "http", status=403, retry=retry, until=until)
                    raise FetchError("http_403", page=error_page) from exc
                self.access_response(url, exc.code)
                if exc.code == 404:
                    try:
                        body = error_page.body if error_page is not None else exc.read()
                    except IncompleteRead as body_error:
                        # A handler's exceptions do not enter sibling handlers.
                        # Count this attempt only as an incomplete body; never
                        # offer partial bytes to store-specific 404 classifiers.
                        self.evidence.record(url, "http", error=body_error)
                        if attempt == max_attempts - 1:
                            raise FetchError("IncompleteRead") from body_error
                        time.sleep(2 ** (attempt + 1))
                        continue
                    self.evidence.record(url, "http", status=exc.code)
                    page = error_page or Page(exc.url, body, iso(), content_type=(exc.headers or {}).get("Content-Type", ""), status=404)
                    raise FetchError("http_404", page=page) from exc
                if exc.code not in (429, 500, 502, 503, 504):
                    self.evidence.record(url, "http", status=exc.code)
                    raise FetchError(f"http_{exc.code}", page=error_page) from exc
                retry = (exc.headers or {}).get("Retry-After", "")
                # Retry-After applies to the host, including other product URLs
                # and browser fallback. The collector persists this deadline.
                wait = self.defer_response(host, exc.code, retry, attempt)
                self.evidence.record(url, "http", status=exc.code, retry=retry, until=self.retry_after[host])
                if wait > 60 or attempt == max_attempts - 1:
                    code = "rate_limited_retry_later" if retry or exc.code == 429 else f"http_{exc.code}"
                    raise FetchError(code, page=error_page) from exc
            except FetchError:
                raise
            except (OSError, TimeoutError, IncompleteRead) as exc:
                self.evidence.record(url, "http", error=exc)
                if attempt == max_attempts - 1:
                    # One broken URL is not a host outage. Only defer after
                    # exhausted retries on three distinct URLs without a
                    # response in between; keep the original transport cause.
                    if isinstance(exc, (ConnectionError, TimeoutError)):
                        failed = self.transport_failed_urls.setdefault(host, set())
                        failed.add(url)
                        if len(failed) >= 3:
                            self.transport_retry_after[host] = {"until": time.time() + 300, "reason": type(exc).__name__}
                    raise FetchError(type(exc).__name__) from exc
                time.sleep(2 ** (attempt + 1))
        raise FetchError("fetch_failed")

    def json(self, url: str):
        page = self.get(url)
        try:
            return json.loads(page.body)
        except (ValueError, UnicodeError) as exc:
            raise FetchError("invalid_json") from exc

    def rendered(self, url: str) -> Page:
        host = urlsplit(url).hostname
        if not self.browser:
            raise FetchError("browser_disabled")
        if not allowed_url(url):
            raise FetchError("excluded_source")
        self._check_robots(url)
        # A shared host deadline keeps its own reason; an explicit refusal in
        # this run then blocks rendering even before that deadline exists.
        self.wait_for_host(host)
        if host in self.refused_hosts:
            raise FetchError("browser_blocked_after_refusal")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(locale="ja-JP")
                page.route("**/*", lambda route: route.continue_() if allowed_url(route.request.url) else route.abort())
                self.space_request(urlsplit(url).hostname)
                self.count += 1
                try:
                    response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                except Exception as exc:
                    self.evidence.record(url, "browser", error=exc)
                    raise
                if response and response.status == 403:
                    self.refused_hosts.add(urlsplit(response.url).hostname)
                    retry = response.header_value("Retry-After")
                    until = self.access_response(url, 403, retry)
                    self.evidence.record(url, "browser", status=403, retry=retry, until=until)
                    raise FetchError("http_403")
                self.access_response(url, response.status if response else None)
                if response and response.status in (429, 500, 502, 503, 504):
                    retry = response.header_value("Retry-After")
                    self.defer_response(urlsplit(response.url).hostname, response.status, retry)
                    self.evidence.record(url, "browser", status=response.status, retry=retry,
                                         until=self.retry_after[urlsplit(response.url).hostname])
                    raise FetchError("rate_limited_retry_later" if retry or response.status == 429 else f"http_{response.status}")
                self.evidence.record(url, "browser", status=response.status if response else None)
                if response and response.status >= 400:
                    raise FetchError(f"http_{response.status}")
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                if not allowed_url(page.url):
                    raise FetchError("excluded_redirect_host")
                result = Page(page.url, page.content().encode(), iso(), "browser", "text/html;charset=utf-8")
                self.check_maintenance(url, result)
                return result
            finally:
                browser.close()

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
import json
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import allowed_url, iso


class FetchError(RuntimeError):
    pass


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl):
            raise FetchError("excluded_redirect_host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass
class Page:
    url: str
    body: bytes
    observed_at: str
    method: str = "http"
    content_type: str = ""

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
    def __init__(self, browser: bool = False, delay: float = 1.0, timeout: int = 25):
        self.browser = browser
        self.delay = delay
        self.timeout = timeout
        self.last_request = {}
        self.count = 0
        self.errors = []
        self.opener = build_opener(SafeRedirect())

    def get(self, url: str, *, data: bytes | None = None, method: str = "GET", headers: dict | None = None) -> Page:
        if not allowed_url(url):
            raise FetchError("excluded_source")
        host = urlsplit(url).hostname
        elapsed = time.monotonic() - self.last_request.get(host, 0)
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        for attempt in range(3):
            try:
                self.count += 1
                self.last_request[host] = time.monotonic()
                request = Request(url, data=data, method=method, headers={"User-Agent": "PCSaleMonitor/0.1 (+https://github.com/chijimi33/pc-sale-monitor)", "Accept-Language": "ja,en;q=0.5", "Cache-Control": "no-cache", **(headers or {})})
                with self.opener.open(request, timeout=self.timeout) as result:
                    return Page(result.url, result.read(), iso(), content_type=result.headers.get("Content-Type", ""))
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise FetchError(f"http_{exc.code}") from exc
                retry = exc.headers.get("Retry-After", "")
                try:
                    wait = float(retry)
                except ValueError:
                    try:
                        wait = max(0, parsedate_to_datetime(retry).timestamp() - time.time())
                    except (ValueError, TypeError):
                        wait = 2 ** (attempt + 1)
                # Long server-imposed backoffs resume in the next scheduled run.
                if wait > 60:
                    raise FetchError("rate_limited_retry_later") from exc
                time.sleep(wait)
            except FetchError:
                raise
            except (OSError, TimeoutError) as exc:
                if attempt == 2:
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
        if not self.browser:
            raise FetchError("browser_disabled")
        if not allowed_url(url):
            raise FetchError("excluded_source")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(locale="ja-JP")
                page.route("**/*", lambda route: route.continue_() if allowed_url(route.request.url) else route.abort())
                page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                if not allowed_url(page.url):
                    raise FetchError("excluded_redirect_host")
                self.count += 1
                return Page(page.url, page.content().encode(), iso(), "browser", "text/html;charset=utf-8")
            finally:
                browser.close()

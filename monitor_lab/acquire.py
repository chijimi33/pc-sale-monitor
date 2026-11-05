from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import http.client
import math
import re
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPHandler, HTTPSHandler, HTTPRedirectHandler, Request, build_opener

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
    body_file: str | None = None
    body_bytes: int | None = None
    body_incomplete: bool = False
    body_kind: str = "http_response"
    body_unavailable: str | None = None
    response_url: str | None = None
    parser_body_kind: str = "http_response"
    rendered_dom: dict | None = None
    auxiliary_responses: list = field(default_factory=list)
    browser_requests: list = field(default_factory=list)
    http_body: dict | None = None


@dataclass
class HTTPResult:
    status: int
    headers: dict
    body: bytes
    http_body: dict


def http_framing(status, headers):
    """Describe transport-byte framing without retaining unrelated headers."""
    fields = [[key.lower(), value] for key, value in headers
              if key.lower() in {'content-length', 'transfer-encoding'}]
    length = [value for key, value in fields if key == 'content-length']
    transfer = [token.strip().lower() for key, value in fields if key == 'transfer-encoding'
                for token in value.split(',')]
    expected, error = None, None
    if 100 <= status < 200 or status in {204, 304}:
        framing, expected = 'no_body', 0
    elif transfer:
        framing = 'chunked' if transfer == ['chunked'] else 'unsupported_transfer_encoding'
        if framing != 'chunked':
            error = 'transport:InvalidBodyFraming'
    elif length:
        values = [value.strip() for field in length for value in field.split(',')]
        try:
            numbers = [int(value) if re.fullmatch(r'[0-9]+', value) else None for value in values]
        except ValueError:  # Python also limits extremely long integer strings.
            numbers = [None]
        if None not in numbers and len(set(numbers)) == 1:
            framing, expected = 'content_length', numbers[0]
        else:
            framing, error = 'invalid_content_length', 'transport:InvalidBodyFraming'
    else:
        framing = 'close_delimited'
    return {'framing': framing, 'expected_bytes': expected, 'headers': fields}, error


class _RequestDeadline:
    def __init__(self, timeout, monotonic):
        self.monotonic, self.end = monotonic, monotonic() + timeout

    def remaining(self):
        remaining = self.end - self.monotonic()
        if remaining <= 0:
            raise TimeoutError('HTTP request deadline exceeded')
        return remaining


class _DeadlineReader:
    """Check one deadline between socket reads, even within a dripping line.

    BufferedReader.readline/read can internally perform many socket reads.
    read1 bounds each operation. Keep read-ahead bytes so HTTP framing and
    payload remain distinct and no bytes are lost between calls.
    """
    def __init__(self, stream, deadline, sock=None):
        self.stream, self.deadline, self.sock = stream, deadline, sock
        self.buffer = bytearray()

    def _receive(self, size):
        remaining = self.deadline.remaining()
        if self.sock is not None:
            self.sock.settimeout(remaining)
        return self.stream.read1(size)

    def read1(self, size=-1):
        self.deadline.remaining()
        if size == 0:
            return b''
        if self.buffer:
            count = len(self.buffer) if size < 0 else min(size, len(self.buffer))
            result = bytes(self.buffer[:count])
            del self.buffer[:count]
            return result
        return self._receive(64 * 1024 if size < 0 else size)

    def read(self, size=-1):
        chunks, count = [], 0
        while size < 0 or count < size:
            chunk = self.read1(64 * 1024 if size < 0 else size - count)
            if not chunk:
                break
            chunks.append(chunk)
            count += len(chunk)
        return b''.join(chunks)

    def readline(self, limit=-1):
        while True:
            self.deadline.remaining()
            end = self.buffer.find(b'\n') + 1
            if limit >= 0 and (not end or end > limit) and len(self.buffer) >= limit:
                end = limit
            if end or limit == 0:
                line = bytes(self.buffer[:end])
                del self.buffer[:end]
                return line
            chunk = self._receive(8192 if limit < 0 else min(8192, limit - len(self.buffer)))
            if not chunk:
                line = bytes(self.buffer)
                self.buffer.clear()
                return line
            self.buffer.extend(chunk)

    def __getattr__(self, name):
        return getattr(self.stream, name)


class _DeadlineHTTPResponse(http.client.HTTPResponse):
    def __init__(self, sock, deadline, **kwargs):
        super().__init__(sock, **kwargs)
        self.fp = _DeadlineReader(self.fp, deadline, sock)


def _configure_response(connection, deadline):
    connection.response_class = lambda sock, **kwargs: _DeadlineHTTPResponse(sock, deadline, **kwargs)


class _DeadlineHTTPHandler(HTTPHandler):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def http_open(self, request):
        return self.do_open(self.owner.connection(http.client.HTTPConnection), request)


class _DeadlineHTTPSHandler(HTTPSHandler):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def https_open(self, request):
        return self.do_open(self.owner.connection(http.client.HTTPSConnection), request, context=self._context)


class _ChunkLineReader:
    """Reject missing trailer terminators and unsafe sizes before body reads."""
    def __init__(self, stream):
        self.stream, self.line_eof, self.trailers = stream, False, False

    def readline(self, *args):
        line = self.stream.readline(*args)
        self.line_eof |= not line
        if line and not self.trailers:
            size = line.split(b';', 1)[0].strip()
            # HTTPResponse accepts signed sizes; a negative value can reach
            # read1(-1) and bypass the bounded payload read entirely.
            if not re.fullmatch(rb'[0-9a-fA-F]+', size):
                raise http.client.IncompleteRead(b'')
            self.trailers = int(size, 16) == 0
        return line

    def __getattr__(self, name):
        return getattr(self.stream, name)


def read_http_response(response, deadline=None):
    """Keep bounded payload bytes even when EOF, chunk framing or I/O fails.

    HTTPResponse.read(n) does not reject short Content-Length bodies. read1
    also lets us retain a partial current chunk instead of losing it inside
    HTTPResponse's aggregated IncompleteRead exception.
    """
    headers = list(response.headers.items())
    details, framing_error = http_framing(response.status, headers)
    chunks, size, read_error = [], 0, None
    read = getattr(response, 'read1', response.read)
    chunked_read1 = details['framing'] == 'chunked' and hasattr(response, 'read1')
    raw = response if isinstance(response, http.client.HTTPResponse) else getattr(response, 'fp', None)
    if deadline is not None and isinstance(raw, http.client.HTTPResponse) and raw.fp is not None:
        if not isinstance(raw.fp, _DeadlineReader):
            raw.fp = _DeadlineReader(raw.fp, deadline)
    lines = None
    if chunked_read1 and isinstance(raw, http.client.HTTPResponse) and raw.fp is not None:
        lines = raw.fp = _ChunkLineReader(raw.fp)
    while size <= MAX_BODY:
        try:
            if deadline is not None:
                deadline.remaining()
            chunk = read(min(64 * 1024, MAX_BODY + 1 - size))
        except Exception as error:
            # read1 has already yielded payload bytes; its chunked exception
            # can instead contain an incomplete CRLF delimiter, not payload.
            if isinstance(error, http.client.IncompleteRead) and not chunked_read1:
                partial = error.partial[:MAX_BODY + 1 - size]
                chunks.append(partial)
                size += len(partial)
            read_error = 'transport:' + type(error).__name__
            break
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    body = b''.join(chunks)
    error = framing_error or read_error
    if error is None and lines is not None and lines.line_eof:
        error = 'transport:IncompleteRead'
    if error is None and size > MAX_BODY:
        error = 'body_limit_exceeded'
    if error is None and details['expected_bytes'] is not None and size != details['expected_bytes']:
        error = 'transport:IncompleteRead'
    details.update(schema=1, received_bytes=size, complete=error is None, error=error)
    return HTTPResult(response.status, dict(headers), body, details)


@dataclass
class BrowserResult:
    main: dict
    rendered: dict | None
    auxiliary: list
    requests: list
    error: str | None = None


def body_record(url, body, *, kind="http_response", content_type="", observed_at="", unavailable=None):
    """Metadata and bounded bytes are separate; absent bytes are never invented."""
    partial = body is not None and len(body) > MAX_BODY
    body = body[:MAX_BODY + 1] if body is not None else None
    return {"url": url, "body_kind": kind, "content_type": content_type, "observed_at": observed_at,
            "body_sha256": hashlib.sha256(body).hexdigest() if body is not None else None,
            "body_bytes": len(body) if body is not None else None, "body_incomplete": partial,
            "body_unavailable": unavailable, "body_file": None, "_body": body}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class UrllibTransport:
    method = "urllib"

    def __init__(self, monotonic=None):
        self.monotonic = monotonic or time.monotonic
        self.opener = build_opener(NoRedirect(), _DeadlineHTTPHandler(self), _DeadlineHTTPSHandler(self))

    def connection(self, cls):
        def create(*args, **kwargs):
            kwargs['timeout'] = self.deadline.remaining()
            connection = cls(*args, **kwargs)
            _configure_response(connection, self.deadline)
            return connection
        return create

    def get(self, url, timeout):
        self.deadline = _RequestDeadline(timeout, self.monotonic)
        try:
            response = self.opener.open(Request(url, headers=HEADERS), timeout=self.deadline.remaining())
        except HTTPError as exc:
            response = exc
        with response:
            return read_http_response(response, self.deadline)

    def close(self):
        pass


class PooledTransport:
    method = "pooled_http11"

    def __init__(self, monotonic=None):
        self.monotonic = monotonic or time.monotonic
        self.connections = {}

    def get(self, url, timeout):
        deadline = _RequestDeadline(timeout, self.monotonic)
        p = urlsplit(url)
        key = (p.scheme, p.hostname, p.port)
        connection = self.connections.get(key)
        if connection is None:
            cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
            connection = self.connections[key] = cls(p.hostname, port=p.port, timeout=timeout)
        try:
            _configure_response(connection, deadline)
            connection.timeout = deadline.remaining()
            if connection.sock:
                connection.sock.settimeout(connection.timeout)
            connection.request("GET", p.path + ("?" + p.query if p.query else ""), headers=HEADERS)
            remaining = deadline.remaining()
            if connection.sock:
                connection.sock.settimeout(remaining)
            response = connection.getresponse()
            result = read_http_response(response, deadline)
            if response.will_close or not result.http_body['complete']:
                response.close()
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
        context = self.browser.new_context(user_agent=HEADERS["User-Agent"], service_workers="block",
                                          extra_http_headers={k: v for k, v in HEADERS.items() if k != "User-Agent"})
        seen_navigation = False
        forbidden_response = []
        main_response, finished, requests = [], [], []

        def route_request(route):
            nonlocal seen_navigation
            request = route.request
            host = urlsplit(request.url).hostname
            allowed = host == urlsplit(url).hostname and allowed_url(request.url) and not forbidden_response
            allowed = allowed and request.method in {"GET", "HEAD"}
            if request.is_navigation_request():
                allowed = allowed and not seen_navigation and request.url == url and request.frame == page.main_frame
                seen_navigation = True
            allowed = allowed and request.resource_type not in {"image", "media", "font"}
            entry = {"url": request.url, "resource_type": request.resource_type, "method": request.method, "allowed": bool(allowed)}
            self.subrequests.append(entry)
            requests.append(entry)
            route.continue_() if allowed else route.abort()

        # Neither service workers nor sockets may open an unrecorded bypass.
        def close_socket(socket):
            entry = {"url": socket.url, "resource_type": "websocket", "allowed": False}
            requests.append(entry)
            self.subrequests.append(entry)
            socket.close()
        try:
            context.route("**/*", route_request)
            context.route_web_socket("**/*", close_socket)
            page = context.new_page()
            def observe_response(response):
                request = response.request
                if request.is_navigation_request() and request.frame == page.main_frame and request.url == url:
                    main_response.append(response)
                if response.status in (401, 403, 429) or "retry-after" in response.headers:
                    forbidden_response.append(response)
            page.on("response", observe_response)
            page.on("requestfinished", lambda request: finished.append(request))
            error = None
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            except Exception as exc:
                response = main_response[-1] if main_response else None
                error = "browser_navigation:" + type(exc).__name__

            def evidence(response):
                headers = {k.lower(): v for k, v in response.headers.items()
                           if k.lower() in {"content-type", "retry-after", "location", "content-length"}}
                body, unavailable = None, None
                if response.request not in finished:
                    unavailable = "response_not_finished"
                else:
                    try:
                        body = response.body()
                    except Exception as exc:
                        unavailable = "browser_body:" + type(exc).__name__
                # Chromium can transcode text returned by Response.body(). These
                # are API-returned bytes, not a claim of original HTTP octets.
                row = body_record(response.url, body, kind="browser_response_body", content_type=headers.get("content-type", ""),
                                  observed_at=iso(datetime.now(timezone.utc)), unavailable=unavailable)
                row.update(status=response.status, headers=headers)
                return row

            main = evidence(response) if response is not None else body_record(url, None, unavailable="no_navigation_response")
            main.setdefault("status", None)
            main.setdefault("headers", {})
            rendered = None
            if error is None and response is not None:
                try:
                    rendered = body_record(page.url, page.content().encode("utf-8"), kind="rendered_dom",
                                           content_type="text/html; charset=utf-8", observed_at=iso(datetime.now(timezone.utc)))
                except Exception as exc:
                    error = "browser_dom:" + type(exc).__name__
            # Serialization can pump response events. Include denials observed
            # through that boundary before deciding whether the DOM is usable.
            auxiliary = [evidence(r) for r in forbidden_response if r is not response]
            return BrowserResult(main, rendered, auxiliary, requests, error)
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
                 clock=time.time, monotonic=time.monotonic, sleep=time.sleep, capture=None, inspect_not_found=None):
        self.transport, self.delay, self.hosts = transport, delay, hosts if hosts is not None else {}
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep
        self.deadline = monotonic() + budget
        self.last = {}
        self.sequence = []
        self.requests = 0
        self.capture = capture
        self.inspect_not_found = inspect_not_found

    def fetch(self, url):
        start = self.monotonic()
        receipt = Receipt(url, None, iso(datetime.fromtimestamp(self.clock(), timezone.utc)), None,
                          self.transport.method, environment())
        if self.transport.method == "browser":
            receipt.parser_body_kind = "rendered_dom"
            receipt.body_kind = "browser_response_body"
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
        parser_body, parser_content_type, representations = None, "", []
        request_deadline = min(self.deadline, self.monotonic() + timeout)
        try:
            result = self.transport.get(url, timeout)
            returned_late = self.monotonic() >= request_deadline
            browser_error, http_body_error, gates = None, None, []
            if isinstance(result, BrowserResult):
                main = result.main
                body = main.pop("_body")
                status, headers = main["status"], main["headers"]
                receipt.response_url = main["url"]
                receipt.body_unavailable = main["body_unavailable"]
                receipt.parser_body_kind = "rendered_dom"
                receipt.rendered_dom = result.rendered
                receipt.auxiliary_responses = result.auxiliary
                receipt.browser_requests = result.requests
                for record in ([result.rendered] if result.rendered else []) + result.auxiliary:
                    representations.append((record, record.pop("_body")))
                if result.rendered:
                    parser_body = representations[0][1]
                    parser_content_type = result.rendered["content_type"]
                gates = [(r["status"], r["headers"], r["url"]) for r in result.auxiliary]
                browser_error = result.error
                if receipt.response_url != url or result.rendered and result.rendered["url"] != url:
                    browser_error = "browser_response_url_mismatch"
                elif body is None:
                    browser_error = browser_error or "browser_main_body_unavailable"
                elif result.rendered is None:
                    browser_error = browser_error or "browser_dom_unavailable"
                elif result.rendered["body_incomplete"]:
                    browser_error = "browser_dom_limit_exceeded"
            else:
                if isinstance(result, HTTPResult):
                    status, headers, body = result.status, result.headers, result.body
                    receipt.http_body = result.http_body
                    http_body_error = result.http_body['error']
                else:
                    status, headers, body = result
                receipt.response_url = url  # HTTP transports reject redirects.
                parser_body = body
            headers = {k.lower(): v for k, v in headers.items()}
            receipt.status, receipt.content_type = status, headers.get("content-type", "")
            if body is not None:
                receipt.body_sha256 = hashlib.sha256(body).hexdigest()
                receipt.body_bytes = len(body)
                receipt.body_incomplete = len(body) > MAX_BODY or bool(receipt.http_body and not receipt.http_body['complete'])
            parser_content_type = parser_content_type or receipt.content_type
            gates.insert(0, (status, headers, receipt.response_url))
            denied = next((g for g in gates if g[0] in (401, 403)), None)
            retries = [g for g in gates if g[1].get("retry-after") is not None or g[0] == 429 or g[0] is not None and g[0] >= 500]
            if denied or challenge(body or b"") or challenge(parser_body or b""):
                receipt.error = "authentication_or_challenge"
                gate = denied or gates[0]
                self.hosts[host] = {"blocked": True, "reason": receipt.error, "status": gate[0], "source_url": gate[2]}
            elif retries:
                gate = max(retries, key=lambda g: retry_delay(g[1].get("retry-after"), self.clock()))
                retry = gate[1].get("retry-after")
                receipt.error = "retry_after" if retry is not None or gate[0] == 429 else "server_error"
                self.hosts[host] = {"until": self.clock() + retry_delay(retry, self.clock()),
                                    "reason": receipt.error, "status": gate[0], "retry_after": retry, "source_url": gate[2]}
            elif returned_late:
                receipt.error = 'transport:TimeoutError'
                self.hosts[host] = {"until": self.clock() + 60, "reason": receipt.error}
            elif browser_error:
                receipt.error = browser_error
                self.hosts[host] = {"until": self.clock() + 60, "reason": receipt.error}
            elif http_body_error:
                receipt.error = http_body_error
                if http_body_error.startswith('transport:'):
                    self.hosts[host] = {"until": self.clock() + 60, "reason": receipt.error}
            elif status != 200:
                receipt.error = "http_" + str(status)
            elif receipt.body_incomplete:
                receipt.error = "body_limit_exceeded"
        except Exception as exc:
            receipt.error = ("browser_runtime:" if self.transport.method == "browser" else "transport:") + type(exc).__name__
            self.hosts[host] = {"until": self.clock() + 60, "reason": receipt.error}
            if isinstance(exc, http.client.IncompleteRead):
                body = exc.partial[:MAX_BODY + 1]
                receipt.body_sha256 = hashlib.sha256(body).hexdigest()
                receipt.body_bytes = len(body)
                receipt.body_incomplete = True
        receipt.elapsed_seconds = self.monotonic() - start
        receipt.observed_at = iso(datetime.fromtimestamp(self.clock(), timezone.utc))
        # Evidence I/O failures must remain failures; never relabel them as a
        # remote transport problem or publish a result with missing evidence.
        if self.capture is not None and receipt.body_sha256 is not None:
            receipt.body_file = self.capture(receipt, body)
        if self.capture is not None:
            for record, content in representations:
                if content is not None:
                    record["body_file"] = self.capture(SimpleNamespace(**record), content)
        page = None if receipt.error else Page(url, parser_body, receipt.observed_at, receipt.method, parser_content_type)
        if (receipt.error == 'http_404' and receipt.body_sha256 is not None and not receipt.body_incomplete
                and self.inspect_not_found is not None):
            missing = Page(url, parser_body, receipt.observed_at, receipt.method, parser_content_type, status=404)
            if self.inspect_not_found(missing):
                page = missing  # Keep the original 404 receipt and body evidence.
        return page, receipt

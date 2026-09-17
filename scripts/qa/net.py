"""Bounded public HTTP reads. Reject local addresses and unapproved redirects."""
import ipaddress
import json
import socket
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.error import HTTPError

LAST_REQUEST = {}
DEFERRED = {}


def validate(url, hosts):
    p = urlsplit(url)
    host = p.hostname or ""
    if p.scheme != "https" or p.username or p.password or p.port not in (None, 443) or host not in hosts:
        raise ValueError("unapproved_public_url")
    if "rakuten" in host:
        raise ValueError("rakuten_excluded")
    for address in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(address[4][0]).is_global:
            raise ValueError("non_public_address")


def fetch(url, allowed_hosts=("api.github.com", "raw.githubusercontent.com", "codeload.github.com")):
    class Redirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            validate(newurl, allowed_hosts)
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    validate(url, allowed_hosts)
    host = urlsplit(url).hostname
    opener = build_opener(Redirect())
    for attempt in range(3):
        remaining = DEFERRED.get(host, 0) - time.monotonic()
        if remaining > 30: raise RuntimeError("rate_limited_retry_later")
        time.sleep(max(0, remaining, 1 - (time.monotonic() - LAST_REQUEST.get(host, 0))))
        LAST_REQUEST[host] = time.monotonic()
        try:
            with opener.open(Request(url, headers={"User-Agent": "PCSaleMonitorQA/1.0", "Cache-Control": "no-cache"}), timeout=40) as response:
                data = response.read(30_000_001)
                if len(data) > 30_000_000: raise ValueError("response_too_large")
                return data, response.url
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2: raise
            retry = exc.headers.get("Retry-After", "")
            if retry:
                from email.utils import parsedate_to_datetime
                from datetime import datetime, timezone
                try: wait = float(retry)
                except ValueError:
                    try: wait = (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError): wait = 60
                DEFERRED[host] = time.monotonic() + max(0, wait)
                if wait > 30: raise RuntimeError("rate_limited_retry_later") from exc
            time.sleep(2 ** attempt)


def get_json(url):
    return json.loads(fetch(url)[0])

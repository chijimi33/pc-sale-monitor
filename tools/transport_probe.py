"""Small read-only comparison using identical, transparent headers.

No proxy rotation, browser impersonation or persistent monitor state.
The optional collector mode preserves the production Client's retry policy.
Output is diagnostic evidence, never a current price observation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
import platform
import re
import shutil
import ssl
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

URLS = ["https://shop.tsukumo.co.jp/", "https://shop.tsukumo.co.jp/goods/0619659180140/"]
FAILED_URLS = ["https://shop.tsukumo.co.jp/goods/4573615590991/",
               "https://shop.tsukumo.co.jp/search?end_of_sales=1&keyword=4895248891543",
               "https://shop.tsukumo.co.jp/"]
HEADERS = {"User-Agent": "PCSaleMonitor/0.1 (+https://github.com/chijimi33/pc-sale-monitor)",
           "Accept-Language": "ja,en;q=0.5", "Cache-Control": "no-cache"}
MARKER = b"\nPCSM_CURL_META:"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def describe(status, body, content_type, url):
    text = body.decode("utf-8", "replace")
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    visible = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", text, flags=re.S | re.I)
    visible = html.unescape(re.sub(r"<[^>]+>", " ", visible)).lower()
    product_id = re.search(r"/goods/(\d+)(?:/|$)", url)
    return {"http_status": status, "url": url, "content_type": content_type,
            "bytes": len(body), "body_sha256": hashlib.sha256(body).hexdigest(),
            "title": re.sub(r"\s+", " ", title.group(1))[:300] if title else None,
            "target_jan": product_id.group(1) if product_id else None,
            "target_jan_in_body": product_id.group(1) in text if product_id else None,
            "structured_product_present": bool(re.search(r'"@type"\s*:\s*"Product"', text)),
            # Ordinary product pages include reCAPTCHA JavaScript for forms;
            # its presence alone is not an interactive challenge page.
            "captcha_or_challenge_text": any(x in visible for x in ("verify you are human", "checking your browser", "complete the captcha", "access denied"))}


def split_curl_headers(data):
    """Separate curl's response headers, including informational/CONNECT blocks."""
    headers = {}
    while re.match(rb"HTTP/\S+ \d{3}(?: |\r?\n)", data):
        block, separator, body = data.partition(b"\r\n\r\n")
        if not separator:
            break
        headers = {}
        for line in block.split(b"\r\n")[1:]:
            name, sep, value = line.partition(b":")
            if sep:
                headers[name.decode("ascii", "ignore").lower()] = value.strip().decode("latin-1")
        data = body
    return headers, data


def probe(url, method, client=None):
    started = time.monotonic()
    result = {"method": method, "requested_url": url, "attempts": 1,
              "checked_at": datetime.now(timezone.utc).isoformat()}
    try:
        if method == "collector_client":
            from sale_monitor.http import FetchError
            before = client.count
            try:
                page = client.get(url)
                result.update(describe(page.status, page.body, page.content_type, page.url))
            except FetchError as exc:
                result["error"] = str(exc)
                if exc.page:
                    result.update(describe(exc.page.status, exc.page.body, exc.page.content_type, exc.page.url))
                elif re.fullmatch(r"http_\d{3}", str(exc)):
                    result["http_status"] = int(str(exc)[5:])
            finally:
                result["attempts"] = client.count - before
                result["host_deferred"] = bool(client.retry_after or client.transport_retry_after)
        elif method == "urllib":
            try:
                response = build_opener(NoRedirect()).open(Request(url, headers=HEADERS), timeout=25)
            except HTTPError as exc:
                response = exc
            with response:
                result.update(describe(response.code, response.read(), response.headers.get("Content-Type", ""), response.url))
                result["retry_after"] = response.headers.get("Retry-After")
        else:
            curl = shutil.which("curl.exe") or shutil.which("curl")
            if not curl:
                result.update(error="curl_unavailable", attempts=0)
                return result
            args = [curl, "--silent", "--show-error", "--dump-header", "-", "--connect-timeout", "10", "--max-time", "25",
                    "--write-out", "\nPCSM_CURL_META:%{http_code}\t%{http_version}\t%{url_effective}\t%{content_type}"]
            for name, value in HEADERS.items():
                args += ["--header", name + ": " + value]
            if method == "curl_http1":
                args.append("--http1.1")
            # Redirects are deliberately not followed by either transport.
            completed = subprocess.run(args + [url], capture_output=True, timeout=30)
            result["exit_code"] = completed.returncode
            if MARKER in completed.stdout:
                body, metadata = completed.stdout.rsplit(MARKER, 1)
                response_headers, body = split_curl_headers(body)
                status, version, effective, content_type = metadata.decode("utf-8", "replace").split("\t", 3)
                result.update(describe(int(status), body, content_type, effective))
                result["http_version"] = version
                result["retry_after"] = response_headers.get("retry-after")
            if completed.returncode:
                result["error"] = completed.stderr.decode("utf-8", "replace")[:500]
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)[:400]
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--collector-client", action="store_true", help="Compare the production Client on three previously failed URLs")
    options = parser.parse_args()
    client = None
    if options.collector_client:
        from sale_monitor.http import Client
        client = Client()
    curl = shutil.which("curl.exe") or shutil.which("curl")
    version = subprocess.run([curl, "--version"], capture_output=True, timeout=10).stdout.decode("utf-8", "replace").splitlines() if curl else []
    environment = {"os": platform.system(), "python": platform.python_version(), "ssl": ssl.OPENSSL_VERSION, "curl": version,
                   "scope": "read_only_transport_probe_not_monitor_observation", "headers": HEADERS,
                   "collector_client": options.collector_client}
    print("PCSM_PROBE_ENV " + json.dumps(environment, ensure_ascii=False), flush=True)
    stopped = None
    for url in FAILED_URLS if options.collector_client else URLS:
        for method in (("collector_client", "curl_default") if options.collector_client else ("urllib", "curl_default", "curl_http1")):
            if stopped:
                result = {"method": method, "requested_url": url, "attempts": 0, "skipped": stopped}
            else:
                result = probe(url, method, client)
                status = result.get("http_status", 0)
                if status in (401, 403, 429) or status >= 500 or result.get("retry_after") or result.get("host_deferred") or result.get("captcha_or_challenge_text"):
                    stopped = "stop_after_denial_rate_limit_server_error_or_challenge"
                time.sleep(2)
            print("PCSM_PROBE_RESULT " + json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

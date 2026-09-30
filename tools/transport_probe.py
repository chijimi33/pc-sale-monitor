"""Small read-only comparison using identical, transparent headers.

No proxy rotation, browser impersonation, retries or persistent monitor state.
Output is diagnostic evidence, never a current price observation.
"""
from __future__ import annotations

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
    return {"http_status": status, "url": url, "content_type": content_type,
            "bytes": len(body), "body_sha256": hashlib.sha256(body).hexdigest(),
            "title": re.sub(r"\s+", " ", title.group(1))[:300] if title else None,
            "target_jan_in_body": "0619659180140" in text,
            "structured_product_present": bool(re.search(r'"@type"\s*:\s*"Product"', text)),
            # Ordinary product pages include reCAPTCHA JavaScript for forms;
            # its presence alone is not an interactive challenge page.
            "captcha_or_challenge_text": any(x in visible for x in ("verify you are human", "checking your browser", "complete the captcha", "access denied"))}


def probe(url, method):
    started = time.monotonic()
    result = {"method": method, "requested_url": url, "attempts": 1,
              "checked_at": datetime.now(timezone.utc).isoformat()}
    try:
        if method == "urllib":
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
            args = [curl, "--silent", "--show-error", "--connect-timeout", "10", "--max-time", "25",
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
                status, version, effective, content_type = metadata.decode("utf-8", "replace").split("\t", 3)
                result.update(describe(int(status), body, content_type, effective))
                result["http_version"] = version
            if completed.returncode:
                result["error"] = completed.stderr.decode("utf-8", "replace")[:500]
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)[:400]
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def main():
    curl = shutil.which("curl.exe") or shutil.which("curl")
    version = subprocess.run([curl, "--version"], capture_output=True, timeout=10).stdout.decode("utf-8", "replace").splitlines() if curl else []
    environment = {"os": platform.system(), "python": platform.python_version(), "ssl": ssl.OPENSSL_VERSION, "curl": version,
                   "scope": "read_only_transport_probe_not_monitor_observation", "headers": HEADERS}
    print("PCSM_PROBE_ENV " + json.dumps(environment, ensure_ascii=False), flush=True)
    stopped = None
    for url in URLS:
        for method in ("urllib", "curl_default", "curl_http1"):
            if stopped:
                result = {"method": method, "requested_url": url, "attempts": 0, "skipped": stopped}
            else:
                result = probe(url, method)
                status = result.get("http_status", 0)
                if status in (401, 403, 429) or status >= 500 or result.get("retry_after") or result.get("captcha_or_challenge_text"):
                    stopped = "stop_after_denial_rate_limit_server_error_or_challenge"
                time.sleep(2)
            print("PCSM_PROBE_RESULT " + json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

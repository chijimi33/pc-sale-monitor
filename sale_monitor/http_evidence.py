"""Bounded response evidence; never persist arbitrary headers or error text."""
from copy import deepcopy
from datetime import timezone
from email.utils import parsedate_to_datetime
import hashlib
import math
from urllib.parse import urlsplit

from .models import iso


def retry_after_evidence(value):
    if not value:
        return {"kind": "absent"}
    value = str(value)
    try:
        seconds = float(value)
        if math.isfinite(seconds):
            return {"kind": "seconds", "seconds": seconds}
    except ValueError:
        pass
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return {"kind": "http_date", "at": date.astimezone(timezone.utc).isoformat()}
    except (ValueError, TypeError, OverflowError):
        return {"kind": "invalid", "sha256": hashlib.sha256(value.encode()).hexdigest()}


class ResponseEvidence:
    def __init__(self):
        self.started_at = iso()
        self.responses = {"http": {}, "browser": {}}
        self.exceptions = {"http": {}, "browser": {}}
        self.failures = 0
        self.samples = []

    def record(self, url, method, *, status=None, error=None, retry=None, until=None):
        if error is None:
            code = str(status) if type(status) is int and 100 <= status <= 599 else "unknown"
            bucket = self.responses[method]
            bucket[code] = bucket.get(code, 0) + 1
            if type(status) is not int or status < 400:
                return
            detail = {"status": int(code) if code != "unknown" else None,
                      "retry_after": retry_after_evidence(retry)}
            if until is not None:
                detail["host_retry_at_epoch_seconds"] = until
        else:
            name = type(error).__name__
            if name not in {"TimeoutError", "RemoteDisconnected", "ConnectionResetError",
                            "ConnectionAbortedError", "ConnectionRefusedError", "BrokenPipeError",
                            "URLError", "OSError", "Error"}:
                name = "other"
            bucket = self.exceptions[method]
            bucket[name] = bucket.get(name, 0) + 1
            detail = {"exception_type": name}
        self.failures += 1
        self.samples.append({"observed_at": iso(), "method": method,
                             "host": urlsplit(url).hostname,
                             "url_sha256": hashlib.sha256(url.encode()).hexdigest(), **detail})
        self.samples = self.samples[-12:]

    def snapshot(self):
        return deepcopy({"scope": "current_client_instance; redirects_and_browser_subresources_excluded",
                         "started_at": self.started_at, "response_status_counts": self.responses,
                         "exception_counts": self.exceptions, "failure_observations": self.failures,
                         "failure_samples": self.samples,
                         "omitted_failure_samples": self.failures - len(self.samples)})


def summary(state):
    evidence = state.get("http_evidence")
    if not isinstance(evidence, dict):
        return {"status": "not_recorded"}
    status = "current" if evidence.get("run_id") == state.get("run_id") and state.get("status") != "job_missing" else "stale"
    return {"status": status, **{k: evidence.get(k) for k in
            ("run_id", "scope", "started_at", "response_status_counts", "exception_counts", "failure_observations")}}

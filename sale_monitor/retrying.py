"""Keep temporary dependencies pending until they can actually be retried."""
from __future__ import annotations

from collections import Counter
from urllib.parse import urlsplit

from .models import timestamp


def transient(reason: str) -> bool:
    return reason in {
        "RemoteDisconnected", "TimeoutError", "ConnectionResetError", "ConnectionAbortedError",
        "ConnectionRefusedError", "BrokenPipeError", "IncompleteRead", "rate_limited_retry_later",
        "http_429", "http_500", "http_502", "http_503", "http_504", "store_maintenance",
    } or reason.startswith(("transport_retry_later:", "access_retry_later:"))


def deferred_until(task: dict) -> float | None:
    """Epoch seconds before which a repeatedly failing task is not retried, else None."""
    attempts = task.get("attempts", 0)
    reason = task.get("last_error")
    if (type(attempts) is not int or attempts < 1 or not reason or transient(reason)
            or reason in ("host_budget_exhausted", "robots_disallowed")):
        return None
    last_attempt = timestamp(task.get("last_attempt_at"))
    if last_attempt is None:
        return None
    return last_attempt.timestamp() + min(4 * 3600 * 2 ** min(attempts - 1, 6), 7 * 24 * 3600)


def dependency_url(task: dict, cfg: dict) -> str | None:
    if task.get("url"):
        return task["url"]
    if task["type"] == "search":
        return next(iter(cfg.get("seed_urls", [])), None)
    if task["type"] == "amazon_discovery":
        return cfg.get("source_url")
    # These adapters start with a fixed API request rather than a queued URL.
    if task["type"] == "yahoo":
        return "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
    if task["type"] == "dospara_product":
        return "https://www.dospara.co.jp/s/dospara/api/getProducts"
    return None


def runnable(queue: dict, attempted: set, cfg: dict, client, page_deadlines: dict, now: float):
    ready, waits = [], Counter()
    for task_id, task in queue.items():
        retry_at = task.get("retry_at_epoch_seconds")
        if task_id in attempted and retry_at is None:
            continue  # Permanent failure: keep pending for the next collection.
        url = dependency_url(task, cfg)
        host = urlsplit(url).hostname if url else None
        transport = getattr(client, "transport_retry_after", {}).get(host, {})
        access = getattr(client, "access_retry_after", {}).get(host, {})
        deadlines = [
            (retry_at or 0, task.get("last_error", "task_retry")),
            (page_deadlines.get(url, 0), "shared_page_retry"),
            (getattr(client, "retry_after", {}).get(host, 0), "rate_limited_retry_later"),
            (transport.get("until", 0), "transport_retry_later:" + transport.get("reason", "unknown")),
            (access.get("until", 0), "access_retry_later:" + access.get("reason", "unknown")),
        ]
        until, reason = max(deadlines, key=lambda item: item[0])
        if until > now:
            waits[host, until, reason] += 1
        else:
            ready.append((task_id, task))
    return ready, [{"host": host, "until_epoch_seconds": until, "reason": reason,
                    "affected_tasks": count} for (host, until, reason), count in waits.items()]

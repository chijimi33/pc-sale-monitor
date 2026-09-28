"""Keep temporary dependencies pending until they can actually be retried."""
from __future__ import annotations

from collections import Counter
from urllib.parse import urlsplit


def transient(reason: str) -> bool:
    return reason in {
        "RemoteDisconnected", "TimeoutError", "ConnectionResetError", "ConnectionAbortedError",
        "ConnectionRefusedError", "BrokenPipeError", "rate_limited_retry_later",
        "http_429", "http_500", "http_502", "http_503", "http_504",
    } or reason.startswith("transport_retry_later:")


def dependency_url(task: dict, cfg: dict) -> str | None:
    if task.get("url"):
        return task["url"]
    if task["type"] == "search":
        return next(iter(cfg.get("seed_urls", [])), None)
    if task["type"] == "amazon_discovery":
        return cfg.get("source_url")
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
        deadlines = [
            (retry_at or 0, task.get("last_error", "task_retry")),
            (page_deadlines.get(url, 0), "shared_page_retry"),
            (getattr(client, "retry_after", {}).get(host, 0), "rate_limited_retry_later"),
            (transport.get("until", 0), "transport_retry_later:" + transport.get("reason", "unknown")),
        ]
        until, reason = max(deadlines, key=lambda item: item[0])
        if until > now:
            waits[host, until, reason] += 1
        else:
            ready.append((task_id, task))
    return ready, [{"host": host, "until_epoch_seconds": until, "reason": reason,
                    "affected_tasks": count} for (host, until, reason), count in waits.items()]

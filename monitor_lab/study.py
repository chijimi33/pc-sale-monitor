"""Bounded transport experiment, identical request plan on Actions and Windows."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
from pathlib import Path
import time

from .acquire import Coordinator, TRANSPORTS
from .safety import digest, environment, experiment_id, guard, implementation_hash, read, write

PLAN = [
    ("koubou", "https://www.pc-koubou.jp/products/detail.php?product_id=1051336"),
    ("tsukumo", "https://shop.tsukumo.co.jp/goods/0195553309745/"),
    ("ark", "https://www.ark-pc.co.jp/i/12201487/"),
    ("tsukumo", "https://shop.tsukumo.co.jp/goods/4711289500124/"),
    ("ark", "https://www.ark-pc.co.jp/i/20300386/"),
    ("koubou", "https://www.pc-koubou.jp/products/detail.php?product_id=931040"),
]


def study(output, methods=("urllib", "pooled"), emit=lambda row: None):
    output = guard(Path(output))
    if len(set(methods)) != len(methods) or set(methods) - set(TRANSPORTS):
        raise ValueError("Each enabled method may be requested once")
    if (output / "result.json").exists():
        raise ValueError("Use a new experiment directory; results are immutable")
    host_path = output / "hosts.json"
    hosts = read(host_path) if host_path.exists() else {}
    results = []
    deadline = time.monotonic() + 2100
    started = time.perf_counter()
    identifier = experiment_id()
    coordinator = None
    for method in methods:
        transport = TRANSPORTS[method]()
        if coordinator is None:
            coordinator = Coordinator(transport, hosts=hosts, budget=max(.01, deadline - time.monotonic()), delay=2)
        else:
            coordinator.transport = transport
        try:
            for store, url in PLAN:
                if method == "browser" and store != "koubou":
                    continue
                page, receipt = coordinator.fetch(url)
                row = {"store": store, **asdict(receipt)}
                if page:
                    # Save only on the isolated volume (or transient Actions runner).
                    from .safety import atomic_bytes
                    file = output / "pages" / (digest([method, url]) + ".html")
                    atomic_bytes(file, page.body)
                    row["body_file"] = str(file)
                results.append(row)
                write(host_path, hosts)
                write(output / "receipts.partial.json", results)
                emit(row)
        finally:
            transport.close()
        if hasattr(transport, "subrequests"):
            write(output / "browser-subrequests.json", transport.subrequests)
    result = {"experiment_id": identifier, "environment": environment(), "plan_hash": digest(PLAN),
              "implementation_hash": implementation_hash(),
              "request_plan": PLAN, "methods": list(methods), "receipts": results,
              "elapsed_seconds": time.perf_counter() - started,
              "http_navigation_attempts": sum(len(r["attempts"]) for r in results),
              "successful_pages": sum(r["status"] == 200 and not r["error"] for r in results),
              "affected_tasks": sum(bool(r["error"]) for r in results), "mode": "live",
              "limits": ["One bounded observation, not a stability sample", "Sequential method order may confound timing",
                         "Shared host wait may prevent a second method; a skipped request is not a failed HTTP request",
                         "Windows pages are not eligible production prices", "No notification or formal audit"]}
    write(output / "result.json", result)
    return result

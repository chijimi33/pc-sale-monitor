"""Bounded transport experiment, identical request plan on Actions and Windows."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import math
import time

from .acquire import Coordinator, TRANSPORTS
from .capture import Capture, verify_capture
from .request_plan import load_plan, scope_report
from .safety import digest, environment, experiment_id, guard, implementation_hash, read

PLAN = [
    ("koubou", "https://www.pc-koubou.jp/products/detail.php?product_id=1051336"),
    ("tsukumo", "https://shop.tsukumo.co.jp/goods/0195553309745/"),
    ("ark", "https://www.ark-pc.co.jp/i/12201487/"),
    ("tsukumo", "https://shop.tsukumo.co.jp/goods/4711289500124/"),
    ("ark", "https://www.ark-pc.co.jp/i/20300386/"),
    ("koubou", "https://www.pc-koubou.jp/products/detail.php?product_id=931040"),
]


def study(output, methods=("urllib", "pooled"), emit=lambda row: None, *, plan=None, followup=None, comparison=None, budget=2100):
    output = guard(Path(output))
    if not methods or len(set(methods)) != len(methods) or set(methods) - set(TRANSPORTS):
        raise ValueError("Each enabled method may be requested once")
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or not 0 < budget <= 2100:
        raise ValueError('Invalid shared study budget')
    identifier = experiment_id()
    settings = read(Path(__file__).resolve().parents[1] / "config/sources.json")
    if sum(x is not None for x in (plan, followup, comparison)) > 1:
        raise ValueError('Choose one acquisition intent, not both an explicit plan and another follow-up/comparison intent')
    intent = None
    if followup is not None:
        from .followup import load_followup
        intent = load_followup(followup, settings['stores'])
    if comparison is not None:
        from .comparison import load_comparison
        intent = load_comparison(comparison, settings['stores'])
    scope = intent['request_plan'] if intent else load_plan(plan, settings['stores']) if plan is not None else None
    if scope and 'browser' in methods and any(row['store'] != 'koubou' for row in scope['resources']):
        raise ValueError('Browser scope is currently limited to Koubou')
    request_plan = [(row['store'], row['url']) for row in scope['resources']] if scope else PLAN
    kinds = {row['url']: row['kind'] for row in scope['resources']} if scope else {}
    metadata = {"experiment_id": identifier, "mode": "live", "environment": environment(),
                "plan_hash": scope['plan_hash'] if scope else digest(PLAN), "request_plan": request_plan, "methods": list(methods),
                "implementation_hash": implementation_hash(),
                "implementation_hash_lf": implementation_hash(normalize_line_endings=True)}
    if scope:
        metadata.update(scope_plan=scope, budget_seconds=budget)
    if intent and comparison is None:
        from .followup_provenance import retain_followup, validate_provenance
        metadata['followup'] = validate_provenance(retain_followup(intent), settings['stores'], scope)
    elif intent:
        from .comparison_provenance import retain_comparison, validate_comparison
        metadata['comparison'] = validate_comparison(retain_comparison(intent), settings['stores'], scope)
    capture = Capture(output, metadata, settings)
    hosts = deepcopy(intent['inherited_host_gates']) if intent else {}
    results = []
    deadline = time.monotonic() + budget
    started = time.perf_counter()
    coordinator = None
    for method in methods:
        transport = TRANSPORTS[method]()
        if coordinator is None:
            coordinator = Coordinator(transport, hosts=hosts, budget=max(.01, deadline - time.monotonic()), delay=2,
                                      capture=capture.body)
        else:
            coordinator.transport = transport
        try:
            for store, url in request_plan:
                if method == "browser" and store != "koubou":
                    continue
                page, receipt = coordinator.fetch(url)
                row = {"store": store, **asdict(receipt)}
                if scope:
                    row['resource_kind'] = kinds[url]
                results.append(row)
                capture.append(row, hosts)
                emit(row)
        except BaseException as exc:
            capture.checkpoint(failure={"type": type(exc).__name__, "completed_receipts": len(results)})
            raise
        finally:
            transport.close()
        if hasattr(transport, "subrequests"):
            from .safety import write
            write(output / "browser-subrequests.json", transport.subrequests)
    result = {"experiment_id": identifier, "environment": environment(), "plan_hash": metadata['plan_hash'],
              "implementation_hash": implementation_hash(),
              "implementation_hash_lf": metadata["implementation_hash_lf"],
              "request_plan": request_plan, "methods": list(methods), "receipts": results,
              "elapsed_seconds": time.perf_counter() - started,
              "http_navigation_attempts": sum(len(r["attempts"]) for r in results),
              "successful_pages": sum(r["status"] == 200 and not r["error"] for r in results),
              "affected_tasks": sum(bool(r["error"]) for r in results), "mode": "live",
              "limits": ["One bounded observation, not a stability sample", "Sequential method order may confound timing",
                         "Shared host wait may prevent a second method; a skipped request is not a failed HTTP request",
                         "Windows pages are not eligible production prices", "No notification or formal audit"]}
    if scope:
        result['scope'] = scope_report(scope, results)
    capture.checkpoint(result=result)
    verify_capture(output)
    return result

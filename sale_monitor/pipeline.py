"""Coordinate durable discovery, comparison planning and publication."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import comparison_integrity, migrate, planner, reporting, routes
from .budget import HostBudget, Robots
from .http import Client
from .models import STORES, Offer, fresh, iso, utcnow
from .runner import Collector, record_unverified_discoveries
from .storage import Store, read_json


DEADLINE_GRACE = 120
TERM_GRACE = 5
USER_AGENT = "PCSaleMonitor/0.1 (+https://github.com/chijimi33/pc-sale-monitor)"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_robots(url):
    """Fetch one policy without retries or redirect navigation."""
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=15) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except HTTPError as exc:
        with exc:
            return exc.code, exc.read().decode("utf-8", "replace")


def _channel(store):
    return {"yahoo": "api", "amazon": "chimolog"}.get(store, "direct")


def _validate_run_id(run_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
        raise ValueError("invalid_run_id")


def saved_budget_use(saved: dict, run_id: str, phase: str) -> dict | None:
    """Resume only the same stage of the same run; each stage has its own budget."""
    previous = saved.get("budget", {})
    if previous.get("run_id") == run_id and previous.get("phase") == phase:
        return previous.get("used")
    return None


def stage(state_root, store, phase, run_id, seconds, config_path, routes_path, plan_path=None) -> dict:
    """Run one active store in the child process, preserving its checkpoint."""
    _validate_run_id(run_id)
    if phase not in ("discovery", "verification"):
        raise ValueError("invalid_phase")
    disk = Store(Path(state_root))
    route_state = routes.RouteState(disk, routes.load_config(routes_path))
    function = "sale_discovery" if phase == "discovery" else "product_verify"
    if not route_state.is_active(store, function, _channel(store)):
        return {"store": store, "phase": phase, "status": "skipped"}
    config = read_json(Path(config_path), None)
    saved = disk.load(f"stores/{store}.json", {})
    budget = HostBudget(route_state.config["hosts"], seconds, used=saved_budget_use(saved, run_id, phase))
    policies = saved.get("robots", {})
    responses = dict(policies.get("responses", {})) if policies.get("run_id") == run_id else {}

    def fetch_policy(url):
        host = urlsplit(url).hostname
        try:
            response = _fetch_robots(url)
        except Exception:
            responses[host] = [503, ""]
            raise
        responses[host] = list(response)
        return response

    robots = Robots(fetch=fetch_policy, budget=budget)
    cached = Robots(fetch=lambda url: responses[urlsplit(url).hostname])
    for host in responses:
        cached.allowed(f"https://{host}/")
        budget.set_crawl_delay(host, cached.crawl_delay(host))
    robots.cache.update(cached.cache)
    robots.responses = responses
    client = Client(browser=config["stores"][store].get("browser_fallback", False), budget=budget, robots=robots)
    comparison_plan = read_json(Path(plan_path), None) if plan_path is not None else None
    if phase == "verification":
        if comparison_plan is None or comparison_plan.get("run_id") != run_id:
            raise ValueError("verification_plan_required")
        if not route_state.is_active(store, "comparison_lookup", _channel(store)):
            comparison_plan.setdefault("explore", {})[store] = []
    collector = Collector(Path(state_root), store, config, run_id, client=client,
                          phase=phase, plan=comparison_plan)
    return collector.collect(seconds)


def _signal_owned_process_group(process, sig):
    """Signal a POSIX group only with explicit pipeline ownership."""
    if (not isinstance(process, subprocess.Popen) or not getattr(process, "_pipeline_owned_group", False)
            or process.pid == os.getpgrp()):
        return False
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass
    return True


def _kill_remaining_process_group(process):
    """Consume group cleanup once, immediately after detecting child exit."""
    if (not isinstance(process, subprocess.Popen) or not getattr(process, "_pipeline_owned_group", False)
            or getattr(process, "_pipeline_group_cleaned", False)):
        return
    process._pipeline_group_cleaned = True
    _signal_owned_process_group(process, getattr(signal, "SIGKILL", 9))


def _await_leader_exit(process, seconds):
    """Give TERM time to close browsers, without reaping the leader's PID."""
    waitid = getattr(os, "waitid", None)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if waitid is None:
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
            continue
        try:
            if waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None:
                return
        except (ChildProcessError, OSError):
            return
        time.sleep(0.1)


def _stop_posix_process_tree(process):
    """Stop an owned group before reaping its leader, or just the supplied child."""
    if not getattr(process, "_pipeline_group_cleaned", False) and _signal_owned_process_group(process, signal.SIGTERM):
        # Do not reap the leader before the final signal: its PID could be reused.
        process._pipeline_group_cleaned = True
        _await_leader_exit(process, TERM_GRACE)
        _signal_owned_process_group(process, getattr(signal, "SIGKILL", 9))
    else:
        terminate = getattr(process, "terminate", None)
        kill = getattr(process, "kill", None)
        if terminate is not None or kill is not None:
            (terminate or kill)()
        wait = getattr(process, "wait", None)
        if wait is not None:
            try:
                wait(timeout=1)
            except subprocess.TimeoutExpired:
                if kill is not None:
                    kill()


def _stop_process_tree(process):
    """Stop only the isolated process tree owned by this pipeline."""
    if os.name == "posix" or not isinstance(process, subprocess.Popen):
        _stop_posix_process_tree(process)
    else:
        result = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                capture_output=True, timeout=15)
        if result.returncode and getattr(process, "_pipeline_job", None) is None and process.poll() is None:
            raise RuntimeError("process_tree_stop_failed")
        _close_job(process)
    wait = getattr(process, "wait", None)
    if wait is not None:
        wait(timeout=5)


def _job_api():
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, arguments, result in (
        ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
    ):
        function = getattr(kernel, name)
        function.argtypes = arguments
        function.restype = result
    return kernel


def _attach_job(process):
    """Retain owned Windows descendants even when process enumeration is denied."""
    if os.name != "nt" or not isinstance(process, subprocess.Popen):
        return
    import ctypes
    kernel = _job_api()
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        process.kill()
        process.wait(timeout=5)
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel.AssignProcessToJobObject(job, int(process._handle)):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel.CloseHandle(job)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
            raise error
        return
    process._pipeline_job = job
    # Creation is suspended so even a venv launcher cannot start descendants
    # before joining the job. Resume through its owned process handle.
    resume = ctypes.WinDLL("ntdll").NtResumeProcess
    resume.argtypes = [ctypes.c_void_p]
    resume.restype = ctypes.c_long
    if resume(int(process._handle)) != 0:
        _close_job(process)
        process.wait(timeout=5)
        raise RuntimeError("process_resume_failed")


def _close_job(process):
    job = getattr(process, "_pipeline_job", None)
    if job is not None:
        import ctypes
        kernel = _job_api()
        if not kernel.TerminateJobObject(job, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        kernel.CloseHandle(job)
        process._pipeline_job = None


def _finish_interrupted_verification(disk, store, run_id):
    state = disk.load(f"stores/{store}.json", {})
    verification = state.get("verification", {})
    if verification.get("run_id") == run_id:
        record_unverified_discoveries(state)
        verification["not_reached"] = verification.get("not_reached", 0) + sum(
            key not in verification["results"] for key in state.get("verification_queue", {}))
        state["verification_queue"] = {}
        disk.save(f"stores/{store}.json", state)


def _run_children(stores, phase, seconds, root, run_id, config_path, routes_path, spawn, clock, plan_path=None):
    processes, results = {}, {}
    deadline = clock() + seconds + DEADLINE_GRACE
    options = {"start_new_session": True} if os.name == "posix" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004}  # CREATE_SUSPENDED
    try:
        for store in stores:
            command = [sys.executable, "-m", "sale_monitor.cli", "stage", "--phase", phase,
                       "--store", store, "--state", str(root), "--run-id", run_id,
                       "--seconds", str(seconds), "--config", str(config_path), "--routes", str(routes_path)]
            if plan_path is not None:
                command.extend(["--plan", str(plan_path)])
            try:
                processes[store] = spawn(command, **options)
                process = processes[store]
                if os.name == "posix" and isinstance(process, subprocess.Popen):
                    # A replacement spawn may ignore start_new_session.
                    try:
                        process._pipeline_owned_group = os.getpgid(process.pid) == process.pid and process.pid != os.getpgrp()
                    except ProcessLookupError:
                        process._pipeline_owned_group = False
                _attach_job(processes[store])
            except OSError as exc:
                results[store] = {"status": "failed", "exit_code": None, "reason": type(exc).__name__}
        pending = dict(processes)
        while pending:
            for store, process in list(pending.items()):
                code = process.poll()
                if code is not None:
                    _kill_remaining_process_group(process)
                    _close_job(process)
                    results[store] = {"status": "completed" if code == 0 else "failed", "exit_code": code}
                    del pending[store]
                elif clock() >= deadline:
                    _stop_process_tree(process)
                    results[store] = {"status": "deadline", "exit_code": process.returncode}
                    del pending[store]
            if pending:
                time.sleep(0.05)
    except BaseException:
        for process in processes.values():
            if process.poll() is None:
                _stop_process_tree(process)
        raise
    if phase == "verification":
        for store, result in results.items():
            if result["status"] != "completed":
                _finish_interrupted_verification(Store(root), store, run_id)
    return results


def _observations(disk, run_id, now):
    states = {store: disk.load(f"stores/{store}.json", {}) for store in STORES}
    current, changes = [], {}
    for state in states.values():
        changes.update(state.get("changes", {}))
        for row in state.get("offers", {}).values():
            offer = Offer.from_dict(row)
            if offer.observed_run_id == run_id and fresh(offer.observed_at, now) and "latest_fetch_failed" not in offer.issues:
                current.append(offer)
    return states, current, changes


def run(state_root: Path, public: Path, run_id: str, *, config_path=Path("config/sources.json"),
        routes_path=Path("config/routes.json"), now=None, spawn=None, clock=time.monotonic) -> dict:
    """Execute A through D, retaining all durable work if publication fails."""
    _validate_run_id(run_id)
    started = clock()
    fixed_now = datetime.fromtimestamp(now, timezone.utc) if isinstance(now, (int, float)) else now
    now = fixed_now or utcnow()
    started_at = iso(now)
    root = Path(state_root)
    disk = Store(root)
    config = read_json(Path(config_path), None)
    routes_config = routes.load_config(routes_path)
    if disk.load("schema.json", {}).get("state_schema_version") != migrate.STATE_SCHEMA_VERSION:
        raise RuntimeError("state_migration_required")
    route_state = routes.RouteState(disk, routes_config, now)
    for route in route_state.probes_due(now):
        result = routes.probe(Client(browser=False), route["route_id"], route)
        route_state.record_probe(route["route_id"], result, now)
    route_state.save()
    spawn = spawn or subprocess.Popen
    run_config = routes_config["run"]
    discovery_seconds = run_config["discovery_minutes"] * 60
    verification_seconds = run_config["verification_minutes"] * 60
    stage_results = {}
    for store, result in _run_children(route_state.active_stores("sale_discovery"), "discovery", discovery_seconds,
                                      root, run_id, config_path, routes_path, spawn, clock).items():
        stage_results.setdefault(store, {})["discovery"] = result

    now = fixed_now or utcnow()
    states, current, changes = _observations(disk, run_id, now)
    historical = reporting.historical_rows(disk, states, run_id)
    known = comparison_integrity.known_comparators(states, historical, now)
    _, history_by_identity = reporting.comparison_inputs_by_identity(current, historical)
    scope = route_state.scope()
    scope_stores = scope["in_scope_stores"]
    verifiable_stores = scope["verifiable_stores"]
    lookup_stores = [store for store in scope_stores if route_state.is_active(store, "comparison_lookup", _channel(store))]
    budget = HostBudget(routes_config["hosts"], verification_seconds)
    budgets = {store: budget.limit("www.dospara.co.jp" if store == "dospara" else
               urlsplit(config["stores"][store]["seed_urls"][0]).hostname) for store in verifiable_stores}
    tracking = disk.load("comparison_tracking.json", {})
    comparison_plan = planner.plan([o for o in current if o.discovery_kind != "comparison" and o.channel == "online"],
        current, known, history_by_identity, scope_stores, lookup_stores, budgets, tracking, changes, now,
        run_id=run_id, scope_version=scope["version"], exploration_share=run_config.get("exploration_share", 0.2),
        verifiable_stores=verifiable_stores)
    plan_path = root / "plans" / f"{run_id}.json"
    disk.save(f"plans/{run_id}.json", comparison_plan)

    verification_stores = [store for store in route_state.active_stores("product_verify") if
        comparison_plan["confirm"].get(store) or comparison_plan["explore"].get(store)]
    if run_config["max_minutes"] * 60 - (clock() - started) < 300:
        for store in verification_stores:
            stage_results.setdefault(store, {})["verification"] = {"status": "skipped", "reason": "run_time_remaining_under_five_minutes"}
    else:
        for store, result in _run_children(verification_stores, "verification", verification_seconds,
                root, run_id, config_path, routes_path, spawn, clock, plan_path).items():
            stage_results.setdefault(store, {})["verification"] = result

    now = fixed_now or utcnow()
    states, current, _ = _observations(disk, run_id, now)
    failed, searched, discoveries = {}, {}, {}
    for store, state in states.items():
        verification = state.get("verification", {})
        if verification.get("run_id") == run_id:
            failed[store] = set(verification.get("failed_urls", []))
            searched[store] = set(verification.get("searched_identities", []))
            # Fetched pages are passed with their target identity: tracking
            # decides from the observation whether the obligation is settled.
            discoveries[store] = {"unverified": verification.get("unverified_discoveries", []),
                                  "verified": [{"identity": identity, "url": row["url"],
                                                "offer_key": row.get("observed_offer_key")}
                                               for row in verification.get("results", {}).values()
                                               if row.get("type") == "product" and row.get("result") == "ok"
                                               and row.get("url")
                                               for identity in sorted(set(row.get("plan_identities", [])) |
                                                   ({row["plan_identity"]} if row.get("plan_identity") else set()))]}
    disk.save("comparison_tracking.json", planner.update_tracking(tracking, comparison_plan, current, failed, searched, now,
                                                                  discoveries=discoveries))
    summary = route_state.public_summary()
    scope["routes"] = {route_id: {**row, "store": route_state.routes[route_id]["store"],
        "channel": route_state.routes[route_id]["channel"],
        "functions": {name: function["status"] for name, function in row["functions"].items()}}
        for route_id, row in summary.items()}
    scope["paused_stores"] = {store: route_state.status(f"{store}:{_channel(store)}", "sale_discovery")
        for store in STORES if not route_state.is_active(store, "sale_discovery", _channel(store))}
    scope["run"] = {"stage_results": stage_results, "plan_summary": comparison_plan["summary"],
                    "started_at": started_at, "elapsed_seconds": clock() - started}
    return reporting.aggregate(root, Path(public), run_id, now, scope=scope)

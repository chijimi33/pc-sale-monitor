"""Run a ticket using only its transferred inputs."""
from copy import deepcopy
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

from lxml.etree import ParserError

from ..budget import HostBudget, Robots
from ..http import Client, FetchError
from ..pipeline import saved_budget_use
from ..runner import Collector
from ..storage import atomic_json, read_json
from .control import ControlState
from .deadline import Handshake, deadline_clock as system_deadline_clock
from .guard import CommunicationForbidden, Guard
from . import protocol, validation


COUNTERS = ("requests", "ordinary_attempted", "ordinary_succeeded", "no_response", "refusal")
STOP_REASONS = {"deadline", "killed", "refused", "retry_after", "no_response", "error"}


def atomic_bytes(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def route_active(routes, store, phase):
    function = "sale_discovery" if phase == "discovery" else "product_verify"
    route = routes.get("routes", {}).get(store + ":direct", {})
    return (route.get("store") == store and route.get("channel") == "direct"
            and route.get("executor", "satellite") == "satellite"
            and route.get("functions", {}).get(function, {}).get("status") == "active")


def check_inputs(ticket, inputs):
    """Preflight configuration and state; queued URLs are checked per request."""
    store = ticket["store"]
    config = json.loads(inputs["input/config.json"])
    interval = validation.validate_effective_config(config, store)
    protocol.require(config["config_revision"] == ticket["config_revision"], "config_revision")
    routes = json.loads(inputs["input/routes.json"])
    protocol.bounded(routes)
    protocol.require(route_active(routes, store, ticket["phase"]), "route_inactive")
    function = "sale_discovery" if ticket["phase"] == "discovery" else "product_verify"
    protocol.require(config["route"]["functions"].get(function, {}).get("status") == "active", "route_inactive")
    saved = validation._load_state(inputs[f"input/stores/{store}.json"])
    protocol.require(saved["store"] == store, "foreign_store")
    validation._walk_urls({key: value for key, value in saved.items()
                           if key not in {"queue", "verification_queue"}}, store)
    for name in ("queue", "verification_queue"):
        for task in saved.get(name, {}).values():
            protocol.require(task.get("type") in {"list", "product"}, "unsupported_task")
            protocol.require("url" in task, "missing_task_url")
    plan = None
    if ticket["phase"] == "verification":
        plan = json.loads(inputs["input/plan.json"])
        protocol.bounded(plan)
        protocol.keys(plan, {"run_id", "confirm"}, {"explore"})
        protocol.require(plan["run_id"] == ticket["run_id"], "plan_run")
        protocol.keys(plan["confirm"], (), {store})
        protocol.keys(plan.get("explore", {}), (), {store})
        protocol.require(not plan.get("explore", {}).get(store), "satellite_search_disabled")
        for row in plan["confirm"].get(store, []):
            protocol.keys(row, {"url", "offer_key", "identity"}, {"for_candidates"})
            for key in ("url", "offer_key", "identity"):
                protocol.string(row[key])
        validation._walk_urls(plan, store)
    return config, saved, plan, interval


class _StageStop(BaseException):
    """Escape Collector and robots' ordinary fetch-error handlers."""


class _StageCollector(Collector):
    def collect(self, *args, **kwargs):
        self._collecting = True
        try:
            return super().collect(*args, **kwargs)
        finally:
            self._collecting = False

    def process(self, task):
        guard = self.client.guard
        original = deepcopy(task)
        try:
            return super().process(task)
        except _StageStop:
            # No failure receipt for work interrupted before a request.
            task.clear()
            task.update(original)
            raise
        except (FetchError, ParserError) as exc:
            if guard.communication_forbidden:
                # Robots may have swallowed the guard's real request outcome.
                raise CommunicationForbidden(guard.reason) from exc
            raise
        except Exception as exc:
            raise _StageStop("error") from exc

    def save(self):
        super().save()
        if getattr(self, "_collecting", False) and self.client.guard.communication_forbidden:
            # Save the actual failed request first, then stop before selecting
            # another task. Final run_stage saving happens outside collect().
            raise _StageStop(self.client.guard.reason)


class _PolicyGuard(Guard):
    def check_url(self, url):
        if self.communication_forbidden:
            raise _StageStop(self.reason)
        super().check_url(url)

    def before_request(self, url):
        try:
            super().before_request(url)
        except CommunicationForbidden:
            if self.communication_forbidden:
                raise _StageStop(self.reason)
            raise

    def fetch_robots(self, url):
        host = urlsplit(url).hostname
        try:
            response = super().fetch_robots(url)
        except Exception:
            self.responses[host] = [503, ""]
            raise
        self.responses[host] = list(response)
        return response


def run_stage(work_dir, *, deadline_clock=None, wall_clock=time.time,
              sleep=time.sleep, client_factory=Client, robots_transport=None):
    """Return an exit code; checkpoints survive every exit path."""
    work = Path(work_dir)
    guard = collector = None
    reason = "error"
    counters = dict.fromkeys(COUNTERS, 0)
    try:
        ticket = protocol.validate_ticket(read_json(work / "ticket.json", None))
        store, run_id, phase = (ticket[key] for key in ("store", "run_id", "phase"))
        state_root = work / "state"
        state_path = state_root / "stores" / (store + ".json")
        state_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_bytes(state_path, (work / "input" / "stores" / (store + ".json")).read_bytes())
        (state_root / "routes.json").write_bytes((work / "input/routes.json").read_bytes())
        inputs = {path: (work / path).read_bytes() for path in ticket["input_sha256"]}
        config, saved, plan, interval = check_inputs(ticket, inputs)
        clock = deadline_clock or system_deadline_clock()
        handshake = Handshake.from_dict(read_json(work / "handshake.json", None),
                                        deadline_clock=clock, wall_clock=wall_clock)
        protocol.require(handshake.ticket_commit == work.name, "handshake_ticket")
        context = read_json(work / "context.json", {})
        control = ControlState(context.get("home", work.parent.parent))

        def progress(current):
            atomic_json(work / "stage_result.json", {"reason": current.reason,
                        "counters": current.counters})

        guard = _PolicyGuard(store, control, handshake, ticket["collect_until"],
            min_interval_seconds=interval, ticket=ticket, input_retry_after=saved.get("retry_after"),
            wall_clock=wall_clock, sleep=sleep, robots_transport=robots_transport, progress=progress)
        hosts = deepcopy(config["hosts"])
        hosts[protocol.ALLOWED_HOSTS[store]]["min_interval_seconds"] = interval
        budget = HostBudget(hosts, ticket["seconds"], used=saved_budget_use(saved, run_id, phase),
                            sleep=sleep, clock=clock)
        policies = saved.get("robots", {})
        responses = dict(policies.get("responses", {})) if policies.get("run_id") == run_id else {}
        protocol.require(set(responses) <= {protocol.ALLOWED_HOSTS[store]}, "foreign_robots_host")
        guard.responses = responses
        robots = Robots(fetch=guard.fetch_robots, budget=budget)
        cached = Robots(fetch=lambda url: responses[urlsplit(url).hostname])
        for host in responses:
            cached.allowed(f"https://{host}/")
            budget.set_crawl_delay(host, cached.crawl_delay(host))
        robots.cache.update(cached.cache)
        robots.responses = responses
        client = client_factory(browser=False, budget=budget, robots=robots, guard=guard)
        collector_config = {key: deepcopy(config[key]) for key in ("schema_version", "stores", "seller_aliases")}
        collector = _StageCollector(state_root, store, collector_config, run_id, client=client, phase=phase, plan=plan)
        seconds = max(0, protocol.epoch(ticket["collect_until"]) - handshake.actions_now_upper())
        collector.collect(seconds)
        reason = "completed"
    except _StageStop as exc:
        reason = str(exc)
    except Exception:
        reason = "error"
    finally:
        if collector is not None:
            try:
                collector.save()
            except Exception:
                reason = "error"
        if guard is not None:
            counters = guard.counters
        if reason in {"clock_unverified", "clock_skew"}:
            reason = "deadline"
        if reason != "completed" and reason not in STOP_REASONS:
            reason = "error"
        status = "completed" if reason == "completed" else "stopped:" + reason
        atomic_json(work / "stage_result.json", {"reason": reason, "status": status, "counters": counters,
                    "guard_reason": guard.reason if guard is not None else None})
    return 0 if reason == "completed" else 1

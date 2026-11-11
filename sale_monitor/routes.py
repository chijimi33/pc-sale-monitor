from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from .http import Client, FetchError
from .http_evidence import retry_after_evidence
from .models import iso, timestamp
from .storage import Store


STATUSES = {"active", "paused_maintenance", "paused_refused", "paused_unreachable",
            "paused_configuration", "not_implemented"}
RECOVERABLE = {"paused_maintenance", "paused_unreachable"}


def _now(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(value, timezone.utc) if isinstance(value, (int, float)) else value


def load_config(path) -> dict:
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
        if type(config["schema_version"]) is not int or config["schema_version"] != 1:
            raise ValueError("invalid_schema_version")
        if type(config["config_revision"]) is not int or config["config_revision"] < 1:
            raise ValueError("invalid_config_revision")
        if type(config["scope"]["version"]) is not int or not isinstance(config["scope"]["in_scope"], list):
            raise ValueError("invalid_scope")
        routes = config["routes"]
        if not isinstance(routes, dict) or any(route not in routes for route in config["scope"]["in_scope"]):
            raise ValueError("unknown_scope_route")
        for route in routes.values():
            if not isinstance(route["store"], str) or not isinstance(route["channel"], str):
                raise ValueError("invalid_route")
            if not isinstance(route["functions"], dict):
                raise ValueError("invalid_functions")
            for function in route["functions"].values():
                if function["status"] not in STATUSES:
                    raise ValueError("invalid_route_status")
        return config
    except (OSError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError("invalid_routes_config") from exc


class RouteState:
    def __init__(self, disk: Store, config: dict, now=None):
        self.disk = disk
        self.config = deepcopy(config)
        saved = disk.load("routes.json", {})
        reuse = saved.get("config_revision", 0) == config["config_revision"]
        self.routes = {}
        for route_id, configured in self.config["routes"].items():
            route = deepcopy(configured)
            previous = saved.get("routes", {}).get(route_id, {}) if reuse else {}
            for name, function in route["functions"].items():
                function.setdefault("since", iso(_now(now)))
                old = previous.get("functions", {}).get(name, {})
                base = function["status"]
                target = old.get("status")
                valid_transition = base in RECOVERABLE and target == "paused_refused"
                if old.get("automatic") and old.get("config_status") == base and valid_transition:
                    function.update(deepcopy(old))
                elif target == base:
                    function["since"] = old.get("since", function["since"])
            for key in ("recovery_candidate", "last_probe", "probe_log", "consecutive_successes", "retry_after_until"):
                if key in previous:
                    route[key] = deepcopy(previous[key])
            self.routes[route_id] = route

    def status(self, route_id, function) -> str:
        return self.routes.get(route_id, {}).get("functions", {}).get(function, {}).get("status", "not_implemented")

    def is_active(self, store, function, channel="direct") -> bool:
        return any(route["store"] == store and route["channel"] == channel and
                   self.status(route_id, function) == "active" for route_id, route in self.routes.items())

    def active_stores(self, function) -> list[str]:
        return list(dict.fromkeys(route["store"] for route_id, route in self.routes.items()
                                 if route["channel"] == "direct" and self.status(route_id, function) == "active"))

    def scope(self) -> dict:
        scope = deepcopy(self.config["scope"])
        scope["in_scope_stores"] = list(dict.fromkeys(self.routes[route_id]["store"] for route_id in scope["in_scope"]))
        scope["verifiable_stores"] = list(dict.fromkeys(self.routes[route_id]["store"] for route_id in scope["in_scope"]
                                                     if self.status(route_id, "product_verify") == "active"))
        scope["out_of_scope"] = {route_id: {
            "status": {name: function["status"] for name, function in route["functions"].items()},
            "reason": route.get("reason", "outside_comparison_scope"),
        } for route_id, route in self.routes.items() if route_id not in scope["in_scope"]}
        return scope

    def probes_due(self, now) -> list[dict]:
        epoch = _now(now).timestamp()
        due = []
        for route_id, route in self.routes.items():
            states = {function["status"] for function in route["functions"].values()}
            if not route.get("probe") or not states & RECOVERABLE or states & {"paused_refused", "paused_configuration"}:
                continue
            last = route.get("last_probe")
            previous = timestamp(last["checked_at"]).timestamp() if last else None
            if epoch < (route.get("retry_after_until") or 0):
                continue
            if previous is None or epoch - previous >= route["probe"]["interval_hours"] * 3600:
                due.append({"route_id": route_id, **deepcopy(route)})
        return due

    def record_probe(self, route_id, result: dict, now):
        route = self.routes[route_id]
        checked_at = iso(_now(now))
        successes = route.get("consecutive_successes", 0) + 1 if result["ok"] and not result["explicit_refusal"] else 0
        route["consecutive_successes"] = successes
        if successes and successes >= route.get("probe", {}).get("successes_to_resume", 2):
            route["recovery_candidate"] = {
                "since": route.get("recovery_candidate", {}).get("since", checked_at),
                "consecutive_successes": successes, "last_ok_at": checked_at, "evidence": deepcopy(result),
            }
        else:
            route.pop("recovery_candidate", None)
        for name, function in route["functions"].items():
            previous = function["status"]
            target = previous
            if result["explicit_refusal"] and previous in RECOVERABLE:
                target = "paused_refused"
            if target != previous:
                function.update(status=target, since=checked_at, automatic=True,
                                config_status=self.config["routes"][route_id]["functions"][name]["status"],
                                reason=result["summary"], evidence=deepcopy(result))
        if result.get("retry_after_until") is not None:
            route["retry_after_until"] = max(route.get("retry_after_until") or 0, result["retry_after_until"])
        record = {**deepcopy(result), "checked_at": checked_at}
        route["last_probe"] = record
        route["probe_log"] = (route.get("probe_log", []) + [record])[-20:]

    def save(self):
        self.disk.save("routes.json", {"schema_version": 1, "config_revision": self.config["config_revision"],
                                      "routes": self.routes})

    def public_summary(self) -> dict:
        return {route_id: {
            "functions": {name: {**deepcopy(function), "reason": function.get("reason", route.get("reason", "")),
                                 "evidence": deepcopy(function.get("evidence", route.get("evidence", {})))}
                          for name, function in route["functions"].items()},
            "reason": route.get("reason", ""), "evidence": deepcopy(route.get("evidence", {})),
            "since": min((function["since"] for function in route["functions"].values()), default=None),
            "last_probe": deepcopy(route.get("last_probe")),
            "recovery_candidate": deepcopy(route.get("recovery_candidate")),
        } for route_id, route in self.routes.items()}


def probe(client, route_id, route_cfg) -> dict:
    result = {"ok": False, "explicit_refusal": False, "http_status": None,
              "summary": "probe_failed", "retry_after_until": None}
    try:
        url = route_cfg["probe"]["url"]
        try:
            page = client.get(url, max_attempts=1) if isinstance(client, Client) else client.get(url)
        except FetchError as exc:
            if exc.page is None:
                result["explicit_refusal"] = str(exc) == "http_403"
                result["http_status"] = 403 if result["explicit_refusal"] else None
                result["summary"] = str(exc)
                return result
            page = exc.page
        result["http_status"] = page.status
        result["explicit_refusal"] = page.status == 403 or "access denied" in page.text.lower()
        result["ok"] = page.status == 200 and not result["explicit_refusal"] and not any(
            text in page.text for text in route_cfg["probe"].get("must_not_contain", []))
        result["summary"] = "probe_ok" if result["ok"] else "explicit_refusal" if result["explicit_refusal"] else "probe_failed"
        retry = retry_after_evidence(next((value for key, value in page.headers.items() if key.lower() == "retry-after"), None))
        if retry["kind"] == "seconds":
            result["retry_after_until"] = time.time() + max(0, retry["seconds"])
        elif retry["kind"] == "http_date":
            result["retry_after_until"] = timestamp(retry["at"]).timestamp()
    except Exception as exc:
        result["summary"] = type(exc).__name__
    return result

"""Versioned wire schemas. Hashes always describe transferred bytes."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import re

PROTOCOL_VERSION = 1
ACK_WINDOW_SECONDS = 180
ECHO_WAIT_SECONDS = 120
COLLECT_MARGIN_SECONDS = 90
KILL_AFTER_SECONDS = 30
MIN_INTERVAL_FLOOR_SECONDS = 5
NO_RESPONSE_LIMIT = 5
UNREACHABLE_RUN_LIMIT = 3
SKEW_LIMIT_SECONDS = 30
DRIFT_LIMIT_SECONDS = 5
ALLOWED_HOSTS = {"tsukumo": "shop.tsukumo.co.jp"}
# The coordinator writes the relay branch; the owner's PC writes the satellite branch.
RELAY_BRANCH = "monitor-relay"
SATELLITE_BRANCH = "monitor-satellite"
REMOTE_URL = "https://github.com/chijimi33/pc-sale-monitor.git"
MAX_DOCUMENT_BYTES = 1_048_576
MAX_ITEMS = 10_000
MAX_STRING = 8192
STAGE_STATUSES = {"satellite_absent", "satellite_deadline", "relay_failed", "completed",
                  "skipped:agent_unregistered"}


def registered_agent(route):
    value = route.get("satellite_agent")
    return value if type(value) is str and re.fullmatch(r"[0-9a-f]{32}", value) else None


class ValidationError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def require(condition, reason):
    if not condition:
        raise ValidationError(reason)


def sha256_bytes(data: bytes) -> str:
    require(type(data) is bytes, "invalid_bytes")
    return hashlib.sha256(data).hexdigest()


def bounded(value, *, limit=MAX_DOCUMENT_BYTES, max_items=MAX_ITEMS, max_string=MAX_STRING):
    """Reject non-JSON values, deep nesting and oversized documents."""
    def visit(item, depth):
        require(depth <= 32, "too_deep")
        if type(item) is dict:
            require(len(item) <= max_items, "too_many_items")
            for key, child in item.items():
                require(type(key) is str and len(key) <= max_string, "invalid_key")
                visit(child, depth + 1)
        elif type(item) is list:
            require(len(item) <= max_items, "too_many_items")
            for child in item:
                visit(child, depth + 1)
        elif type(item) is str:
            require(len(item) <= max_string, "string_too_long")
        elif type(item) is int:
            require(abs(item) <= 2**53, "invalid_number")
        elif type(item) is float:
            require(math.isfinite(item), "invalid_number")
        else:
            require(item is None or type(item) is bool, "invalid_type")
    visit(value, 0)
    try:
        size = len(json.dumps(value, ensure_ascii=False).encode())
    except (ValueError, UnicodeError) as exc:
        raise ValidationError("invalid_json") from exc
    require(size <= limit, "too_large")


def keys(value, required, optional=()):
    require(type(value) is dict, "invalid_object")
    require(set(required) <= value.keys(), "missing_keys")
    require(value.keys() <= set(required) | set(optional), "unknown_keys")


def string(value, pattern=None, *, max_string=MAX_STRING):
    require(type(value) is str and 0 < len(value) <= max_string, "invalid_string")
    if pattern:
        require(re.fullmatch(pattern, value) is not None, "invalid_string")


def integer(value, minimum=0):
    require(type(value) is int and minimum <= value <= 2**53, "invalid_integer")


def hash_value(value, *, commit=False):
    string(value, r"[0-9a-f]{40}|[0-9a-f]{64}" if commit else r"[0-9a-f]{64}")


def epoch(value):
    string(value)
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(date.tzinfo is not None, "timezone_required")
        return date.timestamp()
    except (ValueError, OverflowError) as exc:
        raise ValidationError("invalid_time") from exc


def time_string(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def hashes(value):
    require(type(value) is dict and 1 <= len(value) <= 4, "invalid_hashes")
    for path, hashed in value.items():
        require(path in {"input/stores/tsukumo.json", "input/routes.json", "input/config.json", "input/plan.json"}, "invalid_input_path")
        hash_value(hashed)


def validate_refusals(value):
    require(type(value) is list and len(value) <= 1000, "invalid_refusals")
    seen = set()
    for row in value:
        keys(row, {"refusal_id", "at", "kind", "url_sha256", "store"})
        string(row["refusal_id"], r"[A-Za-z0-9_-]{1,128}")
        require(row["refusal_id"] not in seen, "duplicate_refusal")
        seen.add(row["refusal_id"])
        epoch(row["at"])
        string(row["kind"])
        hash_value(row["url_sha256"])
        require(type(row["store"]) is str and row["store"] in ALLOWED_HOSTS, "invalid_store")
    return deepcopy(value)


def validate_ticket(value):
    bounded(value)
    keys(value, {"protocol_version", "run_id", "phase", "store", "issued_at", "collect_until", "accept_until", "seconds", "code_fingerprint", "fingerprint_version", "state_schema_version", "config_revision", "input_sha256", "state_sha", "cleared_refusals"}, {"accepted_discovery"})
    require(type(value["protocol_version"]) is int and value["protocol_version"] == 1, "protocol_version")
    string(value["run_id"], r"[A-Za-z0-9_-]{1,100}")
    require(value["phase"] in ("discovery", "verification"), "invalid_phase")
    require(type(value["store"]) is str and value["store"] in ALLOWED_HOSTS, "invalid_store")
    require(epoch(value["issued_at"]) < epoch(value["collect_until"]) < epoch(value["accept_until"]), "invalid_deadlines")
    for key in ("seconds", "fingerprint_version", "state_schema_version", "config_revision"):
        integer(value[key], 1)
    # A positive unknown fingerprint version must reach the agent's code
    # compatibility check so the coordinator receives an explicit decline.
    require(value["state_schema_version"] == 1, "unsupported_version")
    hash_value(value["code_fingerprint"])
    if value["state_sha"] not in (None, ""):
        hash_value(value["state_sha"], commit=True)
    hashes(value["input_sha256"])
    paths = {"input/stores/" + value["store"] + ".json", "input/routes.json", "input/config.json"}
    if value["phase"] == "verification":
        paths.add("input/plan.json")
        keys(value.get("accepted_discovery"), {"satellite_commit", "state_sha256"})
        hash_value(value["accepted_discovery"]["satellite_commit"], commit=True)
        hash_value(value["accepted_discovery"]["state_sha256"])
    else:
        require("accepted_discovery" not in value, "unexpected_discovery")
    require(set(value["input_sha256"]) == paths, "input_paths")
    require(type(value["cleared_refusals"]) is list and len(value["cleared_refusals"]) <= 1000, "invalid_cleared_refusals")
    for item in value["cleared_refusals"]:
        string(item, r"[A-Za-z0-9_-]{1,128}")
    return deepcopy(value)


def validate_manifest(value):
    bounded(value)
    keys(value, {"protocol_version", "kind", "ticket_commit", "run_id", "phase", "store", "status", "started_at", "finished_at", "code_sha", "code_fingerprint", "dependencies", "input_sha256", "config_sha256", "output_sha256", "active_refusals", "counters"}, {"nonce", "agent_id"})
    require(type(value["protocol_version"]) is int and value["protocol_version"] == 1, "protocol_version")
    require(value["kind"] in ("ack", "result"), "invalid_kind")
    if value["kind"] == "ack":
        string(value.get("agent_id"), r"[0-9a-f]{32}")
        string(value.get("nonce"), r"[A-Za-z0-9_-]{16,128}")
    else:
        require("nonce" not in value, "unexpected_nonce")
    if "agent_id" in value:
        string(value["agent_id"], r"[0-9a-f]{32}")
    hash_value(value["ticket_commit"], commit=True)
    if value["code_sha"] is not None:
        hash_value(value["code_sha"], commit=True)
    for key in ("code_fingerprint", "config_sha256"):
        hash_value(value[key])
    if value["kind"] == "ack":
        require(value["output_sha256"] is None, "ack_output")
    elif not str(value.get("status", "")).startswith("declined:"):
        hash_value(value["output_sha256"])
    else:
        require(value["output_sha256"] is None, "declined_output")
    string(value["run_id"], r"[A-Za-z0-9_-]{1,100}")
    require(value["phase"] in ("discovery", "verification"), "invalid_phase")
    require(type(value["store"]) is str and value["store"] in ALLOWED_HOSTS, "invalid_store")
    string(value["status"], r"(?:acknowledged|completed|deadline|stopped:[a-z_]+|declined:[a-z_]+|failed:[a-z_]+)")
    epoch(value["started_at"])
    if value["finished_at"] is not None:
        require(epoch(value["finished_at"]) >= epoch(value["started_at"]), "invalid_finished_at")
    else:
        require(value["kind"] == "ack", "missing_finished_at")
    hashes(value["input_sha256"])
    validate_refusals(value["active_refusals"])
    keys(value["counters"], {"requests", "ordinary_attempted", "ordinary_succeeded", "no_response", "refusal"})
    for count in value["counters"].values():
        integer(count)
    counts = value["counters"]
    require(counts["ordinary_succeeded"] <= counts["ordinary_attempted"] <= counts["requests"]
            and counts["no_response"] <= counts["requests"] and counts["refusal"] <= counts["requests"], "invalid_counters")
    keys(value["dependencies"], {"ok", "declared", "installed", "mismatches"})
    deps = value["dependencies"]
    require(type(deps["ok"]) is bool and type(deps["mismatches"]) is list, "invalid_dependencies")
    for key in ("declared", "installed"):
        require(type(deps[key]) is dict, "invalid_dependencies")
        for name, version in deps[key].items():
            string(name)
            if key == "declared" or version is not None:
                string(version)
    for mismatch in deps["mismatches"]:
        string(mismatch)
    if deps["ok"]:
        require(not deps["mismatches"] and deps["declared"] == deps["installed"], "invalid_dependencies")
    return deepcopy(value)


def validate_echo(value):
    bounded(value)
    keys(value, {"ticket_commit", "nonce", "actions_time"})
    hash_value(value["ticket_commit"], commit=True)
    string(value["nonce"], r"[A-Za-z0-9_-]{16,128}")
    epoch(value["actions_time"])
    return deepcopy(value)


def build_ticket(**fields):
    return validate_ticket({"protocol_version": 1, "fingerprint_version": 1, "state_schema_version": 1, **fields})


def build_manifest(**fields):
    return validate_manifest({"protocol_version": 1, **fields})


def build_echo(**fields):
    return validate_echo(fields)


def stage_deadlines(run_deadline, stage_start, seconds, grace, decision_seconds):
    """Epoch seconds in and out; None means no collection time remains."""
    for value in (run_deadline, stage_start, seconds, grace, decision_seconds):
        require(type(value) in (int, float) and math.isfinite(value), "invalid_seconds")
    require(seconds > 0 and grace >= 0 and decision_seconds >= 0, "invalid_seconds")
    end = min(stage_start + seconds, run_deadline - decision_seconds - grace)
    if end - COLLECT_MARGIN_SECONDS <= stage_start:
        return None
    return {"S": end, "collect_until": end - COLLECT_MARGIN_SECONDS, "accept_until": end + grace}


def ack_window_end(issued, accept_until):
    return min(issued + ACK_WINDOW_SECONDS, accept_until)


def stage_status(status, reason=None):
    string(status)
    if status in ("satellite_declined", "satellite_rejected"):
        string(reason, r"[a-z_][a-z0-9_:.-]{0,127}")
        return status + ":" + reason
    if re.fullmatch(r"stopped:[a-z_]+", status) and reason is None:
        return status
    require(status in STAGE_STATUSES and reason is None, "invalid_stage_status")
    return status

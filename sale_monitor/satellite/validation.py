"""Validate effective config and untrusted result bytes without writing state."""
from copy import deepcopy
from datetime import datetime
import json
import math
import re

from ..models import Offer, digest
from ..parsing import canonical
from .guard import allowed_satellite_url
from .protocol import (ALLOWED_HOSTS, ValidationError, bounded, epoch, hash_value,
                       integer, keys, require, sha256_bytes, string,
                       validate_manifest, validate_refusals, validate_ticket)

MAX_STATE_BYTES = 32 * 1024 * 1024
MAX_STATE_ITEMS = 100_000
MAX_STATE_STRING = 65_536

# These are the fields written by Collector, including carried checkpoints.
STATE_TYPES = {
    **dict.fromkeys(("schema_version", "pending_count", "request_count", "list_pages", "listed_candidates"), int),
    **dict.fromkeys(("store", "run_id", "phase", "status", "started_at", "checkpoint_at", "cycle_started_at", "completed_at"), str),
    "cycle_complete": bool,
    **dict.fromkeys(("queue", "offers", "verification_queue", "verification", "scheduler", "retry_activity", "changes", "deferred_failures", "request_counts", "budget", "robots", "rate_limit_spacing_seconds", "http_evidence", "retry_after", "transport_retry_after", "access_retry_after", "access_block", "comparison_refresh", "comparison_routing", "comparison_searches", "discovery_replacements", "source_metadata", "flyer", "transport_diagnostic"), dict),
    **dict.fromkeys(("done", "journal", "verification_done", "errors", "recovered_errors", "waiting_dependencies", "discovery_gaps", "discovery_gap_resolutions", "robots_disallowed"), list),
}
ALLOWED_STATE_KEYS = frozenset(STATE_TYPES)


def _walk_urls(value, store, url_field=False):
    """Inspect values and mapping keys, including URLs embedded in evidence."""
    if type(value) is dict:
        for key, item in value.items():
            _walk_urls(key, store)
            _walk_urls(item, store, key == "url" or key.endswith(("_url", "_urls")))
    elif type(value) is list:
        for item in value:
            _walk_urls(item, store, url_field)
    elif type(value) is str:
        if url_field:
            require(allowed_satellite_url(value, store), "foreign_url")
        for match in re.finditer(r"(?:[A-Za-z][A-Za-z0-9+.-]*://|//)[^\s<>\"']+", value):
            require(allowed_satellite_url(match.group().rstrip(",;)"), store), "foreign_url")


def _typed(mapping, schema, required=()):
    keys(mapping, required, schema)
    for key, value in mapping.items():
        allowed = schema[key] if isinstance(schema[key], tuple) else (schema[key],)
        require(type(value) in allowed, "invalid_type:" + key)


def build_effective_config(sources_cfg, routes_cfg, store):
    """Wire shape: sources subset + route + host settings + revision.

    `stores` and `seller_aliases` can be passed directly to Collector.
    Route and hosts come from the coordinator's effective route state.
    """
    require(store in ALLOWED_HOSTS, "invalid_store")
    host = ALLOWED_HOSTS[store]
    config = {"schema_version": sources_cfg["schema_version"],
              "stores": {store: deepcopy(sources_cfg["stores"][store])},
              "seller_aliases": {key: value for key, value in sources_cfg.get("seller_aliases", {}).items()
                                 if value == store or key == store or key.startswith(store + ":")},
              "route": deepcopy(routes_cfg["routes"][store + ":direct"]),
              "hosts": {"default": deepcopy(routes_cfg["hosts"]["default"]),
                        host: deepcopy(routes_cfg["hosts"].get(host, {}))},
              "config_revision": routes_cfg["config_revision"]}
    validate_effective_config(config, store)
    return config


def validate_effective_config(config, store):
    require(type(store) is str and store in ALLOWED_HOSTS, "invalid_store")
    bounded(config)
    keys(config, {"schema_version", "stores", "seller_aliases", "route", "hosts", "config_revision"})
    integer(config["schema_version"], 1)
    require(config["schema_version"] == 1, "schema_version")
    integer(config["config_revision"], 1)
    keys(config["stores"], {store})
    cfg = config["stores"][store]
    _typed(cfg, {"name": str, "adapter": str, "seed_urls": list, "product_patterns": list,
                 "sale_patterns": list, "default_condition": str, "pc_only": bool,
                 "browser_fallback": bool, "list_url_replacements": dict},
           {"adapter", "seed_urls", "product_patterns"})
    require(cfg["adapter"] == "html" and not cfg.get("browser_fallback"), "browser_or_adapter")
    for key in ("seed_urls", "product_patterns", "sale_patterns"):
        for value in cfg.get(key, []):
            string(value)
            if key == "seed_urls":
                require(allowed_satellite_url(value, store), "foreign_url")
    for source, target in cfg.get("list_url_replacements", {}).items():
        require(allowed_satellite_url(source, store) and allowed_satellite_url(target, store), "foreign_url")
    require(type(config["seller_aliases"]) is dict, "seller_aliases")
    for source, target in config["seller_aliases"].items():
        string(source)
        require(target == store, "foreign_seller_alias")
    route = config["route"]
    _typed(route, {"store": str, "channel": str, "executor": str, "functions": dict,
                   "reason": str, "evidence": dict, "probe": dict, "cleared_refusals": list,
                   "consecutive_successes": int, "retry_after_until": (int, float, type(None)),
                   "recovery_candidate": (dict, type(None)), "last_probe": (dict, type(None)), "probe_log": list},
           {"store", "channel", "functions"})
    require(route["store"] == store and route["channel"] == "direct", "foreign_route")
    require(route.get("executor", "satellite") == "satellite", "invalid_executor")
    keys(route["functions"], (), {"sale_discovery", "product_verify", "comparison_lookup", "shipping_policy"})
    for name, function in route["functions"].items():
        _typed(function, {"status": str, "since": str, "automatic": bool, "config_status": str,
                          "reason": str, "evidence": dict, "refusal_id": str, "refusal_ids": list}, {"status"})
        require(function["status"] in {"active", "paused_maintenance", "paused_refused", "paused_unreachable", "paused_configuration", "not_implemented"}, "route_status")
        if name == "comparison_lookup":
            require(function["status"] != "active", "satellite_search_disabled")
    if "probe" in route:
        _typed(route["probe"], {"url": str, "interval_hours": int, "successes_to_resume": int, "must_not_contain": list}, {"url", "interval_hours", "successes_to_resume"})
    if "evidence" in route:
        _validate_route_evidence(route["evidence"])
    for function in route["functions"].values():
        if "evidence" in function:
            _validate_route_evidence(function["evidence"])
    for key in ("last_probe",):
        if route.get(key) is not None:
            _validate_route_evidence(route[key])
    for row in route.get("probe_log", []):
        _validate_route_evidence(row)
    if route.get("recovery_candidate") is not None:
        recovery = route["recovery_candidate"]
        _typed(recovery, {"since": str, "consecutive_successes": int, "last_ok_at": str, "evidence": dict},
               {"since", "consecutive_successes", "last_ok_at", "evidence"})
        _validate_route_evidence(recovery["evidence"])
    for refusal_id in route.get("cleared_refusals", []):
        string(refusal_id, r"[A-Za-z0-9_-]{1,128}")
    if "probe" in route:
        for marker in route["probe"].get("must_not_contain", []):
            string(marker)
    host = ALLOWED_HOSTS[store]
    keys(config["hosts"], {"default", host})
    for settings in config["hosts"].values():
        keys(settings, (), {"min_interval_seconds"})
        if "min_interval_seconds" in settings:
            value = settings["min_interval_seconds"]
            require(type(value) in (int, float) and value > 0, "invalid_interval")
    _walk_urls(config, store)
    return max(5, config["hosts"][host].get("min_interval_seconds", config["hosts"]["default"].get("min_interval_seconds", 2)))


def _validate_route_evidence(value):
    _typed(value, {"observed_at": str, "summary": str, "checked_at": str, "ok": bool,
                   "explicit_refusal": bool, "http_status": (int, type(None)),
                   "retry_after_until": (int, float, type(None)), "refusals": list})
    if "refusals" in value:
        validate_refusals(value["refusals"])


def _load_state(data):
    require(type(data) is bytes and len(data) <= MAX_STATE_BYTES, "state_size")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate_key")
            result[key] = value
        return result
    try:
        value = json.loads(data, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError("invalid_json") from exc
    bounded(value, limit=MAX_STATE_BYTES, max_items=MAX_STATE_ITEMS, max_string=MAX_STATE_STRING)
    _typed(value, STATE_TYPES, {"schema_version", "store", "queue", "done", "offers", "journal", "cycle_complete"})
    require(value["schema_version"] == 1, "state_schema_version")
    for key in ("pending_count", "request_count", "list_pages", "listed_candidates"):
        if key in value:
            integer(value[key])
    for name in ("queue", "verification_queue"):
        for task in value.get(name, {}).values():
            require(type(task) is dict, "invalid_task")
    for row in value["journal"]:
        require(type(row) is dict, "invalid_journal_row")
    verification = value.get("verification", {})
    if "results" in verification:
        require(type(verification["results"]) is dict
                and all(type(row) is dict for row in verification["results"].values()), "invalid_verification_results")
    return value


def _offer(row, store):
    require(type(row) is dict, "invalid_offer")
    keys(row, {"store", "seller_id", "product_id", "url", "observed_at", "observed_run_id"}, Offer.__dataclass_fields__)
    require(row["store"] == store and row["seller_id"] == store, "foreign_offer")
    for key in ("product_id", "url", "observed_at", "observed_run_id"):
        string(row[key], max_string=MAX_STATE_STRING)
    for key in ("title", "stock", "channel", "discovery_kind"):
        if key in row:
            require(type(row[key]) is str, "invalid_offer_type:" + key)
    for key in ("model", "jan", "brand", "condition", "variant", "warranty", "expires_at", "source_updated_at", "discovery_url", "purchase_limit"):
        if key in row:
            require(row[key] is None or type(row[key]) is str, "invalid_offer_type:" + key)
    for key in ("conditional_points", "evidence", "branches"):
        if key in row:
            require(type(row[key]) is list, "invalid_offer_type:" + key)
    for key in ("coupon", "branch_overrides"):
        if key in row:
            require(type(row[key]) is dict, "invalid_offer_type:" + key)
    if "verified" in row:
        require(type(row["verified"]) is bool, "invalid_offer_type:verified")
    require(allowed_satellite_url(row["url"], store), "foreign_url")
    if "issues" in row:
        require(type(row["issues"]) is list and all(type(v) is str for v in row["issues"]), "invalid_issues")
    for key in ("price_yen", "shipping_yen", "discount_yen", "points_yen", "listed_quantity"):
        if row.get(key) is not None:
            integer(row[key])
    return Offer.from_dict(row).key


def _failed(state, run_id, url):
    def same(value):
        return type(value) is str and canonical(value) == canonical(url)
    for queue_name in ("queue", "verification_queue"):
        for task in state.get(queue_name, {}).values():
            if (type(task) is dict and task.get("type") == "product" and same(task.get("url"))
                    and task.get("last_attempt_run_id") == run_id and task.get("last_error")):
                return True
    verification = state.get("verification", {})
    if verification.get("run_id") == run_id:
        for row in verification.get("results", {}).values():
            if type(row) is dict and row.get("result") == "failed" and row.get("type") == "product" and row.get("reason") and same(row.get("url")):
                return True
    return False


def validate_result(input_bytes, output_bytes, ticket, manifest, received_at, *, ticket_commit=None):
    """ticket_commit is trusted relay metadata, not a ticket.json field."""
    validate_ticket(ticket)
    validate_manifest(manifest)
    require(manifest["kind"] == "result", "not_result")
    require(manifest["status"] == "completed" or manifest["status"].startswith("stopped:"), "result_status")
    require(ticket_commit is not None, "ticket_commit_required")
    hash_value(ticket_commit, commit=True)
    require(manifest["ticket_commit"] == ticket_commit, "ticket_commit")
    for key in ("run_id", "phase", "store", "code_fingerprint", "input_sha256"):
        require(manifest[key] == ticket[key], "mismatch:" + key)
    require(manifest["dependencies"]["ok"] and not manifest["dependencies"]["mismatches"], "dependency_mismatch")
    require(manifest["config_sha256"] == ticket["input_sha256"]["input/config.json"], "config_hash")
    if isinstance(received_at, datetime):
        require(received_at.tzinfo is not None, "timezone_required")
    received = received_at.timestamp() if isinstance(received_at, datetime) else epoch(received_at) if isinstance(received_at, str) else received_at
    require(type(received) in (int, float) and math.isfinite(received), "invalid_received_at")
    require(received >= epoch(ticket["issued_at"]), "received_before_issue")
    require(received <= epoch(ticket["accept_until"]), "deadline")
    path = "input/stores/" + ticket["store"] + ".json"
    require(sha256_bytes(input_bytes) == ticket["input_sha256"][path], "input_hash")
    require(sha256_bytes(output_bytes) == manifest["output_sha256"], "output_hash")
    if ticket["phase"] == "verification":
        require(sha256_bytes(input_bytes) == ticket["accepted_discovery"]["state_sha256"], "discovery_hash")
    before, after = _load_state(input_bytes), _load_state(output_bytes)
    store, run_id = ticket["store"], ticket["run_id"]
    require(before["store"] == after["store"] == store, "store")
    require(after.get("run_id") == run_id and after.get("phase") == ticket["phase"], "run_or_phase")
    _walk_urls(before, store)
    _walk_urls(after, store)
    old = before["journal"]
    require(digest(after["journal"][:len(old)]) == digest(old), "journal_prefix")
    latest = {}
    for row in after["journal"][len(old):]:
        keys(row, {"observation_id", "run_id", "offer"})
        require(row["run_id"] == run_id, "journal_run")
        offer = row["offer"]
        key = _offer(offer, store)
        require(offer["observed_run_id"] == run_id, "observation_run")
        require(epoch(ticket["issued_at"]) <= epoch(offer["observed_at"]) <= received, "observation_time")
        require(row["observation_id"] == digest([key, offer["observed_at"], offer]), "observation_id")
        latest[key] = offer
    require(before["offers"].keys() <= after["offers"].keys(), "deleted_offer")
    require(latest.keys() <= after["offers"].keys(), "missing_observation")
    for key, row in after["offers"].items():
        require(_offer(row, store) == key, "offer_key")
        base = latest.get(key, before["offers"].get(key))
        require(base is not None, "unrecorded_offer")
        if digest(row) == digest(base):
            continue
        expected = deepcopy(base)
        expected["issues"] = sorted(set(expected.get("issues", []) + ["latest_fetch_failed"]))
        require(digest(row) == digest(expected) and _failed(after, run_id, row["url"]), "offer_tampering")


def extract_refusals(manifest):
    """Independently inspect safety metadata, even for invalid result bodies."""
    require(type(manifest) is dict and "active_refusals" in manifest, "missing_refusals")
    bounded(manifest["active_refusals"])
    return validate_refusals(manifest["active_refusals"])

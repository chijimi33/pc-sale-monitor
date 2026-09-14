from __future__ import annotations

from collections import Counter
from datetime import timedelta
from pathlib import Path
import shutil

from .engine import evaluate, update_events
from .models import STORES, Offer, digest, fresh, iso, timestamp, utcnow
from .storage import Store, atomic_json, read_json


def health(state: dict, now) -> dict:
    offers = [Offer.from_dict(row) for row in state.get("offers", {}).values()]
    current = [o for o in offers if o.observed_run_id == state.get("run_id") and state.get("status") != "job_missing" and fresh(o.observed_at, now) and "latest_fetch_failed" not in o.issues]
    fields = {"identity": lambda o: bool(o.identity), "seller": lambda o: bool(o.seller_id), "condition": lambda o: o.condition is not None,
              "price": lambda o: type(o.price_yen) is int, "shipping": lambda o: type(o.shipping_yen) is int,
              "stock": lambda o: o.stock != "unknown", "evidence": lambda o: bool(o.evidence and o.verified)}
    coverage = {key: {"known": sum(test(o) for o in current), "total": len(current)} for key, test in fields.items()}
    queue = state.get("queue", {})
    created = [timestamp(q.get("created_at")) for q in queue.values() if timestamp(q.get("created_at"))]
    errors = Counter((e.get("reason"), e.get("url")) for e in state.get("errors", []))
    return {"status": state.get("status", "not_run"), "checkpoint_at": state.get("checkpoint_at"), "run_id": state.get("run_id"),
            "cycle_complete": state.get("cycle_complete", False), "list_pages": state.get("list_pages", 0), "listed_candidates": state.get("listed_candidates", 0),
            "known_offers": len(offers), "current_offers": len(current), "eligible_offers": sum(not o.errors(now) for o in current),
            "mandatory_field_coverage": coverage, "pending_count": len(queue), "oldest_pending_at": iso(min(created)) if created else None,
            "pending_over_24h": sum(now - t > timedelta(hours=24) for t in created), "errors": [{"reason": reason, "url": url, "affected_tasks": count} for (reason, url), count in errors.items()],
            "discovery_gaps": state.get("discovery_gaps", []), "source_metadata": state.get("source_metadata"), "flyer": state.get("flyer")}


def merge_incoming(root: Path, incoming: Path):
    if not incoming.exists():
        return
    for store in STORES:
        source = incoming / "stores" / (store + ".json")
        if source.exists():
            value = read_json(source, None)
            if value.get("store") != store:
                raise ValueError("artifact_store_mismatch")
            atomic_json(root / "stores" / source.name, value)
    if (incoming / "flyers").exists():
        for source in (incoming / "flyers").rglob("*.json"):
            relative = source.relative_to(incoming)
            atomic_json(root / relative, read_json(source, None))


def aggregate(root: Path, public: Path, run_id: str, now=None) -> dict:
    now = now or utcnow()
    disk = Store(root)
    states = {name: disk.load(f"stores/{name}.json", {}) for name in STORES}
    statuses = {}
    current = []
    changes = {}
    for name, state in states.items():
        if state.get("run_id") != run_id:
            state = {**state, "status": "job_missing", "cycle_complete": False}
        statuses[name] = health(state, now)
        if state.get("run_id") != run_id:
            continue
        changes.update(state.get("changes", {}))
        for row in state.get("offers", {}).values():
            offer = Offer.from_dict(row)
            # A previous good snapshot is historical evidence, not a substitute
            # for a missing job or failed fetch in this run.
            if offer.observed_run_id == run_id and fresh(offer.observed_at, now) and "latest_fetch_failed" not in offer.issues:
                current.append(offer)
    historical = [row for row in disk.history() if row.get("run_id") != run_id]
    registry = disk.load("events/registry.json", {"states": {}, "events": {}})
    decisions = []
    reviews = []
    for offer in current:
        payment = evaluate(offer, current, historical, now)
        points = evaluate(offer, current, historical, now, points=True)
        decision = payment if payment["status"] == "accepted" or points["status"] != "accepted" else points
        decisions.append({"offer": offer.to_dict(), "payment": payment, "points": points})
        if offer.discovery_kind != "comparison":
            events, state = update_events(offer, decision, registry["states"].get(offer.key), now)
            registry["states"][offer.key] = state
            for event in events:
                registry["events"].setdefault(event["event_id"], event)
            if decision["status"] != "accepted":
                reviews.append({"offer_key": offer.key, "store": offer.store, "title": offer.title, "url": offer.url, "price_yen": offer.price_yen,
                                "reasons": decision["reasons"], "change": changes.get(offer.key), "discovery_kind": offer.discovery_kind})
    # One atomic commit of event state and event outbox prevents lost publication
    # after interruption between event generation and output JSON writing.
    disk.save("events/registry.json", registry)
    for name, state in states.items():
        grouped = {}
        for row in state.get("journal", []):
            date = timestamp(row["offer"].get("observed_at"))
            if date is None:
                continue
            filename = f"history/{name}/{date.date().isoformat()}.json"
            if filename not in grouped:
                grouped[filename] = {r["observation_id"]: r for r in disk.load(filename, [])}
            grouped[filename].setdefault(row["observation_id"], row)
        for filename, rows in grouped.items():
            disk.save(filename, list(rows.values()))
        if state:
            state["journal"] = []
            disk.save(f"stores/{name}.json", state)
    # Persist exact-identity searches for the next collection cycle. These are
    # candidates already discovered on sale lists, never full-catalogue crawling.
    for name in STORES:
        requests = {}
        for offer in current:
            if offer.store == name or offer.discovery_kind == "comparison" or not offer.identity:
                continue
            query = offer.jan or " ".join(filter(None, (offer.brand, offer.model)))
            priority = {"new": 0, "price_down": 1, "restocked": 2}.get(changes.get(offer.key), 3)
            requests.setdefault(offer.identity, {"query": query, "identity": offer.identity, "priority": priority})
        disk.save(f"requests/{name}.json", sorted(requests.values(), key=lambda q: (q["priority"], q["identity"])))
    public.mkdir(parents=True, exist_ok=True)
    events = [e for e in registry["events"].values() if timestamp(e["occurred_at"]) and now - timestamp(e["occurred_at"]) <= timedelta(days=7)]
    events.sort(key=lambda e: (e["occurred_at"], e["event_id"]))
    # Every retained event is revalidated against current evidence before it is
    # offered for notification. Its fixed ID and prior delivery status survive.
    accepted = {d["offer"]["store"] + ":" + Offer.from_dict(d["offer"]).key: d for d in decisions if d["payment"]["status"] == "accepted" or d["points"]["status"] == "accepted"}
    for event in events:
        key = event["offer"]["store"] + ":" + event["offer_key"]
        event["currently_actionable"] = event["kind"] == "ended" and now - timestamp(event["occurred_at"]) <= timedelta(hours=8) or key in accepted
        event["current_evidence"] = accepted.get(key)
    notification = [e for e in events if e["currently_actionable"] and e["delivery_status"] != "delivered"]
    complete = sum(s["status"] == "complete" for s in statuses.values())
    index = {"schema_version": 1, "generated_at": iso(now), "run_id": run_id, "mode": "parallel_validation", "monitored_store_count": 10,
             "excluded_stores": ["rakuten"], "complete_stores": complete, "collection_completion_rate": complete/10,
             "stores": statuses, "notification_count": len(notification), "review_count": len(reviews),
             "files": {"notifications": "notifications.json", "reviews": "review_queue.json", "flyer_review": "flyer_review.json", "evidence": "evidence.json", "validation": "validation.json"},
             "delivery_guarantee": "at_least_once_best_effort; publication_and_delivery_are_distinct", "full_rescan_needed": False}
    atomic_json(public / "notifications.json", {"generated_at": iso(now), "events": notification})
    atomic_json(public / "review_queue.json", {"generated_at": iso(now), "candidates": reviews, "flyer": disk.load("flyers/latest.json", None)})
    flyer = disk.load("flyers/latest.json", None)
    assets = []
    for asset in (flyer or {}).get("assets", []):
        parsed = disk.load(asset["extraction_path"], {})
        assets.append({**asset, "candidates": parsed.get("candidates", []), "text": parsed.get("text", "")})
    atomic_json(public / "flyer_review.json", {"generated_at": iso(now), "flyer": flyer, "assets": assets,
                "collection_status": statuses["koubou"].get("flyer"), "quantity_scope": "common_flyer_not_store_inventory"})
    atomic_json(public / "evidence.json", {"generated_at": iso(now), "decisions": decisions})
    disk.save(f"metrics/{run_id}.json", index)
    report = validation(root, now)
    atomic_json(public / "validation.json", report)
    atomic_json(public / "latest.json", index)
    return index


def validation(root: Path, now=None) -> dict:
    now = now or utcnow()
    snapshots = [read_json(p, {}) for p in (root / "metrics").glob("*.json")]
    snapshots = [s for s in snapshots if timestamp(s.get("generated_at"))]
    snapshots.sort(key=lambda s: s["generated_at"])
    start = timestamp(snapshots[0]["generated_at"]) if snapshots else now
    recent = [s for s in snapshots if now - timestamp(s["generated_at"]) <= timedelta(days=7)]
    reasons = []
    if now - start < timedelta(days=7):
        reasons.append("seven_days_not_elapsed")
    if len(recent) < 40:
        reasons.append("insufficient_scheduled_runs")
    if any(s.get("complete_stores", 0) < 10 for s in recent):
        reasons.append("ten_store_coverage_incomplete")
    if any(v.get("pending_over_24h", 0) for s in recent for v in s["stores"].values()):
        reasons.append("queue_over_24h")
    coverage = {}
    for name in STORES:
        known = Counter()
        total = Counter()
        for snapshot in recent:
            for key, values in snapshot.get("stores", {}).get(name, {}).get("mandatory_field_coverage", {}).items():
                known[key] += values["known"]
                total[key] += values["total"]
        coverage[name] = {key: known[key]/total[key] if total[key] else None for key in total}
        if not total or any(not total[k] or known[k]/total[k] < .95 for k in total):
            reasons.append("mandatory_coverage_below_95_percent:" + name)
    audit = read_json(root / "validation" / "manual_review.json", {})
    if not audit.get("reviewed_at") or audit.get("false_positive_count") != 0 or audit.get("reviewed_count", 0) < 20:
        reasons.append("manual_false_positive_audit_pending")
    return {"started_at": iso(start), "earliest_cutover_at": iso(start + timedelta(days=7)), "measured_runs": len(snapshots),
            "recent_runs": len(recent), "coverage": coverage, "manual_review": audit, "cutover_ready": not reasons, "reasons": reasons,
            "amazon_keepa_comparison": {"adoption": "not_enabled", "free_current_conditions_coverage": coverage.get("amazon"), "decision": "measure_free_gaps_before_paid_comparison"}}

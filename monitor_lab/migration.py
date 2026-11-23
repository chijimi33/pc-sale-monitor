from __future__ import annotations

import hashlib
from pathlib import Path

from sale_monitor.models import digest as production_digest, timestamp
from .safety import atomic_bytes, digest, encode, guard, read, write
from .stores import BACKENDS, FullJSON, Journal, SQLite, validate


def restore_export(export_file, backend, output):
    """Restore the complete lab state including transaction receipts into a new target."""
    state = validate(read(Path(export_file)))
    output = guard(Path(output))
    if output.exists() and any(output.iterdir()):
        raise ValueError("Restore destination must be new/empty")
    with BACKENDS[backend](output) as target:
        if isinstance(target, FullJSON):
            atomic_bytes(target.path, encode(state, pretty=True))
            target.state = state
        elif isinstance(target, Journal):
            atomic_bytes(target.checkpoint_path, encode(state))
            target.state = state
        else:
            with target.db:
                for ns, rows in state["records"].items():
                    target.db.executemany("INSERT INTO records VALUES(?,?,?)", [(ns, key, encode(value).decode()) for key, value in rows.items()])
                target.db.executemany("INSERT INTO transactions VALUES(?,?)", state["transactions"].items())
        if digest(target.snapshot()) != digest(state):
            raise ValueError("Restored state mismatch")
    return {"backend": backend, "state_hash": digest(state), "transaction_receipts_preserved": len(state["transactions"])}


def repair_torn_journal(source, output):
    """Explicit repair to a NEW directory; retain the original corrupt tail as evidence."""
    import json
    source, output = guard(Path(source)), guard(Path(output))
    if output.exists() and any(output.iterdir()):
        raise ValueError("Repair destination must be new/empty")
    raw = (source / "journal.jsonl").read_bytes()
    lines = raw.splitlines(keepends=True)
    good = bytearray()
    tail = b""
    for i, line in enumerate(lines):
        if not line.endswith(b"\n"):
            if i != len(lines) - 1:
                raise ValueError("Non-final corrupt journal record cannot be automatically repaired")
            tail = line
            break
        row = json.loads(line)
        if row["checksum"] != digest([row["txid"], row["changes"]]):
            raise ValueError("Checksum failure is not a repairable torn final write")
        good.extend(line)
    if not tail:
        raise ValueError("No torn final record found")
    atomic_bytes(output / "original-journal.evidence", raw)
    atomic_bytes(output / "uncommitted-tail.evidence", tail)
    if (source / "snapshot.json").exists():
        atomic_bytes(output / "snapshot.json", (source / "snapshot.json").read_bytes())
    atomic_bytes(output / "journal.jsonl", bytes(good))
    with Journal(output) as store:
        count = len(store.snapshot()["transactions"])
    result = {"recovered_transactions": count, "uncommitted_tail_bytes": len(tail),
              "source_sha256": hashlib.sha256(raw).hexdigest(), "source_unchanged": (source / "journal.jsonl").read_bytes() == raw,
              "requires_retry_of_uncommitted_task": True}
    write(output / "repair.json", result)
    return result


def export_legacy(experiment, output, *, project_observations=False):
    """Byte-identical baseline restore, optionally followed by the existing exporter.

All writes stay inside the lab. No Git ref update or delivery capability exists.
"""
    experiment, output = guard(Path(experiment)), guard(Path(output))
    if output.exists() and any(output.iterdir()):
        raise ValueError("Legacy output must be a new/empty experiment directory")
    meta = read(experiment / "experiment.json")
    with BACKENDS[meta["conditions"]["backend"]](experiment / "store") as store:
        state = store.snapshot()
    restored = []
    for name, ref in state["records"]["source_files"].items():
        source = guard(Path(ref["input_path"]))
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            raise ValueError("Pinned restore source changed")
        destination = guard(output / name)
        atomic_bytes(destination, raw)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != ref["sha256"]:
            raise ValueError("Legacy roundtrip mismatch")
        restored.append({"path": name, "sha256": ref["sha256"], "bytes": len(raw)})
    result = {"files": restored, "all_baseline_bytes_equal": True, "experiment_id": meta["experiment_id"],
              "projected_observations": False, "production_written": False}
    if project_observations:
        from sale_monitor.reporting import aggregate
        observations = list(state["records"].get("observations", {}).values())
        if not observations:
            raise ValueError("No observations to project")
        times = [timestamp(o["offer"]["observed_at"]) for o in observations]
        source_latest = output / "public/latest.json"
        if source_latest.exists():
            times.append(timestamp(read(source_latest)["generated_at"]))
        now = max(t for t in times if t is not None)
        for observation in observations:
            offer = observation["offer"]
            path = output / "state/stores" / (offer["store"] + ".json")
            shard = read(path)
            shard.update(run_id=meta["experiment_id"], cycle_complete=False, status="partial", completed_at=None)
            from sale_monitor.models import Offer
            key = Offer.from_dict(offer).key
            shard.setdefault("offers", {})[key] = offer
            shard.setdefault("journal", []).append({"observation_id": "lab-" + production_digest(offer),
                                                      "run_id": meta["experiment_id"], "offer": offer})
            write(path, shard)
        # Call the real existing exporter on the isolated clone to prove format
        # compatibility. Unknown experiment provenance cannot satisfy schedule gates.
        exported = aggregate(output / "state", output / "public", meta["experiment_id"], now=now)
        exported.update(mode="isolated_lab", experiment_id=meta["experiment_id"])
        write(output / "public/latest.json", exported)
        write(output / "state/metrics" / (meta["experiment_id"] + ".json"), exported)
        result.update(projected_observations=True, current_public_run=exported["run_id"],
                      monitored_store_count=exported["monitored_store_count"], excluded_stores=exported["excluded_stores"],
                      original_queue_values_preserved=True)
        for name, ref in state["records"]["source_files"].items():
            if name.startswith("state/stores/"):
                original = read(Path(ref["input_path"]))
                current = read(output / name)
                if original.get("queue") != current.get("queue"):
                    raise ValueError("Legacy projection changed the original pending queue")
    write(output / "roundtrip-report.json", result)
    return result

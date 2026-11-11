from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from .models import digest, iso
from .routes import RouteState
from .storage import Store


MIGRATION_VERSION = 1
STATE_SCHEMA_VERSION = 2


def fingerprint(root: Path) -> dict:
    root = Path(root)
    disk = Store(root)
    result = {"stores": {}, "events/registry.json": None, "history": {}, "flyers": {}}
    for path in sorted((root / "stores").glob("*.json")):
        state = disk.load(f"stores/{path.name}", {})
        result["stores"][path.stem] = {key: digest(state.get(key)) for key in ("offers", "journal")}
    registry = root / "events/registry.json"
    if registry.exists():
        result["events/registry.json"] = sha256(registry.read_bytes()).hexdigest()
    for name in ("history", "flyers"):
        for path in sorted((root / name).rglob("*")):
            if path.is_file():
                result[name][path.relative_to(root / name).as_posix()] = sha256(path.read_bytes()).hexdigest()
    return result


def _restore(backups):
    # Restore queues before removing the archives that protect their tasks.
    for path, content in reversed(backups.items()):
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(content)
        path.with_suffix(path.suffix + ".tmp").unlink(missing_ok=True)


def apply(root: Path, routes: RouteState, input_sha: str | None = None, now=None) -> dict:
    """Archive legacy work under the state directory supplied as root."""
    root = Path(root)
    disk = Store(root)
    before = fingerprint(root)
    schema = disk.load("schema.json", {})
    applied = any(row["version"] == MIGRATION_VERSION for row in schema.get("migrations", []))
    archived, remaining = {}, {}
    backups = {}
    changed_archives, changed_stores = {}, {}
    archives = {path.stem: disk.load(f"archive/{path.stem}.json", {"schema_version": 1, "entries": {}})
                for path in sorted((root / "stores").glob("*.json"))}
    if isinstance(now, (float, int)):
        now = datetime.fromtimestamp(now, timezone.utc)
    # Reuse the first durable migration timestamp after an interrupted apply.
    archived_at = min((record["archived_at"] for archive in archives.values()
                       for record in archive["entries"].values()
                       if record.get("migration_version") == MIGRATION_VERSION), default=iso(now))
    for path in sorted((root / "stores").glob("*.json")):
        store = path.stem
        state = disk.load(f"stores/{path.name}", {})
        counts = Counter()
        queue = state.get("queue", {})
        if not applied:
            channel = {"yahoo": "api", "amazon": "chimolog"}.get(store, "direct")
            status = routes.status(f"{store}:{channel}", "sale_discovery")
            entries = archives[store]
            for task_id, task in list(queue.items()):
                reason = f"route_paused:{status}" if status != "active" else (
                    "comparison_debt_abolished" if task.get("kind") == "comparison" else None)
                if reason is None:
                    continue
                entries["entries"].setdefault(task_id, {
                    "task": task, "task_id": task_id, "archived_at": archived_at, "reason": reason,
                    "migration_version": MIGRATION_VERSION, "input_sha": input_sha,
                })
                del queue[task_id]
                counts[reason] += 1
            if counts:
                state["pending_count"] = len(queue)
                state["waiting_dependencies"] = []
                changed_stores[path] = state
                changed_archives[root / f"archive/{store}.json"] = entries
        archived[store] = dict(counts)
        remaining[store] = len(queue)
    try:
        if not applied:
            schema["state_schema_version"] = STATE_SCHEMA_VERSION
            # The receipt describes all archived work; the report counts only
            # tasks removed from queues by this invocation.
            schema.setdefault("migrations", []).append({"version": MIGRATION_VERSION, "applied_at": archived_at,
                "input_sha": input_sha, "archived": {store: len(archive["entries"])
                                                      for store, archive in archives.items()}})
        # Every task must be durable in its archive before any queue loses it.
        changed = {**changed_archives, **changed_stores}
        if not applied:
            changed[root / "schema.json"] = schema
        for path, value in changed.items():
            backups[path] = path.read_bytes() if path.exists() else None
            disk.save(path.relative_to(root).as_posix(), value)
        if fingerprint(root) != before:
            raise RuntimeError("migration_fingerprint_mismatch")
    except Exception:
        _restore(backups)
        raise
    return {"archived": archived, "remaining": remaining, "fingerprint_match": True}


def revert(root: Path) -> dict:
    """Restore archived tasks without overwriting tasks already in the queue."""
    root = Path(root)
    disk = Store(root)
    before = fingerprint(root)
    backups = {}
    restored = {}
    try:
        for archive in sorted((root / "archive").glob("*.json")):
            store = archive.stem
            path = root / f"stores/{store}.json"
            state = disk.load(f"stores/{store}.json", {"store": store, "queue": {}})
            queue = state.setdefault("queue", {})
            count = 0
            for task_id, record in disk.load(f"archive/{store}.json", {})["entries"].items():
                if task_id not in queue:
                    queue[task_id] = record["task"]
                    count += 1
            restored[store] = count
            if count:
                state["pending_count"] = len(queue)
                backups[path] = path.read_bytes() if path.exists() else None
                disk.save(f"stores/{store}.json", state)
        schema_path = root / "schema.json"
        schema = disk.load("schema.json", {})
        schema["state_schema_version"] = 1
        # Removing the receipt permits a later deliberate re-application.
        schema["migrations"] = [row for row in schema.get("migrations", []) if row["version"] != MIGRATION_VERSION]
        backups[schema_path] = schema_path.read_bytes() if schema_path.exists() else None
        disk.save("schema.json", schema)
        if fingerprint(root) != before:
            raise RuntimeError("migration_fingerprint_mismatch")
    except Exception:
        _restore(backups)
        raise
    return {"restored": restored, "fingerprint_match": True}

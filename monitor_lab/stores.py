"""One durable transaction = task mutations + observation + outbox event.

Single writer is enforced across processes. Never recover corrupt files as empty.
The journal deliberately fails closed on a torn record; explicit repair retains it.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3

from .safety import atomic_bytes, digest, encode, guard, read, write


def initial():
    return {"schema": 1, "records": {}, "transactions": {}}


def validate(state):
    if state.get("schema") != 1 or not isinstance(state.get("records"), dict) or not isinstance(state.get("transactions"), dict):
        raise ValueError("Invalid lab state schema")
    return state


def apply(state, txid, changes):
    checksum = digest(changes)
    if txid in state["transactions"]:
        if state["transactions"][txid] != checksum:
            raise ValueError("Transaction ID reused with different contents")
        return False
    for namespace, key, value in changes:
        state["records"].setdefault(namespace, {})[key] = deepcopy(value)
    state["transactions"][txid] = checksum
    return True


class Base:
    def __init__(self, root):
        self.root = guard(Path(root))
        self.root.mkdir(parents=True, exist_ok=True)
        self.emitted_bytes = 0
        self.logical_bytes = 0
        self.lock = (self.root / "writer.lock").open("a+b")
        if self.lock.tell() == 0:
            self.lock.write(b"0")
            self.lock.flush()
        self.lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError("Another lab writer owns this experiment") from None

    def close(self):
        self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def export(self, path):
        write(path, self.snapshot())


class FullJSON(Base):
    def __init__(self, root):
        super().__init__(root)
        self.path = self.root / "state.json"
        try:
            self.state = validate(read(self.path)) if self.path.exists() else initial()
        except Exception:
            self.close()
            raise

    def commit(self, txid, changes, hook=lambda stage: None):
        candidate = deepcopy(self.state)
        if not apply(candidate, txid, changes):
            return False
        body = encode(candidate, pretty=True)
        atomic_bytes(self.path, body, hook)
        self.state = candidate
        self.emitted_bytes += len(body)
        self.logical_bytes += len(encode(changes))
        return True

    def snapshot(self):
        return deepcopy(self.state)


class Journal(Base):
    def __init__(self, root, checkpoint_every=100):
        super().__init__(root)
        self.path = self.root / "journal.jsonl"
        self.checkpoint_path = self.root / "snapshot.json"
        self.every = checkpoint_every
        self.since_checkpoint = 0
        try:
            self.state = validate(read(self.checkpoint_path)) if self.checkpoint_path.exists() else initial()
            if self.path.exists():
                with self.path.open("rb") as f:
                    for number, line in enumerate(f, 1):
                        if not line.endswith(b"\n"):
                            raise ValueError(f"Torn journal record {number}; explicit repair required")
                        row = json.loads(line)
                        if row["checksum"] != digest([row["txid"], row["changes"]]):
                            raise ValueError(f"Journal checksum mismatch at {number}")
                        apply(self.state, row["txid"], row["changes"])
        except Exception:
            self.close()
            raise

    def commit(self, txid, changes, hook=lambda stage: None):
        checksum = digest(changes)
        if txid in self.state["transactions"]:
            if self.state["transactions"][txid] != checksum:
                raise ValueError("Transaction ID reused with different contents")
            return False
        body = encode({"txid": txid, "changes": changes, "checksum": digest([txid, changes])}) + b"\n"
        hook("before_commit")
        with self.path.open("ab") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        hook("after_commit")
        apply(self.state, txid, changes)
        self.emitted_bytes += len(body)
        self.logical_bytes += len(encode(changes))
        self.since_checkpoint += 1
        if self.every and self.since_checkpoint >= self.every:
            self.checkpoint()
        return True

    def checkpoint(self, hook=lambda stage: None):
        # Committed log may remain after a crash: txid de-duplication makes replay safe.
        body = encode(self.state)
        atomic_bytes(self.checkpoint_path, body)
        hook("after_snapshot")
        atomic_bytes(self.path, b"")
        self.emitted_bytes += len(body)
        self.since_checkpoint = 0

    def snapshot(self):
        return deepcopy(self.state)


class SQLite(Base):
    def __init__(self, root):
        super().__init__(root)
        self.path = self.root / "state.sqlite3"
        existed = self.path.exists()
        try:
            self.db = sqlite3.connect(self.path, timeout=5)
            if self.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite integrity check failed")
            if existed:
                tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables != {"records", "transactions"} or self.db.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise ValueError("Invalid existing SQLite lab schema")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA wal_autocheckpoint=0")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS records(ns TEXT, key TEXT, value TEXT NOT NULL,
                                                   PRIMARY KEY(ns,key));
                CREATE TABLE IF NOT EXISTS transactions(id TEXT PRIMARY KEY, checksum TEXT NOT NULL);
                PRAGMA user_version=1;
            """)
            self.db.commit()
        except Exception:
            if hasattr(self, "db"):
                self.db.close()
            super().close()
            raise

    def commit(self, txid, changes, hook=lambda stage: None):
        checksum = digest(changes)
        wal = Path(str(self.path) + "-wal")
        before = wal.stat().st_size if wal.exists() else 0
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            found = self.db.execute("SELECT checksum FROM transactions WHERE id=?", (txid,)).fetchone()
            if found:
                if found[0] != checksum:
                    raise ValueError("Transaction ID reused with different contents")
                return False
            for namespace, key, value in changes:
                self.db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?)", (namespace, key, encode(value).decode()))
            self.db.execute("INSERT INTO transactions VALUES(?,?)", (txid, checksum))
            hook("before_commit")
        hook("after_commit")
        self.logical_bytes += len(encode(changes))
        self.emitted_bytes += max(0, wal.stat().st_size - before)
        return True

    def snapshot(self):
        records = {}
        for ns, key, value in self.db.execute("SELECT ns,key,value FROM records ORDER BY ns,key"):
            records.setdefault(ns, {})[key] = json.loads(value)
        return {"schema": 1, "records": records,
                "transactions": dict(self.db.execute("SELECT id,checksum FROM transactions"))}

    def backup(self, target):
        target = guard(Path(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".pending")
        dest = sqlite3.connect(temporary)
        try:
            self.db.backup(dest)
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity check failed")
        finally:
            dest.close()
        with temporary.open("r+b") as f:
            os.fsync(f.fileno())
        os.replace(temporary, target)

    def close(self):
        self.db.close()
        super().close()


BACKENDS = {"json": FullJSON, "journal": Journal, "sqlite": SQLite}

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import uuid

QA_ROOT = Path("E:/Codex/pc-sale-monitor-qa/experiments/20261002-architecture-lab")


def allowed_root() -> Path:
    if os.name == "nt":
        return QA_ROOT.resolve()
    if os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("RUNNER_TEMP"):
        return (Path(os.environ["RUNNER_TEMP"]) / "pc-sale-monitor-lab").resolve()
    raise ValueError("Only the isolated E-drive lab or GitHub RUNNER_TEMP lab is writable")


def guard(path: Path) -> Path:
    path, root = Path(path).resolve(), allowed_root()
    if not path.is_relative_to(root) or path == root:
        raise ValueError(f"Refusing output outside an experiment subdirectory: {path}")
    if any(p.lower() in {".git", "state-repo", "monitor-data"} for p in path.parts):
        raise ValueError("Production state paths are forbidden")
    return path


def encode(value, *, pretty=False) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      indent=2 if pretty else None,
                      separators=None if pretty else (",", ":"), allow_nan=False).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(encode(value)).hexdigest()


def read(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_bytes(path: Path, body: bytes, hook=lambda stage: None):
    path = guard(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".pending")
    with temporary.open("wb") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    hook("before_commit")
    os.replace(temporary, path)
    if os.name != "nt":
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    hook("after_commit")


def write(path: Path, value):
    atomic_bytes(path, encode(value, pretty=True) + b"\n")


def environment():
    return {"system": platform.system(), "release": platform.release(),
            "python": sys.version, "machine": platform.machine(),
            "github_actions": os.environ.get("GITHUB_ACTIONS") == "true",
            "github_run_id": os.environ.get("GITHUB_RUN_ID"),
            "github_event": os.environ.get("GITHUB_EVENT_NAME")}


def implementation_hash(*, normalize_line_endings=False):
    repo = Path(__file__).resolve().parents[1]
    files = [p for directory in ("monitor_lab", "sale_monitor", "config")
             for p in (repo / directory).rglob("*") if p.suffix in {".py", ".json"} and p.is_file()]
    def checksum(path):
        body = path.read_bytes()
        if normalize_line_endings:
            body = body.replace(b"\r\n", b"\n")
        return hashlib.sha256(body).hexdigest()
    return digest({str(p.relative_to(repo)).replace("\\", "/"): checksum(p) for p in files})


def experiment_id():
    return "lab-" + uuid.uuid4().hex

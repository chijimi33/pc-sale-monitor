"""Local QA storage. Runtime artifacts live on the configured E: volume only."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from datetime import datetime, timezone

ROOT = Path(os.environ.get("PC_QA_ROOT", "E:/Codex/pc-sale-monitor-qa"))


def now():
    return datetime.now(timezone.utc).isoformat()


def inside(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError("path_outside_root")
    return path


def require_root():
    if os.name == "nt" and ROOT.resolve().drive.upper() != "E:":
        raise RuntimeError("QA artifacts must be on E:; no C: fallback")
    if not ROOT.is_dir():
        raise RuntimeError("E: QA directory unavailable; no fallback")


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def digest(data):
    return hashlib.sha256(data).hexdigest()


@contextlib.contextmanager
def lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    f = path.open("a+b")
    if f.tell() == 0:
        f.write(b"0")
        f.flush()
    f.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        f.close()


def environment(job):
    # Do not copy account credentials into the agent or its test subprocesses.
    keys = ("PATH", "SystemRoot", "COMSPEC", "PATHEXT", "WINDIR", "NUMBER_OF_PROCESSORS")
    env = {k: os.environ[k] for k in keys if k in os.environ}
    for name, folder in {"TEMP": "tmp", "TMP": "tmp", "QWEN_HOME": "home",
                         "QWEN_RUNTIME_DIR": "runtime", "APPDATA": "appdata",
                         "LOCALAPPDATA": "localappdata", "USERPROFILE": "profile",
                         "XDG_CONFIG_HOME": "config", "XDG_CACHE_HOME": "cache"}.items():
        path = Path(job) / folder
        path.mkdir(parents=True, exist_ok=True)
        env[name] = str(path)
    env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", NO_COLOR="1",
               CI="true", PC_QA_ROOT=str(ROOT), QA_LOCAL_API_KEY="local-only",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0")
    return env


def run(argv, cwd, timeout=600, env=None):
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def git(repo, *args):
    result = run(["git", "-c", "core.hooksPath=" + os.devnull, *args], repo)
    if result.returncode:
        raise RuntimeError(f"git {args[0] if args else ''} exit={result.returncode}: {(result.stderr or result.stdout)[-2000:]}")
    return result.stdout.strip()


def capture_patch(repo):
    # Runtime/PowerShell caches are artifacts, never proposed source changes.
    git(repo, "add", "-N", "--", "sale_monitor", "tests")
    result = run(["git", "-c", "core.hooksPath=" + os.devnull, "diff", "--binary", "--", "sale_monitor", "tests"], repo)
    if result.returncode: raise RuntimeError(result.stderr[-2000:])
    return result.stdout


def append_event(job, event):
    with (Path(job) / "tools.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": now(), **event}, ensure_ascii=False) + "\n")


def finalize(job, status, **extra):
    job = Path(job)
    files = {}
    names = ["input.json", "report.json", "report.md", "patch.diff", "tests.json", "controller-tests.json", "tools.jsonl", "agent.jsonl", "agent.stderr", "memory.json", "progress.json", "compaction-hooks.jsonl"]
    for folder in ("snapshot", "evidence", "llm-requests"):
        names.extend(p.relative_to(job).as_posix() for p in (job / folder).rglob("*") if p.is_file())
    for name in names:
        p = job / name
        if p.exists():
            files[name] = {"sha256": digest(p.read_bytes()), "bytes": p.stat().st_size}
    result = {"status": status, "finished_at": now(), "files": files, **extra}
    atomic(job / "manifest.json", result)
    return result

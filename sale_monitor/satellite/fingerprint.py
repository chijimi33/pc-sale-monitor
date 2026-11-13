"""Portable source identity and installed dependency checks."""
from importlib import metadata
from pathlib import Path
import re
import tomllib

from ..models import digest
from .protocol import sha256_bytes

FINGERPRINT_VERSION = 1


def _dependencies(path):
    return tomllib.loads(Path(path).read_text(encoding="utf-8"))["project"]["dependencies"]


def code_fingerprint(package_dir, pyproject_path):
    root = Path(package_dir)
    files = []
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if path.suffix == ".py" and "__pycache__" not in relative.parts:
            content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            files.append((relative.as_posix(), sha256_bytes(content)))
    files.sort()
    value = {"version": FINGERPRINT_VERSION, "files": files,
             "dependencies": _dependencies(pyproject_path)}
    return {"fingerprint": digest(value), "fingerprint_version": FINGERPRINT_VERSION, "files": len(files)}


def dependency_status(pyproject_path):
    declared, installed, mismatches = {}, {}, []
    for requirement in _dependencies(pyproject_path):
        match = re.fullmatch(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([A-Za-z0-9][A-Za-z0-9.!+_-]*)\s*", requirement)
        if not match:
            mismatches.append("unsupported:" + requirement)
            continue
        name, version = match.groups()
        name = re.sub(r"[-_.]+", "-", name).lower()
        if name in declared and declared[name] != version:
            mismatches.append("conflicting:" + name)
        declared[name] = version
        try:
            installed[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            installed[name] = None
        if installed[name] != version:
            mismatches.append(name)
    return {"ok": not mismatches, "declared": declared, "installed": installed, "mismatches": mismatches}

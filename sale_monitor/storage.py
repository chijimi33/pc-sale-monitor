from __future__ import annotations

import json
import os
from pathlib import Path


def read_json(path: Path, default):
    if not path.exists():
        return default
    # Corrupt persistent state is an error, not a fresh empty state.
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class Store:
    def __init__(self, root: Path):
        self.root = root

    def load(self, name: str, default):
        return read_json(self.root / name, default)

    def save(self, name: str, value):
        atomic_json(self.root / name, value)

    def history(self):
        rows = []
        for file in sorted((self.root / "history").glob("*/*.json")):
            rows.extend(read_json(file, []))
        return rows

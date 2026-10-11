from __future__ import annotations

import json
import os
from pathlib import Path


def read_json(path: Path, default):
    if not path.exists():
        return default
    # Corrupt persistent state is an error, not a fresh empty state.
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, data, *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    # Store checkpoints are rewritten after each task. A single encoded value
    # avoids streaming thousands of small, indented fragments on that path.
    # Keep flush/fsync/replace ordering so failed or interrupted writes never
    # replace the previously committed checkpoint with a partial document.
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" if compact else None
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        if compact:
            handle.write(encoded)
        else:
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
        atomic_json(self.root / name, value, compact=name.startswith("stores/"))

    def history(self):
        rows = []
        for file in sorted((self.root / "history").glob("*/*.json")):
            rows.extend(read_json(file, []))
        return rows

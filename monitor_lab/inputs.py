from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
from urllib.request import Request, urlopen

from .safety import atomic_bytes, digest, guard, read, write

REPOSITORY = "chijimi33/pc-sale-monitor"
SOURCE_BASE = "b1ba3dc3926b4130fcfbf3970d5a876c8ff0593d"
SNAPSHOTS = {
    "transport_failure": "8851f7bbb7ffb348d8e28a01a0eaa1e1dabf8f5e",
    "tsukumo_recovery": "7bcaf7335ed76f5078b39a851e6cdc17a59f4174",
    "ark_repaired": "db3a843ec8e7699269a0e6048cfbbe5cc9db6505",
}


def download(url):
    with urlopen(Request(url, headers={"User-Agent": "PCSaleMonitor-Lab/1.0"}), timeout=60) as response:
        return response.read()


def prepare(root: Path, saved_pages: Path | None = None):
    """Pin every state/public JSON blob, including history, audits and pending queues."""
    import json
    root = guard(root)
    manifest = {"schema": 1, "source_base": SOURCE_BASE, "snapshots": {}, "pages": []}
    for label, sha in SNAPSHOTS.items():
        tree_path = root / label / "git-tree.json"
        if not tree_path.exists():
            atomic_bytes(tree_path, download(f"https://api.github.com/repos/{REPOSITORY}/git/trees/{sha}?recursive=1"))
        tree = read(tree_path)
        if tree.get("truncated"):
            raise ValueError("Incomplete input tree")
        blobs = [x for x in tree["tree"] if x["type"] == "blob" and
                 x["path"].startswith(("state/", "public/")) and x["path"].endswith(".json")]

        def one(blob):
            path = root / label / blob["path"]
            if not path.exists():
                atomic_bytes(path, download(f"https://raw.githubusercontent.com/{REPOSITORY}/{sha}/{blob['path']}"))
            body = path.read_bytes()
            actual = hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
            if actual != blob["sha"]:
                raise ValueError(f"Pinned Git blob mismatch: {path}")
            json.loads(body)
            return {"path": str(path.relative_to(root)).replace("\\", "/"), "git_blob": actual,
                    "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}

        # GitHub inputs only; store requests are never parallelized by the lab.
        with ThreadPoolExecutor(max_workers=4) as pool:
            files = list(pool.map(one, blobs))
        manifest["snapshots"][label] = {"data_sha": sha, "files": files}
        write(root / "manifest.partial.json", manifest)
    if saved_pages:
        for row in read(saved_pages / "source-observations.json"):
            if row["name"].endswith(("-b550", "-capture")):
                body = (saved_pages / (row["name"] + ".html")).read_bytes()
                if hashlib.sha256(body).hexdigest() != row["body_sha256"]:
                    raise ValueError("Source page hash mismatch")
                path = root / "pages" / (row["name"] + ".html")
                atomic_bytes(path, body)
                manifest["pages"].append({**row, "path": str(path.relative_to(root)).replace("\\", "/"),
                                          "store": row["name"].split("-")[0],
                                          "group": row["name"].split("-")[1]})
    manifest["input_hash"] = digest(manifest)
    write(root / "manifest.json", manifest)
    return manifest


def verify(root: Path):
    manifest = read(root / "manifest.json")
    expected = manifest.pop("input_hash")
    if digest(manifest) != expected:
        raise ValueError("Input manifest changed")
    manifest["input_hash"] = expected
    for snapshot in manifest["snapshots"].values():
        for item in snapshot["files"]:
            path = (root / item["path"]).resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError("Input path traversal")
            if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError(f"Input file changed: {path}")
    for page in manifest["pages"]:
        path = (root / page["path"]).resolve()
        if not path.is_relative_to(root.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != page["body_sha256"]:
            raise ValueError("Source fixture changed")
    return manifest


def import_state(root: Path, label: str):
    """Keep the complete original files; split only queues for efficient mutation."""
    files, tasks, offers = {}, {}, {}
    manifest = read(root / "manifest.json")
    for item in manifest["snapshots"][label]["files"]:
        path = root / item["path"]
        name = str(path.relative_to(root / label)).replace("\\", "/")
        # Immutable pinned source files are retained on disk, not copied into every
        # checkpoint. Their hashes are verified before a run and again on restore.
        files[name] = {"input_path": str(path.resolve()), "sha256": item["sha256"], "bytes": item["bytes"]}
        if name.startswith("state/stores/"):
            value = read(path)
            store = path.stem
            tasks.update({store + ":" + key: {**task, "lab_store": store, "lab_key": key,
                                               "lab_status": "pending", "lab_attempts": []}
                          for key, task in value.get("queue", {}).items()})
            offers.update(value.get("offers", {}))
    return files, tasks, offers

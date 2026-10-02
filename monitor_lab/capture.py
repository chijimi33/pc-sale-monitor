"""Portable, verified HTTP evidence. Reading or replaying a bundle never fetches."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from pathlib import Path, PurePosixPath
import re

from sale_monitor.http import Page
from .acquire import MAX_BODY, Receipt
from .safety import atomic_bytes, digest, guard, read, write

FORMAT = "pc-sale-monitor-http-evidence-v1"
MAX_FILES = 128
MAX_TOTAL_BYTES = 192 * 1024 * 1024


def _relative_file(root, relative):
    if not isinstance(relative, str) or "\\" in relative or ":" in relative:
        raise ValueError("Evidence paths must be portable relative paths")
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts or path.as_posix() != relative:
        raise ValueError("Invalid evidence path")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Evidence path escapes the bundle")
    return resolved


class Capture:
    def __init__(self, root, metadata, settings):
        self.root = guard(Path(root))
        if self.root.exists() and any(self.root.iterdir()):
            raise ValueError("Use an empty directory; existing evidence is immutable")
        self.root.mkdir(parents=True, exist_ok=True)
        self.metadata = metadata
        self.receipts = []
        write(self.root / "study.json", metadata)
        write(self.root / "source-settings.json", settings)
        self.checkpoint()

    def body(self, receipt, body):
        if len(body) > MAX_BODY + 1 or hashlib.sha256(body).hexdigest() != receipt.body_sha256:
            raise ValueError("Response body does not match its receipt")
        name = "bodies/" + receipt.body_sha256 + ".body"
        target = self.root / name
        if target.exists():
            if target.read_bytes() != body:
                raise ValueError("Stored body was changed")
        else:
            atomic_bytes(target, body)
        return name

    def append(self, row, hosts):
        self.receipts.append(row)
        write(self.root / "receipts.partial.json", self.receipts)
        write(self.root / "hosts.json", hosts)
        self.checkpoint()

    def checkpoint(self, *, result=None, failure=None):
        if result is not None:
            write(self.root / "result.json", result)
        if failure is not None:
            write(self.root / "failure.json", failure)
        names = {"study.json", "source-settings.json"}
        names.update(r["body_file"] for r in self.receipts if r.get("body_file"))
        names.update(name for name in ("receipts.partial.json", "hosts.json", "result.json", "failure.json", "browser-subrequests.json")
                     if (self.root / name).exists())
        entries = []
        for name in sorted(names):
            body = _relative_file(self.root, name).read_bytes()
            entries.append({"path": name, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        manifest = {"format": FORMAT, "experiment_id": self.metadata["experiment_id"],
                    "mode": self.metadata["mode"], "complete": result is not None,
                    "receipt_count": len(self.receipts), "files": entries,
                    "files_sha256": digest(entries)}
        write(self.root / "capture-manifest.json", manifest)
        return manifest


def verify_capture(root, *, allow_partial=False):
    root = Path(root).resolve()
    manifest = read(root / "capture-manifest.json")
    if manifest.get("format") != FORMAT or not isinstance(manifest.get("complete"), bool):
        raise ValueError("Unsupported evidence manifest")
    if not manifest["complete"] and not allow_partial:
        raise ValueError("Capture is incomplete; partial evidence requires explicit opt-in")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not 2 <= len(entries) <= MAX_FILES:
        raise ValueError("Invalid evidence file count")
    if digest(entries) != manifest.get("files_sha256"):
        raise ValueError("Evidence index checksum changed")
    seen, total = set(), 0
    for entry in entries:
        relative = entry["path"]
        if relative in seen:
            raise ValueError("Duplicate evidence path")
        seen.add(relative)
        size = entry.get("bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > MAX_BODY + 1:
            raise ValueError("Invalid evidence file size")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ValueError("Evidence bundle is too large")
        checksum = entry.get("sha256")
        if not isinstance(checksum, str) or not re.fullmatch("[0-9a-f]{64}", checksum):
            raise ValueError("Invalid evidence checksum")
        file = _relative_file(root, relative)
        if not file.is_file() or file.stat().st_size != size or hashlib.sha256(file.read_bytes()).hexdigest() != checksum:
            raise ValueError("Evidence file missing or changed: " + relative)
    required = {"study.json", "source-settings.json"}
    if manifest["receipt_count"]:
        required.add("receipts.partial.json")
    if manifest["complete"]:
        required.add("result.json")
    if not required.issubset(seen):
        raise ValueError("Incomplete evidence index")
    metadata = read(root / "study.json")
    if (metadata["experiment_id"], metadata["mode"]) != (manifest["experiment_id"], manifest["mode"]):
        raise ValueError("Evidence experiment identity changed")
    receipts = read(root / "receipts.partial.json") if "receipts.partial.json" in seen else []
    if len(receipts) != manifest["receipt_count"]:
        raise ValueError("Evidence receipt count changed")
    for row in receipts:
        file = row.get("body_file")
        checksum = row.get("body_sha256")
        if row.get("status") is not None and checksum is None:
            raise ValueError("HTTP response is missing its body evidence")
        if checksum is not None:
            if not file or file not in seen:
                raise ValueError("Response receipt is missing its body")
            body = _relative_file(root, file).read_bytes()
            if hashlib.sha256(body).hexdigest() != checksum or len(body) != row.get("body_bytes"):
                raise ValueError("Response receipt and body disagree")
        elif file is not None:
            raise ValueError("Unverifiable response body")
    result = read(root / "result.json") if manifest["complete"] else None
    if result is not None and (result["receipts"] != receipts or result["experiment_id"] != manifest["experiment_id"]
                               or result.get("mode") != manifest["mode"]):
        raise ValueError("Final result and evidence receipts disagree")
    return {"manifest": manifest, "metadata": metadata, "receipts": receipts, "result": result,
            "settings": read(root / "source-settings.json"), "verified_bytes": total}


def replay_capture(source, output, *, allow_partial=False):
    """Parse verified response bodies, preserving the original evidence time."""
    from .evidence import normalize
    source, output = Path(source).resolve(), guard(Path(output))
    if output.exists() and any(output.iterdir()):
        raise ValueError("Replay output must be empty")
    verified = verify_capture(source, allow_partial=allow_partial)
    rows = []
    for receipt in verified["receipts"]:
        row = {"receipt": receipt, "parsed": False, "observation": None}
        if receipt["status"] == 200 and not receipt.get("error") and not receipt.get("body_incomplete"):
            body = _relative_file(source, receipt["body_file"]).read_bytes()
            page = Page(receipt["url"], body, receipt["observed_at"], "captured_http_replay",
                        receipt.get("content_type", ""))
            observation = normalize(receipt["store"], page, verified["settings"]["stores"][receipt["store"]],
                                    "replay:" + verified["manifest"]["experiment_id"] + ":" + receipt["method"], receipt)
            row.update(parsed=True, observation=observation.to_dict())
        rows.append(row)
    result = {"mode": "captured_http_replay", "source_experiment_id": verified["manifest"]["experiment_id"],
              "source_mode": verified["manifest"]["mode"], "source_complete": verified["manifest"]["complete"],
              "source_manifest_sha256": hashlib.sha256((source / "capture-manifest.json").read_bytes()).hexdigest(),
              "http_requests": 0, "formal_audits_added": 0, "production_prices_added": 0,
              "parsed_pages": sum(row["parsed"] for row in rows), "observations": rows}
    write(output / "result.json", result)
    return result


def smoke_capture(output):
    """Build a synthetic portable bundle for the CI artifact round-trip check."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat()
    output = guard(Path(output))
    capture = Capture(output, {"experiment_id": "lab-fixture-archive-smoke", "mode": "fixture"},
                      {"stores": {"koubou": {"default_condition": "new"}}})
    for status, body in [(200, b'<html><h1>Archive fixture</h1><input id="priceIncTax" value="10980"></html>'),
                         (404, b"<html>Fixture page not found</html>")]:
        receipt = Receipt("https://www.pc-koubou.jp/products/detail.php?product_id=1", status, now,
                          hashlib.sha256(body).hexdigest(), "fixture", {},
                          error=None if status == 200 else "http_404", evidence_mode="fixture",
                          body_bytes=len(body))
        receipt.body_file = capture.body(receipt, body)
        capture.append({"store": "koubou", **asdict(receipt)}, {})
    result = {"experiment_id": "lab-fixture-archive-smoke", "mode": "fixture", "receipts": capture.receipts,
              "http_navigation_attempts": 0}
    capture.checkpoint(result=result)
    verified = verify_capture(output)
    return {"complete": verified["manifest"]["complete"], "receipts": len(verified["receipts"]),
            "http_requests": 0, "mode": "fixture"}

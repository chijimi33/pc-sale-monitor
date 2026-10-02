"""Portable, verified HTTP evidence. Reading or replaying a bundle never fetches."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit

from sale_monitor.http import Page
from .acquire import MAX_BODY, Receipt, http_framing
from .safety import atomic_bytes, digest, guard, read, write

LEGACY_FORMAT = "pc-sale-monitor-http-evidence-v1"
CHECKPOINT_FORMAT = "pc-sale-monitor-http-evidence-v2"
FORMAT = "pc-sale-monitor-http-evidence-v3"
MAX_FILES = 128
MAX_TOTAL_BYTES = 192 * 1024 * 1024
MAX_CHECKPOINTS = 128


def representations(row):
    return [row] + ([row['rendered_dom']] if row.get('rendered_dom') else []) + row.get('auxiliary_responses', [])


def parser_representation(row):
    """Choose the recorded parser input, never relabel historical browser bytes."""
    if row.get('method') == 'browser' or row.get('parser_body_kind') == 'rendered_dom':
        if row.get('parser_body_kind') != 'rendered_dom' or not row.get('rendered_dom'):
            raise ValueError('Browser parser representation is unavailable or legacy-ambiguous')
        return row['rendered_dom']
    return row


def captured_page(root, row, *, method):
    record = parser_representation(row)
    if record.get('body_incomplete') or record.get('body_unavailable'):
        raise ValueError('Parser representation is incomplete')
    body = _relative_file(Path(root), record['body_file']).read_bytes()
    if hashlib.sha256(body).hexdigest() != record['body_sha256'] or len(body) != record['body_bytes']:
        raise ValueError('Capture changed during replay')
    return Page(record.get('url', row['url']), body, row['observed_at'], method, record.get('content_type', ''))


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
        # Exclusive creation closes the race between two writers checking an
        # empty output directory. An abandoned capture is never overwritten.
        with (self.root / "capture-owner").open("xb") as claim:
            claim.write(b"immutable capture\n")
            claim.flush()
            os.fsync(claim.fileno())
        self.metadata = deepcopy(metadata)
        self.receipts = []
        self.hosts = {}
        self.generation = 0
        self.complete = False
        write(self.root / "study.json", metadata)
        write(self.root / "source-settings.json", settings)
        self.fixed_hashes = {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest()
                             for name in ("study.json", "source-settings.json")}
        self.checkpoint()

    def body(self, receipt, body):
        if self.complete:
            raise ValueError("Completed capture is immutable")
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

    def append(self, row, hosts, *, hook=lambda stage: None):
        if self.complete:
            raise ValueError("Completed capture is immutable")
        self.receipts.append(deepcopy(row))
        self.hosts = deepcopy(hosts)
        self.checkpoint(hook=hook)

    def checkpoint(self, *, result=None, failure=None, hook=lambda stage: None):
        if self.complete:
            raise ValueError("Completed capture is immutable")
        if result is not None and (result.get("receipts") != self.receipts or
                (result.get("experiment_id"), result.get("mode")) !=
                (self.metadata["experiment_id"], self.metadata["mode"])):
            raise ValueError("Result does not describe this capture")
        for name, checksum in self.fixed_hashes.items():
            if hashlib.sha256((self.root / name).read_bytes()).hexdigest() != checksum:
                raise ValueError("Immutable capture metadata changed")
        self.generation += 1
        if self.generation > MAX_CHECKPOINTS:
            raise ValueError("Capture checkpoint limit exceeded")
        prefix = f"checkpoints/{self.generation:06d}/"
        directory = self.root / prefix
        directory.mkdir(parents=True, exist_ok=False)
        records = {"receipts": prefix + "receipts.json", "hosts": prefix + "hosts.json"}
        write(self.root / records["receipts"], self.receipts)
        hook("after_receipts_snapshot")
        write(self.root / records["hosts"], self.hosts)
        for name, value in (("result", result), ("failure", failure)):
            if value is not None:
                records[name] = prefix + name + ".json"
                write(self.root / records[name], value)
        browser_log = self.root / "browser-subrequests.json"
        if browser_log.exists():
            records["browser_subrequests"] = prefix + "browser-subrequests.json"
            atomic_bytes(self.root / records["browser_subrequests"], browser_log.read_bytes())
        names = {"study.json", "source-settings.json"}
        names.update(r["body_file"] for row in self.receipts for r in representations(row) if r.get("body_file"))
        names.update(records.values())
        entries = []
        for name in sorted(names):
            body = _relative_file(self.root, name).read_bytes()
            entries.append({"path": name, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        if (len(entries) > MAX_FILES or any(e['bytes'] > MAX_BODY + 1 for e in entries)
                or sum(e['bytes'] for e in entries) > MAX_TOTAL_BYTES):
            raise ValueError('Capture evidence limit exceeded; previous checkpoint is preserved')
        manifest = {"format": FORMAT, "experiment_id": self.metadata["experiment_id"],
                    "mode": self.metadata["mode"], "complete": result is not None,
                    "receipt_count": len(self.receipts), "files": entries,
                    "files_sha256": digest(entries), "generation": self.generation, "records": records}
        write(directory / "manifest.json", manifest)
        hook("before_publish")
        # This is the sole authoritative publication boundary. Referenced files
        # never change, so a killed writer leaves the old or new snapshot intact.
        write(self.root / "capture-manifest.json", manifest)
        self.complete = result is not None
        hook("after_publish")
        # Convenience exports are not part of the integrity index. Readers use
        # manifest.records, including when these copies are absent or outdated.
        for name, alias in (("receipts", "receipts.partial.json"), ("hosts", "hosts.json"),
                            ("result", "result.json"), ("failure", "failure.json")):
            if name in records:
                atomic_bytes(self.root / alias, (self.root / records[name]).read_bytes())
        return manifest


def verify_capture(root, *, allow_partial=False):
    root = Path(root).resolve()
    manifest = read(root / "capture-manifest.json")
    return _verify_manifest(root, manifest, allow_partial=allow_partial)


def _verify_manifest(root, manifest, *, allow_partial=False):
    if (not isinstance(manifest, dict) or manifest.get("format") not in (FORMAT, CHECKPOINT_FORMAT, LEGACY_FORMAT)
            or not isinstance(manifest.get("complete"), bool)):
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
    if manifest["format"] in (FORMAT, CHECKPOINT_FORMAT):
        generation = manifest.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or not 1 <= generation <= MAX_CHECKPOINTS:
            raise ValueError("Invalid checkpoint generation")
        records = manifest.get("records")
        if not isinstance(records, dict) or not {"receipts", "hosts"}.issubset(records):
            raise ValueError("Checkpoint record map is missing")
        prefix = f"checkpoints/{generation:06d}/"
        for name in records.values():
            _relative_file(root, name)
            if not name.startswith(prefix) or name not in seen:
                raise ValueError("Checkpoint record is outside its generation")
    else:
        records = {key: name for key, name in (("receipts", "receipts.partial.json"), ("hosts", "hosts.json"),
                                              ("result", "result.json"), ("failure", "failure.json")) if name in seen}
    required = {"study.json", "source-settings.json"}
    if manifest["receipt_count"] and "receipts" not in records:
        raise ValueError("Receipt record is missing")
    if manifest["complete"] and "result" not in records:
        raise ValueError("Final result record is missing")
    if not required.issubset(seen):
        raise ValueError("Incomplete evidence index")
    metadata = read(root / "study.json")
    if (metadata["experiment_id"], metadata["mode"]) != (manifest["experiment_id"], manifest["mode"]):
        raise ValueError("Evidence experiment identity changed")
    receipts = read(root / records["receipts"]) if "receipts" in records else []
    if len(receipts) != manifest["receipt_count"]:
        raise ValueError("Evidence receipt count changed")
    for row in receipts:
        details = row.get('http_body')
        if details is not None:
            try:
                framing, framing_error = http_framing(row['status'], details['headers'])
                valid = (details['schema'] == 1 and type(details['complete']) is bool
                         and type(details['received_bytes']) is int and details['received_bytes'] >= 0
                         and details['received_bytes'] == row['body_bytes']
                         and all(details[key] == value for key, value in framing.items())
                         and (details['error'] is None or isinstance(details['error'], str) and bool(details['error']))
                         and details['complete'] == (details['error'] is None)
                         and row.get('body_incomplete') == (not details['complete'])
                         and (details['complete'] or bool(row.get('error')))
                         and (framing_error is None or details['error'] == framing_error)
                         and (not details['complete'] or details['received_bytes'] <= MAX_BODY
                              and (framing['expected_bytes'] is None or details['received_bytes'] == framing['expected_bytes'])))
            except (KeyError, TypeError, ValueError, AttributeError):
                valid = False
            if not valid:
                raise ValueError('HTTP body completeness evidence is inconsistent')
        if row.get('body_incomplete') and not row.get('error'):
            raise ValueError('Successful receipt has an incomplete body')
        if manifest['format'] == FORMAT and row.get('method') == 'browser' and row.get('attempts'):
            if row.get('body_kind') != 'browser_response_body' or row.get('parser_body_kind') != 'rendered_dom':
                raise ValueError('Invalid browser representation kinds')
            dom = row.get('rendered_dom')
            if dom and (dom.get('body_kind') != 'rendered_dom' or dom.get('content_type') != 'text/html; charset=utf-8'
                        or not dom.get('observed_at')):
                raise ValueError('Invalid rendered DOM provenance')
            if not row.get('error') and (not dom or row.get('response_url') != row['url'] or dom['url'] != row['url']
                                        or dom.get('body_incomplete') or not dom.get('body_sha256')
                                        or row.get('auxiliary_responses')):
                raise ValueError('Successful browser receipt has incomplete or blocked provenance')
            for auxiliary in row.get('auxiliary_responses', []):
                if (auxiliary.get('body_kind') != 'browser_response_body'
                        or urlsplit(auxiliary['url']).hostname != urlsplit(row['url']).hostname
                        or not auxiliary.get('observed_at') or auxiliary.get('status') is None):
                    raise ValueError('Invalid auxiliary response provenance')
        for record in representations(row):
            file, checksum = record.get('body_file'), record.get('body_sha256')
            missing = record.get('body_unavailable')
            if missing and (checksum is not None or file is not None or record.get('body_bytes') is not None):
                raise ValueError('Unavailable response body contains contradictory evidence')
            if record.get('status') is not None and checksum is None:
                if not (manifest['format'] == FORMAT and row.get('method') == 'browser' and missing and row.get('error')):
                    raise ValueError('HTTP response is missing its body evidence')
            if checksum is not None:
                if not file or file not in seen:
                    raise ValueError('Response receipt is missing its body')
                body = _relative_file(root, file).read_bytes()
                if hashlib.sha256(body).hexdigest() != checksum or len(body) != record.get('body_bytes'):
                    raise ValueError('Response receipt and body disagree')
                if len(body) > MAX_BODY and not record.get('body_incomplete'):
                    raise ValueError('Oversized body is not marked incomplete')
            elif file is not None:
                raise ValueError('Unverifiable response body')
    result = read(root / records["result"]) if manifest["complete"] else None
    if result is not None and (result["receipts"] != receipts or result["experiment_id"] != manifest["experiment_id"]
                               or result.get("mode") != manifest["mode"]):
        raise ValueError("Final result and evidence receipts disagree")
    return {"manifest": manifest, "metadata": metadata, "receipts": receipts, "result": result,
            "settings": read(root / "source-settings.json"), "verified_bytes": total,
            "hosts": read(root / records["hosts"]) if "hosts" in records else {}}


def recover_capture(source, output):
    """Reconstruct a verified snapshot in a new directory; preserve the source."""
    source, output = Path(source).resolve(), guard(Path(output))
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Recovery requires a separate directory outside the source")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Recovery output must be empty")
    try:
        verify_capture(source, allow_partial=True)
    except (ValueError, KeyError, TypeError, OSError) as error:
        pointer_error = str(error)
    else:
        raise ValueError("Capture already verifies; recovery is unnecessary")
    candidates = list((source / "checkpoints").glob("*/manifest.json"))
    if not candidates or len(candidates) > MAX_CHECKPOINTS:
        raise ValueError("No bounded checkpoint history is available for recovery")
    valid, invalid = [], []
    for file in candidates:
        try:
            manifest = read(file)
            verified = _verify_manifest(source, manifest, allow_partial=True)
            if file.parent.name != f"{manifest['generation']:06d}":
                raise ValueError("Checkpoint directory disagrees with generation")
            valid.append(verified)
        except (ValueError, KeyError, TypeError, OSError) as error:
            invalid.append({"path": file.relative_to(source).as_posix(), "error": str(error)})
    if not valid:
        raise ValueError("No valid checkpoint; corrupt evidence was not treated as empty")
    chosen = max(valid, key=lambda row: row["manifest"]["generation"])
    if not chosen["receipts"]:
        raise ValueError("No verified receipts; an empty initial checkpoint is not evidence recovery")
    original = chosen["manifest"]
    recovery = {"source_manifest_error": pointer_error, "selected_generation": original["generation"],
                "selected_receipt_count": len(chosen["receipts"]), "invalid_checkpoints": invalid,
                "original_snapshot_complete": original["complete"], "source_preserved": True,
                "commit_status": "unknown_after_pointer_damage",
                "requires_partial_opt_in": True, "http_requests": 0}
    # Validate before creating any output, then copy only the chosen snapshot's
    # indexed files. All corrupt/uncommitted files remain untouched at source.
    output.mkdir(parents=True, exist_ok=True)
    with (output / "capture-owner").open("xb") as claim:
        claim.write(b"recovered capture\n")
        claim.flush()
        os.fsync(claim.fileno())
    for entry in original["files"]:
        body = _relative_file(source, entry["path"]).read_bytes()
        if len(body) != entry["bytes"] or hashlib.sha256(body).hexdigest() != entry["sha256"]:
            raise ValueError("Source changed during recovery")
        atomic_bytes(output / entry["path"], body)
    write(output / "recovery.json", recovery)
    body = (output / "recovery.json").read_bytes()
    manifest = deepcopy(original)
    manifest["complete"] = False  # A damaged pointer cannot prove publication.
    manifest["recovery"] = "recovery.json"
    manifest["files"].append({"path": "recovery.json", "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
    manifest["files"].sort(key=lambda entry: entry["path"])
    manifest["files_sha256"] = digest(manifest["files"])
    write(output / "capture-manifest.json", manifest)
    verify_capture(output, allow_partial=True)
    return recovery


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
            if receipt['method'] == 'browser' and verified['manifest']['format'] != FORMAT:
                row['skip_reason'] = 'legacy_browser_representation_ambiguous'
                rows.append(row)
                continue
            page = captured_page(source, receipt, method='captured_http_replay')
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

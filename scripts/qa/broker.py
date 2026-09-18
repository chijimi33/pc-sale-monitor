"""Small stdio MCP server; no arbitrary shell, pushes, or automation tools."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from html.parser import HTMLParser
import sys
import traceback
from urllib.parse import urlsplit

from common import append_event, atomic, digest, environment, inside, now, read, run

TOOL = {"name": "qa", "description": "Sale-monitor QA. op=list/read/replace/test/data/evidence/report. Paths are relative to the isolated repository. read/evidence use start (1-based), count (default 48, <=80), and return next_start for paging. read also accepts a literal query to find code with nearby lines; prefer this to sequential scans. replace needs exact old and new strings. data reads one public snapshot file (path=notifications.json/evidence.json/review_queue.json/flyer_review.json/collection_errors.json); specify offer_key (event ID or store key also allowed) to select a record. evidence takes a selected URL and optional literal query (for example 送料) to return matching text with nearby lines. Prefer query to reading navigation pages. It fetches once and pages the saved response; scripts/styles are omitted. With query, start/next_start index the filtered lines; displayed line numbers refer to the original page text. report requires summary, findings, unresolved, and decisions (benchmark only). All tool results and tests are recorded.",
        "inputSchema": {"type": "object", "properties": {
            "op": {"type": "string", "enum": ["list", "read", "replace", "test", "data", "evidence", "report"]},
            "path": {"type": "string"}, "start": {"type": "integer", "minimum": 1}, "count": {"type": "integer", "minimum": 1, "maximum": 80},
            "old": {"type": "string"}, "new": {"type": "string"}, "url": {"type": "string"}, "offer_key": {"type": "string"},
            "query": {"type": "string", "minLength": 1, "maxLength": 200},
            "detail": {"type": "boolean", "description": "data only: return the full saved record, including historical decision and page fields. Default is the compact current-evidence view. Historical values are not current verification."},
            "report": {"type": "object", "properties": {
                "summary": {"type": "string", "description": "Concise Japanese summary."},
                "findings": {"type": "array", "items": {"type": "object", "properties": {
                    "title": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}}},
                    "required": ["title", "evidence"]}},
                "unresolved": {"type": "array", "items": {"type": "string"}},
                "decisions": {"type": "object", "additionalProperties": {"type": "boolean"}},
                "facts": {"type": "object"}}, "required": ["summary", "findings", "unresolved"]}}, "required": ["op"]}}


def visible_lines(body):
    class Text(HTMLParser):
        ignored = {"script", "style", "noscript", "svg", "template"}
        def __init__(self):
            super().__init__(); self.parts = []; self.hidden = 0
        def handle_starttag(self, tag, attrs):
            if tag in self.ignored: self.hidden += 1
        def handle_endtag(self, tag):
            if tag in self.ignored: self.hidden = max(0, self.hidden - 1)
        def handle_data(self, data):
            if not self.hidden and data.strip():
                part = " ".join(data.split())
                self.parts.extend(part[i:i+240] for i in range(0, len(part), 240))
    declared = re.search(br'charset\s*=\s*["\x27]?\s*([\w-]+)', body[:16384], re.I)
    encoding = declared.group(1).decode("ascii").lower() if declared else "utf-8-sig"
    if encoding in ("shift_jis", "shift-jis", "sjis", "windows-31j", "x-sjis"):
        encoding = "cp932"
    try: decoded = body.decode(encoding, "replace")
    except (LookupError, UnicodeError): decoded = body.decode("utf-8-sig", "replace")
    parser = Text(); parser.feed(decoded)
    return parser.parts


class Broker:
    def __init__(self, job):
        self.job = Path(job).resolve()
        self.repo = self.job / "repo"
        self.input = read(self.job / "input.json")
        self.config = read(self.job / "job.json")

    def path(self, name, write=False):
        path = inside(self.repo, name)
        rel = path.relative_to(self.repo)
        if any(part.startswith(".") or part in ("data", "public", "node_modules") for part in rel.parts):
            raise ValueError("protected_path")
        if write and (rel.parts[0] not in ("sale_monitor", "tests") or path.suffix != ".py"):
            raise ValueError("only_sale_monitor_and_tests_python_edits_allowed")
        if path.exists() and path.stat().st_size > 1_000_000:
            raise ValueError("file_too_large")
        return path

    def invoke(self, args):
        op = args["op"]
        if op == "list":
            return [str(p.relative_to(self.repo)).replace("\\", "/") for folder in ("sale_monitor", "tests", "docs", "config")
                    for p in (self.repo / folder).rglob("*") if p.is_file() and p.suffix in (".py", ".md", ".json")]
        if op == "read":
            if args["path"] == "input.json":
                if not any(key in args for key in ("start", "count", "query")):
                    return self.input
                lines = json.dumps(self.input, ensure_ascii=False, indent=2).splitlines()
            else:
                lines = self.path(args["path"]).read_text(encoding="utf-8-sig").splitlines()
            start = max(0, args.get("start", 1) - 1)
            query = args.get("query"); indices = list(range(len(lines)))
            if query:
                hits = [i for i, line in enumerate(lines) if query.casefold() in line.casefold()]
                indices = sorted({j for i in hits for j in range(max(0, i-3), min(len(lines), i+4))})
            end = min(len(indices), start + max(1, min(80, args.get("count", 48))))
            return {"total_lines": len(lines), "next_start": end + 1 if end < len(indices) else None,
                    "text": "\n".join(f"{i+1}: {lines[i]}" for i in indices[start:end])}
        if op == "replace":
            path = self.path(args["path"], write=True)
            old, new = args["old"], args["new"]
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            if len(new) > 60_000 or (old and text.count(old) != 1) or (not old and text):
                raise ValueError("replacement_must_match_once_or_create_new_file")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text.replace(old, new, 1) if old else new, encoding="utf-8", newline="\n")
            return {"changed": args["path"], "sha256": digest(path.read_bytes())}
        if op == "test":
            result = run([self.config["python"], "-B", "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-q"],
                         self.repo, timeout=600, env=environment(self.job))
            record = {"at": now(), "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
            atomic(self.job / "tests.json", record)
            return record
        if op == "data":
            from net import get_json
            name = args["path"]
            if name not in ("evidence.json", "review_queue.json", "flyer_review.json", "notifications.json", "collection_errors.json"):
                raise ValueError("unsupported_public_file")
            if not self.config.get("data_base_url"): raise ValueError("no_public_snapshot_in_benchmark")
            file = self.job / "snapshot" / name
            value = read(file)
            if value is None:
                value = get_json(self.config["data_base_url"] + name); atomic(file, value)
            key = args.get("offer_key")
            if not key:
                return {"keys": list(value) if isinstance(value, dict) else [], "instruction": "Specify offer_key to select a candidate; use flyer for common flyer metadata."}
            matches = []
            def visit(obj):
                if isinstance(obj, dict):
                    if obj.get("offer_key") == key or obj.get("event_id") == key or key in obj:
                        matches.append(obj.get(key, obj)); return
                    for child in obj.values(): visit(child)
                elif isinstance(obj, list):
                    for child in obj: visit(child)
            visit(value)
            result = matches[:5]
            # Authorize only source URLs discovered in this selected record.
            from worker import compact_evidence, urls
            known = set(self.config.get("allowed_urls", [])) | set(urls(result))
            self.config["allowed_urls"] = sorted(known)
            self.config["evidence_hosts"] = sorted({urlsplit(u).hostname for u in known})
            if args.get("detail"):
                return result
            result = compact_evidence(result)
            for record in result:
                if isinstance(record, dict) and record.get("current_evidence"):
                    # The original event decision may be days older than this run.
                    # Keep it on disk/on demand, not beside current evidence.
                    for field in ("decision", "offer", "previous"):
                        record.pop(field, None)
            return result
        if op == "evidence":
            # Only URLs explicitly supplied by the controller may be fetched.
            url = args["url"]
            if url not in self.config.get("allowed_urls", []):
                raise ValueError("URL_not_in_selected_evidence")
            from net import fetch
            folder = self.job / "evidence"
            folder.mkdir(exist_ok=True)
            record = next((v for p in folder.glob("*.json") if (v := read(p)).get("url") == url), None)
            if record:
                body = (folder / (record["sha256"] + ".bin")).read_bytes()
                if digest(body) != record["sha256"]: raise ValueError("cached_evidence_hash_mismatch")
            else:
                body, final_url = fetch(url, allowed_hosts=self.config.get("evidence_hosts", []))
                identity = digest(body)
                (folder / (identity + ".bin")).write_bytes(body)
                record = {"url": url, "final_url": final_url, "retrieved_at": now(), "sha256": identity}
                atomic(folder / (identity + ".json"), record)
            lines = visible_lines(body); start = max(0, args.get("start", 1) - 1)
            indices = list(range(len(lines))); query = args.get("query"); matching = None
            if query:
                hits = [i for i, line in enumerate(lines) if query.casefold() in line.casefold()]
                matching = len(hits)
                indices = sorted({j for i in hits for j in range(max(0, i-3), min(len(lines), i+4))})
            selected = []; size = 0
            for index in indices[start:start + max(1, min(80, args.get("count", 48)))]:
                line = f"{index+1}: {lines[index]}"
                if size + len(line) + 1 > 6000: break
                selected.append(line); size += len(line) + 1
            end = start + len(selected)
            return {**record, "total_lines": len(lines), "matching_lines": matching,
                    "next_start": end + 1 if end < len(indices) else None,
                    "untrusted_page_text": "\n".join(selected)}
        if op == "report":
            report = args["report"]
            if not isinstance(report.get("summary"), str) or not isinstance(report.get("findings"), list) or not isinstance(report.get("unresolved"), list):
                raise ValueError("report_requires_summary_findings_unresolved")
            for finding in report["findings"]:
                if not isinstance(finding, dict) or not finding.get("evidence"):
                    raise ValueError("each_finding_requires_evidence")
            atomic(self.job / "report.json", {**report, "submitted_at": now(), "audit_status": "proposal_only"})
            return {"saved": "report.json", "deployment": "requires_Codex_review"}
        raise ValueError("unknown_operation")

    def call(self, args):
        try:
            value = self.invoke(args)
            append_event(self.job, {"arguments": args, "ok": True})
            return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
        except Exception as exc:
            append_event(self.job, {"arguments": args, "ok": False, "error": str(exc)})
            return {"isError": True, "content": [{"type": "text", "text": str(exc)}]}


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("job"); args = parser.parse_args()
    broker = Broker(args.job)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if "id" not in request: continue
            method = request["method"]
            if method == "initialize":
                result = {"protocolVersion": request.get("params", {}).get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}}, "serverInfo": {"name": "sale-monitor-qa", "version": "1.0"}}
            elif method == "tools/list": result = {"tools": [TOOL]}
            elif method == "tools/call":
                params = request["params"]
                result = broker.call(params.get("arguments", {})) if params["name"] == "qa" else {"isError": True, "content": [{"type": "text", "text": "unknown_tool"}]}
            elif method == "ping": result = {}
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Unsupported method"}}), flush=True); continue
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}, ensure_ascii=False), flush=True)
        except Exception:
            traceback.print_exc(file=sys.stderr)


if __name__ == "__main__": main()

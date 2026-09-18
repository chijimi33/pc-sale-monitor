"""Small stdio MCP server; no arbitrary shell, pushes, or automation tools."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import traceback
from urllib.parse import urlsplit

from common import append_event, atomic, digest, environment, inside, now, read, run

TOOL = {"name": "qa", "description": "Sale-monitor QA. op=list/read/replace/test/data/evidence/report. Paths are relative to the isolated repository. read optionally takes start (1-based), count (<=160). replace needs exact old and new strings. data reads one public snapshot file (path=notifications.json/evidence.json/review_queue.json/flyer_review.json/collection_errors.json); specify offer_key (event ID or store key also allowed) to select a record. evidence takes a URL from selected records. report requires a report object with summary, findings, unresolved, and decisions (benchmark only). All tool results and tests are recorded.",
        "inputSchema": {"type": "object", "properties": {
            "op": {"type": "string", "enum": ["list", "read", "replace", "test", "data", "evidence", "report"]},
            "path": {"type": "string"}, "start": {"type": "integer"}, "count": {"type": "integer"},
            "old": {"type": "string"}, "new": {"type": "string"}, "url": {"type": "string"}, "offer_key": {"type": "string"},
            "report": {"type": "object", "properties": {
                "summary": {"type": "string", "description": "Concise Japanese summary."},
                "findings": {"type": "array", "items": {"type": "object", "properties": {
                    "title": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}}},
                    "required": ["title", "evidence"]}},
                "unresolved": {"type": "array", "items": {"type": "string"}},
                "decisions": {"type": "object", "additionalProperties": {"type": "boolean"}},
                "facts": {"type": "object"}}, "required": ["summary", "findings", "unresolved"]}}, "required": ["op"]}}


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
                return self.input
            lines = self.path(args["path"]).read_text(encoding="utf-8-sig").splitlines()
            start = max(0, args.get("start", 1) - 1)
            return {"total_lines": len(lines), "text": "\n".join(f"{i+1}: {line}" for i, line in enumerate(lines[start:start + min(160, args.get("count", 160))], start))}
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
            from worker import urls
            known = set(self.config.get("allowed_urls", [])) | set(urls(result))
            self.config["allowed_urls"] = sorted(known)
            self.config["evidence_hosts"] = sorted({urlsplit(u).hostname for u in known})
            return result
        if op == "evidence":
            # Only URLs explicitly supplied by the controller may be fetched.
            url = args["url"]
            if url not in self.config.get("allowed_urls", []):
                raise ValueError("URL_not_in_selected_evidence")
            from net import fetch
            body, final_url = fetch(url, allowed_hosts=self.config.get("evidence_hosts", []))
            identity = digest(body)
            folder = self.job / "evidence"
            folder.mkdir(exist_ok=True)
            (folder / (identity + ".bin")).write_bytes(body)
            record = {"url": url, "final_url": final_url, "retrieved_at": now(), "sha256": identity}
            atomic(folder / (identity + ".json"), record)
            from html.parser import HTMLParser
            class Text(HTMLParser):
                def __init__(self):
                    super().__init__(); self.parts = []
                def handle_data(self, data):
                    if data.strip(): self.parts.append(data.strip())
            parser = Text(); parser.feed(body.decode("utf-8", "replace"))
            return {**record, "untrusted_page_text": "\n".join(parser.parts)[:24000]}
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

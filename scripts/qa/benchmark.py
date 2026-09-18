"""Two identical task repetitions per quantization; never select on speed alone."""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import statistics
import sys
import traceback

from agent import execute
from common import ROOT, atomic, capture_patch, digest, finalize, git, lock, now, read, require_root
from model import PROFILES, server

POLICY = """楽天は除外、監視分母10。Aには候補以外の独立販売者2者以上、最安総額から10%以上かつ500円以上安いことが必要（境界を含む）。送料不明、期限切れ、残数0、今回未取得、型番相違は現在比較不可。同じ販売者は1者。工房共通チラシは1回解析し6店舗共有、掲載数量は店舗在庫に変換しない。Bは1年の観測最安を500円以上更新＋現在比較1店以上、または90日内30日以上の観測幅、3日以上の日別最安の中央値より10%以上かつ500円以上安く期間最安以下。転記しただけの古い価格は現在価格にならない。"""
CASES = [
    ("A_boundary", True, {"price": 4500, "shipping": 0, "model": "X", "run": "r", "stock": 1, "expired": False}, [("b",5000,"X","r"),("c",5100,"X","r")]),
    ("A_499yen", False, {"price": 4491, "shipping": 0, "model": "X", "run": "r", "stock": 1, "expired": False}, [("b",4990,"X","r"),("c",5100,"X","r")]),
    ("A_9percent", False, {"price": 9100, "shipping": 0, "model": "X", "run": "r", "stock": 1, "expired": False}, [("b",10000,"X","r"),("c",11000,"X","r")]),
    ("duplicate_seller", False, {"price": 4000, "shipping": 0, "model": "X", "run": "r", "stock": 1, "expired": False}, [("b",5000,"X","r"),("b",5100,"X","r")]),
    ("unknown_shipping", False, {"price": 4000, "shipping": None, "model": "X", "run": "r", "stock": 1, "expired": False}, [("b",5000,"X","r"),("c",5100,"X","r")]),
    ("zero_stock", False, {"price": 4000, "shipping": 0, "model": "X", "run": "r", "stock": 0, "expired": False}, [("b",5000,"X","r"),("c",5100,"X","r")]),
    ("expired", False, {"price": 4000, "shipping": 0, "model": "X", "run": "r", "stock": 1, "expired": True}, [("b",5000,"X","r"),("c",5100,"X","r")]),
    ("stale_price", False, {"price": 4000, "shipping": 0, "model": "X", "run": "old", "stock": 1, "expired": False}, [("b",5000,"X","r"),("c",5100,"X","r")]),
    ("wrong_model", False, {"price": 4000, "shipping": 0, "model": "X", "run": "r", "stock": 1, "expired": False}, [("b",5000,"Y","r"),("c",5100,"X","r")]),
    ("rakuten_comparison", False, {"price": 4000, "shipping": 0, "model": "X", "run": "r", "stock": 1, "expired": False}, [("rakuten",5000,"X","r"),("c",5100,"X","r")]),
    ("stale_comparison", False, {"price": 4000, "shipping": 0, "model": "X", "run": "r", "stock": 1, "expired": False}, [("b",5000,"X","old"),("c",5100,"X","r")]),
]
FACTS = {"denominator": 10, "flyer_parse_count": 1, "branch_stock_from_printed_quantity": None,
         "fetch_failure_is_stockout": False, "copy_refreshes_price": False,
         "B_499_yen_year_record_qualifies": False, "B_no_current_comparison_qualifies": False,
         "B_29_day_span_qualifies": False, "audit_proposal_is_reviewed": False}
BUG = '''def qualifies(offer, comparisons, current_run="r"):
    """Comparisons are (seller_id, total_yen, exact_model, run_id); candidate seller is 'a'."""
    prices = [c[1] for c in comparisons]
    if len(prices) < 2:
        return False
    total = offer["price"] + (offer["shipping"] or 0)
    return min(prices) - total > 500 or total <= min(prices) * 0.9
'''


def prepare(job, config):
    repo = job / "repo"
    (repo / "sale_monitor").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "sale_monitor/__init__.py").write_text("")
    (repo / "sale_monitor/check.py").write_text(BUG, encoding="utf-8")
    (repo / "tests/test_check.py").write_text('''import unittest
from sale_monitor.check import qualifies
class Boundary(unittest.TestCase):
    def test_inclusive(self):
        self.assertTrue(qualifies(dict(price=4500,shipping=0,model="X",run="r",stock=1,expired=False), [("b",5000,"X","r"),("c",5100,"X","r")]))
''', encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=QA fixture", "-c", "user.email=qa@localhost", "commit", "-qm", "Benchmark fixture")
    inp = {"policy": POLICY, "cases": [{"id": name, "offer": offer, "comparisons": comparisons} for name, _, offer, comparisons in CASES],
           "fact_questions": list(FACTS), "instructions": "qa read input.json と sale_monitor/check.py を読み、全ケースのA可否を判断し、関数を汎用的に修正、必要な回帰テストを追加・実行。report.decisionsに{id: boolean}、report.factsに各設問の値を保存。findingsはtitleとevidence配列を持つオブジェクト。報告書は簡潔にし、全テストを文章で繰り返し説明しない。外部ページは不要。確認していない事実を作らない。"}
    atomic(job / "input.json", inp)
    atomic(job / "job.json", {"python": config["python"], "allowed_urls": []})
    return digest(json.dumps(inp, sort_keys=True).encode())


def score(job):
    report = read(job / "report.json", {})
    decisions = report.get("decisions", {})
    facts = report.get("facts", {})
    correct = sum(decisions.get(name) is expected for name, expected, *_ in CASES)
    correct += sum(key in facts and facts[key] == value and type(facts[key]) is type(value) for key, value in FACTS.items())
    repair = 0; error = None
    try:
        spec = importlib.util.spec_from_file_location("fixture_check", job / "repo/sale_monitor/check.py")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        for _, expected, offer, comparisons in CASES:
            repair += module.qualifies(offer, comparisons) is expected
        # Held-out values ensure the implementation generalizes beyond the presented cases.
        for price, expected in ((9000, True), (9001, False), (9500, False)):
            repair += module.qualifies(dict(price=price, shipping=0, model="Z", run="n", stock=2, expired=False), [("b",10000,"Z","n"),("c",11000,"Z","n")], "n") is expected
    except Exception as exc: error = repr(exc)
    events = [json.loads(line) for line in (job / "tools.jsonl").read_text(encoding="utf-8").splitlines()] if (job / "tools.jsonl").exists() else []
    errors = [x for x in events if not x["ok"]]
    tests = read(job / "tests.json", {})
    safety = not any("outside_root" in x.get("error", "") or "protected_path" in x.get("error", "") for x in errors)
    passed = correct == len(CASES) + len(FACTS) and repair == len(CASES) + 3 and tests.get("exit_code") == 0 and safety
    return {"judgment_correct": correct, "judgment_total": len(CASES)+len(FACTS), "repair_correct": repair,
            "repair_total": len(CASES)+3, "tests_passed": tests.get("exit_code") == 0, "tool_errors": len(errors),
            "no_prohibited_write_attempt": safety, "repair_error": error, "passed": passed}


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--config", default=str(ROOT / "config.json")); args = parser.parse_args()
    require_root(); config = read(args.config)
    with lock(ROOT / "worker.lock"):
        batch = ROOT / "benchmarks" / config["benchmark_id"]
        batch.mkdir(parents=True, exist_ok=True)
        atomic(batch / "controller.json", {"pid": os.getpid(), "started_at": now(), "python": sys.executable,
               "script": str(Path(__file__).resolve()), "harness_sha256": {p.name: digest(p.read_bytes()) for p in Path(__file__).parent.glob("*.py")}})
        results = []
        for model in PROFILES:
            for repeat in (1, 2):
                job = batch / f"{model}-{repeat}"
                done = read(job / "manifest.json")
                if done:
                    results.append(done); continue
                if job.exists():
                    # Preserve partial work; a fresh attempt has a separate directory.
                    job = batch / f"{model}-{repeat}-retry-{len(list(batch.glob(model+'*')))}"
                job.mkdir()
                atomic(batch / "progress.json", {"model": model, "repeat": repeat, "job": str(job), "started_at": now()})
                print(f"START {model} repeat={repeat}", flush=True)
                try:
                    fingerprint = prepare(job, config)
                    with server(model, job / "server"):
                        execution = execute(job, config, "Execute the complete benchmark instructions in input.json and save the report.", timeout=3600)
                    metrics = score(job)
                    metrics["passed"] = metrics["passed"] and execution["exit_code"] == 0
                    (job / "patch.diff").write_text(capture_patch(job / "repo"), encoding="utf-8")
                    result = finalize(job, "complete", model=model, repeat=repeat, input_sha256=fingerprint, execution=execution, score=metrics)
                except Exception as exc:
                    (job / "failure-traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
                    result = finalize(job, "failed", model=model, repeat=repeat, error=repr(exc), score={"passed": False})
                results.append(result)
                atomic(batch / "results.json", results)
                print(json.dumps({"model": model, "repeat": repeat, "score": result["score"], "error": result.get("error")}), flush=True)
        eligible = []
        for model in PROFILES:
            rows = [r for r in results if r["model"] == model]
            if len(rows) == 2 and all(r["score"]["passed"] for r in rows):
                eligible.append((statistics.median(r["execution"]["seconds"] for r in rows),
                                 max(r["execution"].get("peak_rss_bytes") or float("inf") for r in rows), model))
        selected = min(eligible)[2] if eligible else None
        atomic(batch / "selection.json", {"selected": selected, "selected_at": now(), "results": results,
                                          "requires_codex_review": True, "benchmark": "sale-monitor-v1"})
        atomic(batch / "progress.json", {"status": "completed", "finished_at": now(), "selected": selected})
        print("SELECTED " + str(selected), flush=True)


if __name__ == "__main__": main()

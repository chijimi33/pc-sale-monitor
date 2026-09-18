"""Two identical task repetitions per quantization; never select on speed alone."""
from __future__ import annotations
import argparse
from contextlib import ExitStack, contextmanager
import importlib.util
import json
import os
from pathlib import Path
import statistics
import sys
import traceback
import time

from agent import execute
from common import ROOT, atomic, capture_patch, digest, finalize, git, lock, now, read, require_root
from model import PROFILES, server

POLICY = """楽天は除外、監視分母10。Aには候補以外の独立販売者2者以上、最安総額から10%以上かつ500円以上安いことが必要（境界を含む）。送料不明、期限切れ、残数0、今回未取得、型番相違は現在比較不可。同じ販売者は1者。工房共通チラシは1回解析し6店舗共有、掲載数量は店舗在庫に変換しない。未確認の数量・金額はnull。Bはどちらの方式でも現在比較1店以上が必須。そのうえで1年の観測最安を500円以上更新、または90日内30日以上の観測幅、3日以上の日別最安の中央値より10%以上かつ500円以上安く期間最安以下。転記しただけの古い価格は現在価格にならない。"""
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
QUESTIONS = {
    "denominator": "楽天を除いた監視対象店数の分母を整数で答える。",
    "flyer_parse_count": "同じ内容ハッシュの工房共通チラシを6店舗に適用するための解析回数を整数で答える。",
    "branch_stock_from_printed_quantity": "共通チラシに全店合計12個と記載。枚方店の現在在庫は未確認。この店の現在在庫数として保存する値を答える（整数またはnull）。変換可否の真偽値ではなく、在庫数そのものを答える。",
    "fetch_failure_is_stockout": "商品ページ取得が失敗しただけで、在庫切れと確定してよいか。真偽値で答える。",
    "copy_refreshes_price": "昨日取得した価格を今日転記しただけで、今日取得した現在価格とみなしてよいか。真偽値で答える。",
    "B_499_yen_year_record_qualifies": "Bの年内観測最安更新方式だけを考える。必要な他条件は満たすが更新幅は499円。この方式で合格か。真偽値で答える。",
    "B_no_current_comparison_qualifies": "現在比較が0店。履歴のどちらの条件も満たすとして、Bとして合格か。真偽値で答える。",
    "B_29_day_span_qualifies": "年内最安更新方式は不成立。中央値方式の他条件はすべて満たすが観測幅が29日。この方式で合格か。真偽値で答える。",
    "audit_proposal_is_reviewed": "Qwenが報告書へ書いた監査提案を、Codexの証拠照合前に正式な確認済み件数へ加えてよいか。真偽値で答える。",
}
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
           "fact_questions": QUESTIONS, "instructions": "qa read input.json と sale_monitor/check.py を読み、全ケースのA可否を判断し、関数を汎用的に修正、必要な回帰テストを追加・実行。report.decisionsに{id: boolean}、report.factsに各設問の値を保存。findingsはtitleとevidence配列を持つオブジェクト。報告書は簡潔にし、全テストを文章で繰り返し説明しない。外部ページは不要。確認していない事実を作らない。"}
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
    safety = not any(any(reason in x.get("error", "") for reason in
                        ("outside_root", "protected_path", "only_sale_monitor_and_tests_python_edits_allowed")) for x in errors)
    passed = correct == len(CASES) + len(FACTS) and repair == len(CASES) + 3 and tests.get("exit_code") == 0 and safety
    return {"judgment_correct": correct, "judgment_total": len(CASES)+len(FACTS), "repair_correct": repair,
            "repair_total": len(CASES)+3, "tests_passed": tests.get("exit_code") == 0, "tool_errors": len(errors),
            "no_prohibited_write_attempt": safety, "repair_error": error, "passed": passed}


@contextmanager
def benchmark_lock(wait):
    deadline = time.monotonic() + (6 * 3600 if wait else 0)
    while True:
        guard = lock(ROOT / "worker.lock")
        try:
            guard.__enter__(); break
        except (BlockingIOError, PermissionError):
            if time.monotonic() >= deadline: raise
            time.sleep(10)
    try: yield
    finally: guard.__exit__(None, None, None)


def choose_model(results):
    # Startup/resource failures are missing measurements, not model-quality failures.
    expected = {(m, r) for m in PROFILES for r in (1, 2)}
    if len(results) != 6 or {(r["model"], r["repeat"]) for r in results} != expected:
        return None
    if any(r["status"] != "complete" for r in results): return None
    if len({r.get("input_sha256") for r in results}) != 1 or not results[0].get("input_sha256"):
        return None
    eligible = []
    for model in PROFILES:
        rows = [r for r in results if r["model"] == model]
        if all(r["score"]["passed"] for r in rows):
            eligible.append((statistics.median(r["execution"]["seconds"] for r in rows),
                             max(r["execution"].get("peak_rss_bytes") or float("inf") for r in rows), model))
    return min(eligible)[2] if eligible else None


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--wait-lock", action="store_true"); args = parser.parse_args()
    require_root(); config = read(args.config)
    with benchmark_lock(args.wait_lock):
        batch = ROOT / "benchmarks" / config["benchmark_id"]
        batch.mkdir(parents=True, exist_ok=True)
        previous_controller = read(batch / "controller.json")
        if previous_controller:
            atomic(batch / "controllers" / (str(previous_controller["pid"]) + ".json"), previous_controller)
        atomic(batch / "controller.json", {"pid": os.getpid(), "started_at": now(), "python": sys.executable,
               "script": str(Path(__file__).resolve()), "harness_sha256": {p.name: digest(p.read_bytes()) for p in Path(__file__).parent.glob("*.py")}})
        atomic(batch / "selection.json", {"selected": None, "status": "in_progress", "requires_codex_review": True})
        results = []
        attempts = read(batch / "attempts.json", {})
        for model in PROFILES:
            for repeat in (1, 2):
                key = f"{model}-{repeat}"
                job = batch / attempts.get(key, key)
                done = read(job / "manifest.json")
                if done and done["status"] == "complete":
                    results.append(done); continue
                if job.exists():
                    # Preserve partial work; a fresh attempt has a separate directory.
                    job = batch / f"{model}-{repeat}-retry-{len(list(batch.glob(model+'*')))}"
                job.mkdir()
                attempts[key] = job.name
                atomic(batch / "attempts.json", attempts)
                atomic(batch / "progress.json", {"model": model, "repeat": repeat, "job": str(job), "started_at": now()})
                print(f"START {model} repeat={repeat}", flush=True)
                try:
                    fingerprint = prepare(job, config)
                    deadline = time.monotonic() + 3600
                    with ExitStack() as resources:
                        while True:
                            try:
                                resources.enter_context(server(model, job / "server")); break
                            except RuntimeError as exc:
                                if not str(exc).startswith(("GPU_busy_or_process_check_failed", "QA_port_8081_busy")) or time.monotonic() >= deadline:
                                    raise
                                atomic(batch / "progress.json", {"status": "waiting_for_resources", "model": model, "repeat": repeat, "job": str(job), "checked_at": now()})
                                time.sleep(10)
                        atomic(batch / "progress.json", {"status": "running", "model": model, "repeat": repeat, "job": str(job), "started_at": now()})
                        execution = execute(job, config, "Execute the complete benchmark instructions in input.json and save the report.", timeout=max(1, int(deadline - time.monotonic())))
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
        selected = choose_model(results)
        atomic(batch / "selection.json", {"selected": selected, "selected_at": now(), "results": results,
                                          "requires_codex_review": True, "benchmark": "sale-monitor-v1"})
        atomic(batch / "progress.json", {"status": "completed", "finished_at": now(), "selected": selected})
        print("SELECTED " + str(selected), flush=True)


if __name__ == "__main__": main()

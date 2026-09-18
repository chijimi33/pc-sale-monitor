from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

QA = Path(__file__).resolve().parents[1] / "scripts/qa"
sys.path.insert(0, str(QA))
from common import atomic, capture_patch, digest, environment, finalize, git, lock, read
from broker import Broker, visible_lines
from net import validate
from worker import urls
import worker
import benchmark
from review import verify
from benchmark import choose_model, score
from agent import compression_problem, execute
from model import blocks_inference, context_window
from compact_hook import grounding


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "repo/sale_monitor").mkdir(parents=True)
        (self.root / "repo/tests").mkdir()
        atomic(self.root / "input.json", {"immutable": True})
        atomic(self.root / "job.json", {"python": sys.executable, "allowed_urls": []})
        self.broker = Broker(self.root)

    def tearDown(self): self.temp.cleanup()

    def test_path_escape_and_protected_files(self):
        for path in ("../input.json", str(self.root / "input.json"), ".git/config", "public/latest.json", "scripts/qa/worker.py"):
            with self.subTest(path=path), self.assertRaises(ValueError): self.broker.path(path, write=True)

    def test_exact_edit_refuses_ambiguous_match(self):
        path = self.root / "repo/sale_monitor/a.py"
        path.write_text("price = 1\nprice = 1\n")
        with self.assertRaises(ValueError): self.broker.invoke({"op": "replace", "path": "sale_monitor/a.py", "old": "price = 1", "new": "price = 2"})
        self.assertEqual(path.read_text(), "price = 1\nprice = 1\n")

    def test_source_read_defaults_to_small_page_with_continuation(self):
        (self.root / "repo/sale_monitor/a.py").write_text("\n".join(f"value_{i} = {i}" for i in range(120)))
        first = self.broker.invoke({"op": "read", "path": "sale_monitor/a.py"})
        self.assertEqual(len(first["text"].splitlines()), 48)
        second = self.broker.invoke({"op": "read", "path": "sale_monitor/a.py", "start": first["next_start"], "count": 80})
        self.assertTrue(second["text"].startswith("49: value_48"))
        self.assertIsNone(second["next_start"])
        self.assertEqual(len(self.broker.invoke({"op": "read", "path": "sale_monitor/a.py", "count": -1})["text"].splitlines()), 1)

    def test_explicit_input_reread_respects_paging_and_query(self):
        self.broker.input = {"records": [{"price": i} for i in range(150)], "final_pending_count": 17}
        self.assertEqual(self.broker.invoke({"op": "read", "path": "input.json"}), self.broker.input)
        page = self.broker.invoke({"op": "read", "path": "input.json", "start": 1, "count": 80})
        self.assertEqual(len(page["text"].splitlines()), 80)
        self.assertEqual(page["next_start"], 81)
        following = self.broker.invoke({"op": "read", "path": "input.json", "start": 81, "count": 3})
        self.assertTrue(following["text"].startswith("81: "))
        found = self.broker.invoke({"op": "read", "path": "input.json", "query": "final_pending_count"})
        self.assertIn('"final_pending_count": 17', found["text"])
        self.assertLess(len(found["text"].splitlines()), 8)

    def test_evidence_pages_share_one_saved_response_and_hide_scripts(self):
        url = "https://example.com/product"
        self.broker.config["allowed_urls"] = [url]
        body = ('<script>hidden-script</script><style>hidden-style</style>' + '<p>' + 'x'*9000 + '</p><p>送料未確認</p>').encode()
        with patch("net.fetch", return_value=(body, url)) as fetch:
            first = self.broker.invoke({"op": "evidence", "url": url})
            second = self.broker.invoke({"op": "evidence", "url": url, "start": first["next_start"]})
            fetch.assert_called_once()
        self.assertLessEqual(len(first["untrusted_page_text"]), 6000)
        self.assertNotIn("hidden-", first["untrusted_page_text"])
        self.assertIn("送料未確認", second["untrusted_page_text"])
        self.assertIsNone(second["next_start"])
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(first["retrieved_at"], second["retrieved_at"])

    def test_evidence_honors_japanese_page_charset(self):
        page = '<meta http-equiv="Content-Type" content="text/html; charset=Shift_JIS"><p>税込49,800円・送料未確認</p>'
        self.assertEqual(visible_lines(page.encode("cp932")), ["税込49,800円・送料未確認"])
        self.assertEqual(visible_lines('<meta charset="unknown-charset"><p>在庫未確認</p>'.encode()), ["在庫未確認"])

    def test_evidence_search_keeps_nearby_conditions_and_original_lines(self):
        url = "https://example.com/product"
        self.broker.config["allowed_urls"] = [url]
        body = ("<p>navigation</p>"*100 + "<p>送料</p><p>0円</p><p>一部地域を除く</p>" + "<p>footer</p>"*100).encode()
        with patch("net.fetch", return_value=(body, url)):
            found = self.broker.invoke({"op": "evidence", "url": url, "query": "送料"})
            absent = self.broker.invoke({"op": "evidence", "url": url, "query": "在庫"})
        self.assertEqual(found["matching_lines"], 1)
        self.assertIn("101: 送料", found["untrusted_page_text"])
        self.assertIn("一部地域を除く", found["untrusted_page_text"])
        self.assertLess(len(found["untrusted_page_text"]), 200)
        self.assertEqual(absent["untrusted_page_text"], "")
        self.assertEqual(absent["matching_lines"], 0)

    def test_source_search_preserves_locations_without_sequential_scan(self):
        p = self.root / "repo/sale_monitor/a.py"
        p.write_text("# unrelated\n" * 200 + "if store == 'sofmap':\n    shipping = None\n")
        found = self.broker.invoke({"op": "read", "path": "sale_monitor/a.py", "query": "sofmap"})
        self.assertIn("201: if store", found["text"])
        self.assertIn("202:     shipping = None", found["text"])
        self.assertLess(len(found["text"].splitlines()), 10)

    def test_current_data_view_does_not_repeat_historical_event_or_page_noise(self):
        self.broker.config["data_base_url"] = "https://example.com/snapshot/"
        url = "https://www.ark-pc.co.jp/i/15602001/"
        original = {"events": [{"event_id": "event", "offer": {"price_yen": 999},
                                "decision": {"reference_yen": 888}, "previous": {"price_yen": 777},
                                "current_evidence": {"offer": {"price_yen": 100, "shipping_yen": None,
                                    "evidence": [{"url": url, "content_hash": "raw", "fields": {
                                        "specifications": {"unrelated campaign": "wrong product date"}}}]}}}]}
        atomic(self.root / "snapshot/notifications.json", original)
        compact = self.broker.invoke({"op": "data", "path": "notifications.json", "offer_key": "event"})[0]
        self.assertNotIn("decision", compact)
        self.assertNotIn("offer", compact)
        offer = compact["current_evidence"]["offer"]
        self.assertEqual(offer["price_yen"], 100)
        self.assertIsNone(offer["shipping_yen"])
        self.assertEqual(offer["evidence"], [{"url": url, "content_hash": "raw"}])
        full = self.broker.invoke({"op": "data", "path": "notifications.json", "offer_key": "event", "detail": True})
        self.assertEqual(full, original["events"])
        self.assertEqual(read(self.root / "snapshot/notifications.json"), original)
        self.assertIn(url, self.broker.config["allowed_urls"])

    def test_compaction_ledger_does_not_turn_selected_urls_into_fetched_sources(self):
        atomic(self.root / "input.json", {"previous": None, "candidate": [{"url": "https://example.com/unread"}],
            "current": {"stores": {"bic": {"pending_count": 893, "pending_over_24h": 863},
                                   "joshin": {"pending_count": 893, "pending_over_24h": 863}},
                        "validation": {"cutover_ready": False}}})
        body = b"actual evidence"; identity = digest(body)
        atomic(self.root / f"evidence/{identity}.json", {"url": "https://example.com/read", "sha256": identity})
        (self.root / f"evidence/{identity}.bin").write_bytes(body)
        atomic(self.root / "evidence/fake.json", {"url": "https://example.com/unread", "sha256": "fake"})
        ledger = grounding(self.root)
        self.assertFalse(ledger["previous_run_available"])
        self.assertFalse(ledger["cutover_ready"])
        self.assertEqual(ledger["per_store_pending_and_over24h"], {"bic": [893, 863], "joshin": [893, 863]})
        self.assertEqual([s["url"] for s in ledger["qa_retrieved_sources_only"]], ["https://example.com/read"])
        (self.root / f"evidence/{identity}.bin").write_bytes(b"changed")
        self.assertEqual(grounding(self.root)["qa_retrieved_sources_only"], [])

    def test_new_feedback_reaches_already_queued_work_without_mutating_input(self):
        queued = [{"id": "old", "note": "earlier"}]
        current = [{"id": "old", "note": "corrected"}, {"id": "new", "note": "source attribution"}]
        self.assertEqual(worker.merge_feedback(queued, current), current)
        self.assertEqual(queued, [{"id": "old", "note": "earlier"}])

    def test_live_context_override_does_not_change_benchmark_config(self):
        config = {"selected_model": "Q4_K_M", "live_context_window_size": 32768}
        self.assertEqual(context_window(config), 16384)
        live = worker.live_execution_config(config)
        self.assertEqual(context_window(live), 32768)
        self.assertNotIn("context_window_size", config)
        self.assertEqual(context_window(worker.live_execution_config({})), 16384)
        for invalid in (True, "32768", 0, 65536):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                worker.live_execution_config({"live_context_window_size": invalid})

    def test_live_stream_budget_reaches_sdk_without_changing_benchmark(self):
        config = {"python": sys.executable, "node": "unused-node", "live_context_window_size": 32768}
        for name, cfg, threshold, lifetime, timeout in (
            ("benchmark", config, 0.6, 900000, 1200000),
            ("live", worker.live_execution_config(config), 0.75, 1500000, 1500000),
        ):
            with self.subTest(name=name), patch("agent.subprocess.Popen") as popen, patch("agent.attach"):
                popen.return_value.poll.return_value = 0
                popen.return_value.returncode = 0
                popen.return_value.pid = 123
                folder = self.root / name
                execute(folder, cfg, "test", timeout=2940)
                env = popen.call_args.kwargs["env"]
                self.assertEqual(env.get("QWEN_STREAM_MAX_LIFETIME_MS"), str(lifetime))
                settings = read(folder / "home/settings.json")
                self.assertEqual(settings["context"]["autoCompactThreshold"], threshold)
                generation = settings["modelProviders"]["openai"][0]["generationConfig"]
                self.assertEqual(generation["timeout"], timeout)
                self.assertEqual(generation["samplingParams"]["max_tokens"], 4096)
                self.assertIn("2940s", popen.call_args.args[0])
        self.assertNotIn("live_validation", config)

    def test_accepted_truncated_compaction_is_not_silently_reused(self):
        import json
        p = self.root / "runtime/projects/p/chats/session.jsonl"
        p.parent.mkdir(parents=True)
        def save(text):
            p.write_text(json.dumps({"subtype": "chat_compression", "systemPayload": {
                "compressedHistory": [{"role": "user", "parts": [{"text": text}]}]}}) + "\n" + '{"streaming":')
        save("<state_snapshot>facts cut off by the server")
        self.assertEqual(compression_problem(self.root), "incomplete_compaction_snapshot")
        save("<state_snapshot>facts with regional exceptions</state_snapshot>")
        self.assertIsNone(compression_problem(self.root))
        with p.open("ab") as stream: stream.write(b'"unfinished Japanese: \xe3\x81')
        self.assertIsNone(compression_problem(self.root))

    def test_compaction_hook_protocol_and_receipt_stay_in_job(self):
        import json
        env = environment(self.root); env["PC_QA_ROOT"] = str(self.root)
        result = subprocess.run([sys.executable, "-B", str(QA / "compact_hook.py")], env=env,
                                cwd=self.root / "repo", capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreCompact")
        self.assertIn("Escape angle brackets", output["additionalContext"])
        record = json.loads((self.root / "compaction-hooks.jsonl").read_text())
        self.assertEqual(record["instructions_sha256"], digest(output["additionalContext"].encode()))
        self.assertIn("compaction-hooks.jsonl", finalize(self.root, "test")["files"])

    def test_new_file_and_report_remain_proposals(self):
        self.broker.invoke({"op": "replace", "path": "tests/test_new.py", "old": "", "new": "# test\n"})
        self.broker.invoke({"op": "report", "report": {"summary": "確認", "findings": [], "unresolved": [], "audit_status": "reviewed"}})
        self.assertEqual(read(self.root / "report.json")["audit_status"], "proposal_only")

    def test_no_evidence_no_finding(self):
        with self.assertRaises(ValueError): self.broker.invoke({"op": "report", "report": {"summary": "x", "findings": [{"claim": "sale"}], "unresolved": []}})

    def test_unselected_url_not_fetched(self):
        with self.assertRaises(ValueError): self.broker.invoke({"op": "evidence", "url": "https://example.com/"})

    def test_private_addresses_and_rakuten_blocked(self):
        for url, hosts in (("https://www.rakuten.co.jp/a", ["www.rakuten.co.jp"]), ("https://r10.to/a", ["r10.to"]), ("https://127.0.0.1/a", ["127.0.0.1"]), ("http://example.com/", ["example.com"]), ("https://user:pass@example.com/", ["example.com"])):
            with self.subTest(url=url), self.assertRaises(ValueError): validate(url, hosts)

    def test_finalize_detects_later_report_change(self):
        atomic(self.root / "report.json", {"summary": "a"})
        manifest = finalize(self.root, "awaiting_codex_review")
        atomic(self.root / "report.json", {"summary": "b"})
        self.assertNotEqual(manifest["files"]["report.json"]["sha256"], digest((self.root / "report.json").read_bytes()))

    def test_atomic_state_replaces_valid_json(self):
        path = self.root / "state.json"
        atomic(path, {"queue": [1]}); atomic(path, {"queue": [2]})
        self.assertEqual(read(path), {"queue": [2]})
        self.assertFalse(path.with_name("state.json.tmp").exists())

    def test_exclusive_worker_lock(self):
        with lock(self.root / "worker.lock"):
            with self.assertRaises(OSError):
                with lock(self.root / "worker.lock"): pass
        with lock(self.root / "worker.lock"): pass

    def test_source_url_discovery_excludes_rakuten(self):
        self.assertEqual(urls({"a": ["https://www.ark-pc.co.jp/i/1/", "https://item.rakuten.co.jp/x", "https://raw.githubusercontent.com/a"]}), {"https://www.ark-pc.co.jp/i/1/"})

    def test_patch_excludes_runtime_files_and_is_applicable(self):
        repo = self.root / "repo"
        file = repo / "sale_monitor/a.py"; file.write_text("price = 1\n")
        (repo / "tests/a.py").write_text("# baseline\n")
        git(repo, "init", "-q"); git(repo, "add", ".")
        git(repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "baseline")
        file.write_text("price = 2\n")
        (repo / "runtime-cache").write_text("do not publish")
        patch_file = self.root / "patch.diff"
        patch_file.write_text(capture_patch(repo), encoding="utf-8")
        self.assertNotIn("runtime-cache", patch_file.read_text())
        git(repo, "apply", "--reverse", "--check", str(patch_file))

    def test_review_detects_changed_artifact(self):
        atomic(self.root / "report.json", {"summary": "original"})
        finalize(self.root, "awaiting_codex_review")
        verify(self.root)
        atomic(self.root / "report.json", {"summary": "changed"})
        with self.assertRaisesRegex(ValueError, "artifact_changed"): verify(self.root)

    def test_manifest_covers_selected_source_evidence(self):
        atomic(self.root / "snapshot/latest.json", {"run_id": "r"})
        atomic(self.root / "evidence/page.json", {"source": "verified"})
        manifest = finalize(self.root, "awaiting_codex_review")
        self.assertIn("snapshot/latest.json", manifest["files"])
        self.assertIn("evidence/page.json", manifest["files"])
        atomic(self.root / "evidence/page.json", {"source": "changed"})
        with self.assertRaisesRegex(ValueError, "artifact_changed:evidence"): verify(self.root)

    def test_disabled_worker_never_calls_network_or_model(self):
        with patch.object(worker, "ROOT", self.root), patch.object(worker, "snapshot") as snapshot:
            worker.poll({"enabled": False})
            snapshot.assert_not_called()
            self.assertEqual(read(self.root / "status.json")["status"], "disabled_pending_model_review")

    def test_model_requires_review_before_network(self):
        with patch.object(worker, "ROOT", self.root), patch.object(worker, "snapshot") as snapshot:
            with self.assertRaisesRegex(RuntimeError, "requires_Codex_review"):
                worker.poll({"enabled": True, "selected_model": "Q3_K_XL", "benchmark_id": "test"})
            snapshot.assert_not_called()

    def test_review_selection_prioritizes_changed_and_rotates(self):
        candidates = [{"offer_key": "a", "change": "unchanged", "store": "ark"},
                      {"offer_key": "b", "change": "new", "store": "dospara"},
                      {"offer_key": "c", "change": "restocked", "store": "tsukumo"},
                      {"offer_key": "excluded", "change": "new", "store": "rakuten"}]
        self.assertEqual(worker.select_review(candidates, 0)[0]["offer_key"], "b")
        self.assertEqual(worker.select_review(candidates, 1)[0]["offer_key"], "c")
        self.assertEqual(worker.select_review(candidates, 2)[0]["offer_key"], "b")
        self.assertEqual(worker.select_review([], 0), [])

    def test_selection_requires_all_six_measurements(self):
        rows = [{"model": m, "repeat": r, "status": "complete", "input_sha256": "same", "score": {"passed": True},
                 "execution": {"seconds": t, "peak_rss_bytes": 100}} for m, t in (("Q3_K_XL", 10), ("Q4_K_S", 20), ("Q4_K_M", 30)) for r in (1, 2)]
        self.assertEqual(choose_model(rows), "Q3_K_XL")
        self.assertIsNone(choose_model(rows[1:]))
        rows[0]["status"] = "failed"
        self.assertIsNone(choose_model(rows))
        rows[0]["status"] = "complete"; rows[0]["score"]["passed"] = False
        self.assertEqual(choose_model(rows), "Q4_K_S")
        rows[0]["input_sha256"] = "different"
        self.assertIsNone(choose_model(rows))

    def test_queued_input_survives_checkout_failure(self):
        atomic(self.root / "benchmarks/test/selection.json", {"selected": "Q3_K_XL"})
        atomic(self.root / "model-review.json", {"approved_model": "Q3_K_XL"})
        latest = {"run_id": "r", "generated_at": "2026-09-18T00:00:00+00:00"}
        with patch.object(worker, "ROOT", self.root), patch.object(worker, "ensure_available"), patch.object(worker, "snapshot", return_value=("data", "base/", latest, {}, 0)), patch.object(worker, "get_json", return_value={"sha": "base"}), patch.object(worker, "checkout", side_effect=RuntimeError("network unavailable")), patch.object(worker, "execute") as execute, patch.object(worker.shutil, "disk_usage", return_value=type("Disk", (), {"free": 100*1024**3})()):
            for _ in range(4): worker.poll({"enabled": True, "selected_model": "Q3_K_XL", "benchmark_id": "test"})
            execute.assert_not_called()
            state = read(self.root / "state.json")
            self.assertEqual(len(state["held"]), 1)
            self.assertEqual(state["held"][0]["attempts"], 3)
            self.assertEqual(state["queue"], [])
            self.assertEqual(len(list((self.root / "jobs").glob("*/manifest.json"))), 3)

    def test_busy_gpu_preserves_queue_without_consuming_attempts(self):
        atomic(self.root / "benchmarks/test/selection.json", {"selected": "Q3_K_XL"})
        atomic(self.root / "model-review.json", {"approved_model": "Q3_K_XL"})
        latest = {"run_id": "r", "generated_at": "2026-09-18T00:00:00+00:00"}
        with patch.object(worker, "ROOT", self.root), patch.object(worker, "snapshot", return_value=("data", "base/", latest, {}, 0)), patch.object(worker, "ensure_available", side_effect=RuntimeError("GPU_busy_or_process_check_failed")), patch.object(worker, "checkout") as checkout, patch.object(worker, "execute") as execute, patch.object(worker.shutil, "disk_usage", return_value=type("Disk", (), {"free": 100*1024**3})()):
            for _ in range(4): worker.poll({"enabled": True, "selected_model": "Q3_K_XL", "benchmark_id": "test"})
            checkout.assert_not_called(); execute.assert_not_called()
            state = read(self.root / "state.json")
            self.assertEqual(len(state["queue"]), 1)
            self.assertEqual(state["queue"][0]["attempts"], 0)
            self.assertEqual(state["completed"], [])
            self.assertEqual(read(self.root / "status.json")["status"], "waiting_for_resources")

    def test_lm_studio_names_and_daemon_block_even_with_small_working_set(self):
        for name in ("LM Studio", "lm-studio", "lmstudio", "llmster", "llama-server"):
            with self.subTest(name=name):
                self.assertTrue(blocks_inference({"ProcessName": name, "WorkingSet64": 1000}))
        self.assertFalse(blocks_inference({"ProcessName": "notepad", "WorkingSet64": 1000}))
        self.assertTrue(blocks_inference({"ProcessName": "Overwatch", "WorkingSet64": 3 * 1024**3}))

    def test_benchmark_counts_rejected_controller_write(self):
        self.broker.call({"op": "replace", "path": "scripts/qa/worker.py", "old": "", "new": "# disallowed"})
        self.assertFalse(score(self.root)["no_prohibited_write_attempt"])

    def test_resumed_comparison_publishes_all_cached_trials(self):
        config = self.root / "config.json"
        atomic(config, {"benchmark_id": "cached"})
        batch = self.root / "benchmarks/cached"
        for model, seconds in (("Q3_K_XL", 10), ("Q4_K_S", 20), ("Q4_K_M", 30)):
            for repeat in (1, 2):
                atomic(batch / f"{model}-{repeat}/manifest.json", {
                    "model": model, "repeat": repeat, "status": "complete", "input_sha256": "same",
                    "execution": {"exit_code": 0, "seconds": seconds}, "score": {"passed": True}})
        # Simulate a stale checkpoint containing only the last newly run trial.
        atomic(batch / "results.json", [{"model": "stale"}])
        with patch.object(benchmark, "ROOT", self.root), patch.object(benchmark, "require_root"), patch.object(benchmark, "score", return_value={"passed": True}), patch.object(benchmark, "server") as server, patch.object(sys, "argv", ["benchmark", "--config", str(config)]):
            benchmark.main()
            server.assert_not_called()
        results = read(batch / "results.json")
        selection = read(batch / "selection.json")
        self.assertEqual(len(results), 6)
        self.assertEqual(results, selection["results"])
        self.assertEqual(selection["selected"], "Q3_K_XL")
        self.assertEqual(selection["benchmark"], "cached")

    def test_repair_review_detects_self_comparison_and_stale_reuse(self):
        source = self.root / "repo/sale_monitor/check.py"
        source.write_text('def qualifies(offer, comparisons, current_run="r"):\n    return True\n')
        result = score(self.root)
        self.assertFalse(result["review_cases"][0]["passed"])
        source.write_text('def qualifies(offer, comparisons, current_run="r"):\n    return False\n')
        result = score(self.root)
        self.assertFalse(result["review_cases"][1]["passed"])
        self.assertFalse(result["review_cases"][2]["passed"])

    @unittest.skipUnless(os.name == "nt", "Windows process ownership")
    def test_child_ends_when_controller_exits(self):
        import ctypes
        from ctypes import wintypes
        child_pid = self.root / "child-pid.txt"
        program = "import sys,subprocess,os;sys.path.insert(0,sys.argv[1]);from process_guard import attach;p=subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(60)']);attach(p);open(sys.argv[2],'w').write(str(p.pid));os._exit(0)"
        result = subprocess.run([sys.executable, "-B", "-c", program, str(QA), str(child_pid)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, int(child_pid.read_text()))
        if handle:
            try: self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
            finally: kernel.CloseHandle(handle)


if __name__ == "__main__": unittest.main()

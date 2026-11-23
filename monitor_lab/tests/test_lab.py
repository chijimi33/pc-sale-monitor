from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from email.utils import format_datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
import uuid

from sale_monitor.engine import evaluate, update_events
from sale_monitor.http import Page
from sale_monitor.models import Offer
from monitor_lab.acquire import Coordinator, challenge, retry_delay
from monitor_lab.evidence import decide, normalize
from monitor_lab.operations import publish, schedule_report
from monitor_lab.migration import repair_torn_journal, restore_export
from monitor_lab.pipeline import dependencies, make_task, existing_task_id
from monitor_lab.queueing import select_resource, expand_unknown_search, expand_shared_list
from monitor_lab.safety import allowed_root, digest, guard, read, write
from monitor_lab.stores import BACKENDS, Journal, SQLite


class LabTest(unittest.TestCase):
    def setUp(self):
        self.root = allowed_root() / "tests" / (self.id().split(".")[-1] + "-" + uuid.uuid4().hex)

    def test_production_output_and_symlink_are_refused(self):
        with self.assertRaises(ValueError):
            guard(Path(__file__).resolve().parents[2] / "public/latest.json")
        with self.assertRaises(ValueError):
            guard(allowed_root() / ".." / "production")
        with self.assertRaises(ValueError):
            guard(self.root / "state-repo" / "state")

    def test_all_backends_atomic_idempotent_transaction(self):
        changes = [("tasks", "t", {"created_at": "2001-01-01", "attempts": ["disconnect"], "status": "complete"}),
                   ("observations", "o", {"price": 10980}), ("events", "e", {"delivery_status": "unconfirmed"})]
        hashes = []
        for backend, cls in BACKENDS.items():
            with cls(self.root / backend) as store:
                self.assertTrue(store.commit("tx", changes))
                self.assertFalse(store.commit("tx", changes))
                with self.assertRaises(ValueError):
                    store.commit("tx", changes + [("events", "other", {})])
                hashes.append(digest(store.snapshot()))
            with cls(self.root / backend) as store:
                self.assertEqual(hashes[-1], digest(store.snapshot()))
                self.assertEqual(1, len(store.snapshot()["records"]["events"]))
        self.assertEqual(1, len(set(hashes)))

    def test_process_death_at_both_transaction_boundaries(self):
        code = """
import os,sys
from monitor_lab.stores import BACKENDS
s=BACKENDS[sys.argv[1]](sys.argv[2])
s.commit('tx',[('tasks','t',{'status':'complete'}),('observations','o',{'price':10980}),('events','e',{'delivery':'unconfirmed'})],
         lambda stage: os._exit(73) if stage==sys.argv[3] else None)
"""
        for backend, cls in BACKENDS.items():
            for boundary in ("before_commit", "after_commit"):
                root = self.root / backend / boundary
                p = subprocess.run([sys.executable, "-c", code, backend, str(root), boundary], capture_output=True,
                                   env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                self.assertEqual(73, p.returncode, p.stderr)
                with cls(root) as store:
                    records = store.snapshot()["records"]
                    self.assertEqual(boundary == "after_commit", bool(records))
                    if records:
                        self.assertEqual({"tasks", "observations", "events"}, set(records))

    def test_journal_checkpoint_interruption_does_not_duplicate(self):
        with Journal(self.root / "journal", checkpoint_every=0) as store:
            store.commit("x", [("events", "e", {"x": 1})])
            with self.assertRaises(RuntimeError):
                store.checkpoint(lambda stage: (_ for _ in ()).throw(RuntimeError("crash")))
        with Journal(self.root / "journal") as store:
            self.assertEqual({"e": {"x": 1}}, store.snapshot()["records"]["events"])
            self.assertFalse(store.commit("x", [("events", "e", {"x": 1})]))

    def test_corrupt_state_never_becomes_empty(self):
        for backend, cls in BACKENDS.items():
            root = self.root / backend
            with cls(root) as store:
                store.commit("x", [("events", "e", {})])
            file = root / {"json": "state.json", "journal": "journal.jsonl", "sqlite": "state.sqlite3"}[backend]
            file.write_bytes(b"{bad")
            with self.assertRaises((ValueError, sqlite3.DatabaseError)):
                cls(root)
            self.assertEqual(b"{bad", file.read_bytes())

    def test_torn_journal_refuses_even_valid_json_without_newline(self):
        with Journal(self.root / "j") as store:
            store.commit("x", [("events", "e", {})])
        p = self.root / "j/journal.jsonl"
        p.write_bytes(p.read_bytes().rstrip(b"\n"))
        with self.assertRaisesRegex(ValueError, "Torn journal"):
            Journal(self.root / "j")

    def test_single_writer_lock(self):
        for name, cls in BACKENDS.items():
            with cls(self.root / name):
                with self.assertRaises(RuntimeError):
                    cls(self.root / name)

    def test_sqlite_backup_preserves_committed_wal(self):
        with SQLite(self.root / "db") as store:
            store.commit("one", [("tasks", "old", {"created_at": "2020-01-01", "attempts": 7})])
            expected = store.snapshot()
            store.backup(self.root / "copy/state.sqlite3")
            store.commit("two", [("tasks", "new", {})])
        with SQLite(self.root / "copy") as copy:
            self.assertEqual(expected, copy.snapshot())

    def test_export_restore_preserves_receipts_across_all_backends(self):
        changes = [("events", "e", {"delivery_status": "delivered"}),
                   ("tasks", "t", {"created_at": "2000-01-01", "attempts": [1, 2]})]
        with SQLite(self.root / "origin") as source:
            source.commit("original-id", changes)
            source.export(self.root / "export.json")
            expected = source.snapshot()
        for backend, cls in BACKENDS.items():
            restore_export(self.root / "export.json", backend, self.root / backend)
            with cls(self.root / backend) as target:
                self.assertEqual(expected, target.snapshot())
                self.assertFalse(target.commit("original-id", changes))
            with self.assertRaises(ValueError):
                restore_export(self.root / "export.json", backend, self.root / backend)

    def test_explicit_torn_write_repair_keeps_original_and_retry_once(self):
        source = self.root / "source"
        with Journal(source) as store:
            store.commit("committed", [("events", "one", {"x": 1})])
        with (source / "journal.jsonl").open("ab") as f:
            f.write(b'{"txid":"uncommitted",')
        before = (source / "journal.jsonl").read_bytes()
        report = repair_torn_journal(source, self.root / "repaired")
        self.assertEqual(1, report["recovered_transactions"])
        self.assertEqual(before, (source / "journal.jsonl").read_bytes())
        with Journal(self.root / "repaired") as store:
            self.assertTrue(store.commit("uncommitted", [("events", "two", {"x": 2})]))
            self.assertFalse(store.commit("uncommitted", [("events", "two", {"x": 2})]))
            self.assertEqual(2, len(store.snapshot()["records"]["events"]))

    def test_complete_corrupt_journal_is_not_truncated_by_repair(self):
        source = self.root / "source"
        source.mkdir(parents=True)
        raw = b'{"txid":"x","changes":[],"checksum":"invalid"}\n'
        (source / "journal.jsonl").write_bytes(raw)
        with self.assertRaises(ValueError):
            repair_torn_journal(source, self.root / "repaired")
        self.assertEqual(raw, (source / "journal.jsonl").read_bytes())

    def test_publication_failure_and_retry_keep_outbox(self):
        payload = {"events": [{"event_id": "same", "delivery_status": "unconfirmed"}]}
        with SQLite(self.root / "db") as store:
            store.commit("seed", [("events", "same", payload["events"][0])])
            publish(store, self.root / "public", "lab-old", {"old": True})
            for failure in ("before_pointer", "after_pointer"):
                with self.assertRaises(RuntimeError):
                    publish(store, self.root / "public", "lab-new", payload,
                            lambda stage: (_ for _ in ()).throw(RuntimeError("publish failed")) if stage == failure else None)
                expected = "lab-old" if failure == "before_pointer" else "lab-new"
                self.assertEqual(expected, read(self.root / "public/current.json")["generation"])
                self.assertEqual("unconfirmed", store.snapshot()["records"]["events"]["same"]["delivery_status"])
            publish(store, self.root / "public", "lab-new", payload)
            publish(store, self.root / "public", "lab-new", payload)
            self.assertEqual(1, len(store.snapshot()["records"]["events"]))
            with self.assertRaises(ValueError):
                publish(store, self.root / "public", "lab-new", {"changed": True})

    def test_dependency_shared_task_preserves_age_attempts(self):
        page = {"group": "x", "store": "ark", "url": "https://www.ark-pc.co.jp/i/1/"}
        candidate = make_task("koubou", "https://www.pc-koubou.jp/products/detail.php?product_id=1", "2026-10-01", group="x")
        first, keys = dependencies(candidate, [page], {}, "2026-10-01")
        tasks = {k: t for _, k, t in first}
        shared = keys[0]
        tasks[shared]["created_at"] = "2000-01-01"
        tasks[shared]["lab_attempts"] = [{"error": "disconnect"}]
        candidate["url"] += "2"
        second, _ = dependencies(candidate, [page], tasks, "2026-10-02")
        final = {k: t for _, k, t in second}
        self.assertEqual("2000-01-01", final[shared]["created_at"])
        self.assertEqual([{"error": "disconnect"}], final[shared]["lab_attempts"])
        self.assertEqual(2, len(final[shared]["lab_dependencies"]))
        self.assertEqual(9, len(final))  # one known URL and eight genuinely unknown stores

    def test_existing_original_task_is_reused_without_resetting_age(self):
        url = "https://www.ark-pc.co.jp/i/1/"
        old = {"type": "product", "url": url, "lab_store": "ark", "created_at": "2000-01-01",
               "attempts": 17, "last_error": "disconnect", "lab_status": "pending", "lab_key": "original"}
        tasks = {"ark:original": old}
        self.assertEqual("ark:original", existing_task_id(tasks, "ark", url))
        candidate = make_task("koubou", "https://www.pc-koubou.jp/products/detail.php?product_id=1", "2026-10-01", group="x")
        changes, _ = dependencies(candidate, [{"group": "x", "store": "ark", "url": url}], tasks, "2026-10-02")
        task = next(t for _, k, t in changes if k == "ark:original")
        for field in ("attempts", "created_at", "last_error", "lab_key"):
            self.assertEqual(old[field], task[field])

    def test_known_historical_url_suppresses_broad_search_without_reusing_price(self):
        candidate = make_task("koubou", "https://www.pc-koubou.jp/products/detail.php?product_id=1", "2026-10-01", group="x")
        candidate.update(lab_identity="jan:0195553309745", lab_query="0195553309745")
        known = {"old": {"identity": candidate["lab_identity"], "store": "ark", "url": "https://www.ark-pc.co.jp/i/1/", "price_yen": 99999}}
        changes, keys = dependencies(candidate, [], {}, "2026-10-01", known)
        rows = [v for _, _, v in changes]
        ark = [r for r in rows if r["lab_store"] == "ark"]
        self.assertEqual(1, len(ark))
        self.assertEqual("product", ark[0]["lab_kind"])
        self.assertNotIn("price_yen", ark[0])
        self.assertFalse(ark[0]["lab_selected"])
        self.assertEqual("0195553309745", next(r for r in rows if r["lab_kind"] == "search")["query"])


class FakeClock:
    def __init__(self):
        self.now = 1000.0
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class FakeTransport:
    method = "fake"
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
    def get(self, url, timeout):
        self.calls.append(url)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class TransportTest(unittest.TestCase):
    def coordinator(self, responses, **kwargs):
        self.clock = FakeClock()
        self.transport = FakeTransport(responses)
        return Coordinator(self.transport, clock=self.clock, monotonic=self.clock, sleep=self.clock.sleep, **kwargs)

    def test_retry_after_shared_across_tasks_methods_and_restart(self):
        host = {}
        client = self.coordinator([(429, {"Retry-After": "120"}, b"slow")], hosts=host)
        page, first = client.fetch("https://shop.tsukumo.co.jp/a")
        page, second = client.fetch("https://shop.tsukumo.co.jp/b")
        self.assertEqual("retry_after", first.error)
        self.assertEqual("shared_host_wait", second.error)
        self.assertEqual(1, len(self.transport.calls))
        next_client = self.coordinator([], hosts=host)
        self.assertEqual("shared_host_wait", next_client.fetch("https://shop.tsukumo.co.jp/c")[1].error)

    def test_disconnect_retains_failure_and_shared_wait(self):
        client = self.coordinator([ConnectionResetError("disconnected")])
        self.assertEqual("transport:ConnectionResetError", client.fetch("https://shop.tsukumo.co.jp/a")[1].error)
        self.assertEqual("shared_host_wait", client.fetch("https://shop.tsukumo.co.jp/b")[1].error)
        self.assertEqual(1, client.requests)

    def test_403_never_automatically_switches_method(self):
        client = self.coordinator([(403, {}, b"denied")])
        self.assertEqual("authentication_or_challenge", client.fetch("https://www.ark-pc.co.jp/a")[1].error)
        self.clock.now += 100000
        self.assertEqual("shared_host_wait", client.fetch("https://www.ark-pc.co.jp/b")[1].error)
        self.assertEqual(1, client.requests)

    def test_whole_stage_budget_and_previous_sequence(self):
        client = self.coordinator([(200, {}, b"ok"), (200, {}, b"ok")], budget=1, delay=2)
        self.assertIsNone(client.fetch("https://www.ark-pc.co.jp/a")[1].error)
        self.assertEqual("shared_budget_exhausted", client.fetch("https://www.ark-pc.co.jp/b")[1].error)
        self.assertEqual(1, client.requests)

    def test_retry_after_http_date_and_invalid_values(self):
        value = format_datetime(datetime.fromtimestamp(1120, timezone.utc), usegmt=True)
        self.assertEqual(120, retry_delay(value, 1000))
        self.assertEqual(60, retry_delay("nan", 1000))
        self.assertEqual(60, retry_delay("invalid", 1000))

    def test_challenge_telemetry_is_not_a_challenge(self):
        self.assertFalse(challenge(b'<script>var captcha="verify you are human";</script><h1>Product</h1>'))
        self.assertTrue(challenge(b"<h1>Verify you are human</h1>"))

    def test_rakuten_never_requested(self):
        client = self.coordinator([])
        self.assertEqual("excluded_source", client.fetch("https://item.rakuten.co.jp/a")[1].error)
        self.assertEqual(0, client.requests)


NOW = datetime(2026, 10, 1, 20, 5, tzinfo=timezone.utc)


def offer(store, price):
    return Offer(store=store, product_id=store, url="https://example.com/" + store, title="fixture", jan="0195553309745",
                 price_yen=price, shipping_yen=0, stock="in_stock", condition="new", seller_id=store,
                 observed_at=NOW.isoformat(), observed_run_id="lab-run", verified=True, evidence=[{"fixture": True}])


class DecisionTest(unittest.TestCase):
    def test_same_run_A_math_matches_existing_engine(self):
        a, b, c = offer("koubou", 10980), offer("tsukumo", 14099), offer("ark", 17980)
        self.assertEqual(evaluate(a, [b, c], [], NOW), decide(a, [b, c], [], NOW, "lab-run"))
        self.assertEqual("A", decide(a, [b, c], [], NOW, "lab-run")["rule"])

    def test_stale_comparison_cannot_make_an_offer_qualify(self):
        a, b, c = offer("koubou", 10980), offer("tsukumo", 14099), offer("ark", 17980)
        b.observed_run_id = "old"
        result = decide(a, [b, c], [], NOW, "lab-run")
        self.assertEqual("insufficient", result["status"])
        self.assertIn("known_comparator_not_verified_this_run", result["reasons"])

    def test_member_price_unknown_blocks_even_two_other_expensive_comparators(self):
        a, b, c, d = offer("tsukumo", 13937), offer("ark", 15950), offer("koubou", 19800), offer("dospara", 17980)
        c.issues.append("member_price_not_verified")
        result = decide(a, [b, c, d], [], NOW, "lab-run")
        self.assertEqual("insufficient", result["status"])
        self.assertIn("comparator_member_price_not_verified", result["reasons"])

    def test_bundle_condition_and_warranty_differences_not_erased_by_jan(self):
        for attribute, value in (("variant", "two modules"), ("condition", "used"), ("warranty", "90 days")):
            a, b, c = offer("koubou", 10980), offer("tsukumo", 14099), offer("ark", 17980)
            if attribute == "warranty":
                a.warranty = "one year"
            setattr(b, attribute, value)
            self.assertNotEqual("accepted", decide(a, [b, c], [], NOW, "lab-run")["status"])

    def test_B_history_rule_preserved(self):
        a, b = offer("koubou", 10980), offer("tsukumo", 11000)
        old = offer("ark", 13000)
        old.observed_at = "2026-09-01T20:05:00+00:00"
        history = [{"offer": old.to_dict()}]
        self.assertEqual(evaluate(a, [b], history, NOW), decide(a, [b], history, NOW, "lab-run"))
        self.assertEqual("B_observed_year_low", decide(a, [b], history, NOW, "lab-run")["rule"])

    def test_failed_comparison_does_not_reissue_event(self):
        a, b, c = offer("koubou", 10980), offer("tsukumo", 14099), offer("ark", 17980)
        decision = decide(a, [b, c], [], NOW, "lab-run")
        events, state = update_events(a, decision, None, NOW)
        self.assertEqual(1, len(events))
        b.observed_run_id = "old"
        no_events, held = update_events(a, decide(a, [b, c], [], NOW, "lab-run"), deepcopy(state), NOW)
        self.assertEqual([], no_events)
        self.assertEqual(state, held)
        b.observed_run_id = "lab-run"
        self.assertEqual([], update_events(a, decision, held, NOW)[0])


class ParserTest(unittest.TestCase):
    def html(self, schema_extra=None, inside="", footer="", shipping="無料"):
        schema = {"@context": "https://schema.org", "@type": "Product", "name": "fixture", "sku": "0195553309745",
                  "offers": {"@type": "Offer", "price": 10980, "priceCurrency": "JPY", "itemCondition": "https://schema.org/NewCondition",
                             "availability": "https://schema.org/InStock", **(schema_extra or {})}}
        return ('<script type="application/ld+json">' + json.dumps(schema) + '</script><input id="priceIncTax" value="10980">'
                '<div class="productDetail--main__right--price">' + inside + '</div><table><tr><th>送料</th><td>' + shipping + '</td></tr></table>' + footer).encode()

    def parse(self, body):
        page = Page("https://www.pc-koubou.jp/products/detail.php?product_id=1", body, NOW.isoformat())
        return normalize("koubou", page, {"default_condition": "new"}, "lab-run", {"body_sha256": "fixture"})

    def test_primary_hidden_member_price_but_not_footer(self):
        self.assertIn("member_price_not_verified", self.parse(self.html(inside="WEB会員限定価格 ログインして確認")).conflicts)
        self.assertNotIn("member_price_not_verified", self.parse(self.html(footer="<footer>WEB会員限定価格 ログイン</footer>")).conflicts)

    def test_shipping_conflict_keeps_both_sources(self):
        extra = {"shippingDetails": {"shippingRate": {"currency": "JPY", "value": 500}}}
        obs = self.parse(self.html(extra))
        self.assertIn("shipping_conflict_review_needed", obs.conflicts)
        self.assertIsNone(obs.offer.shipping_yen)
        self.assertEqual(500, obs.fields["shipping_yen"]["sources"][-1]["schema"])

    def test_regional_shipping_rule_not_guessed(self):
        extra = {"shippingDetails": {"shippingRate": {"currency": "JPY", "value": 0}, "shippingDestination": {"addressRegion": "Tokyo"}}}
        obs = self.parse(self.html(extra, shipping="不明"))
        self.assertIsNone(obs.offer.shipping_yen)
        self.assertIn("shipping_destination_or_threshold_unverified", obs.conflicts)

    def test_price_conflict_keeps_schema_and_primary(self):
        obs = self.parse(self.html({"price": 9800}))
        self.assertIn("price_conflict_review_needed", obs.conflicts)

    def test_stock_conflict_keeps_schema_and_primary(self):
        obs = self.parse(self.html(inside='<button disabled>在庫切れです</button>'))
        self.assertIn("stock_conflict_review_needed", obs.conflicts)
        self.assertEqual("conflict", obs.fields["stock"]["status"])

    def test_expiry_conflict_is_retained(self):
        schema = {"@context": "https://schema.org", "@type": "Product", "name": "fixture", "sku": "0195553309745",
                  "offers": {"price": 10000, "priceCurrency": "JPY", "priceValidUntil": "2026-09-18"}}
        body = ('<script type="application/ld+json">' + json.dumps(schema) + '</script><li class="itemprice"><span id="item-12201487"></span><div class="date-diff2">開催期間:10/01 23:59まで</div></li>').encode()
        page = Page("https://www.ark-pc.co.jp/i/12201487/", body, NOW.isoformat())
        obs = normalize("ark", page, {"default_condition": "new"}, "lab-run", {})
        self.assertIn("expiry_conflict_review_needed", obs.conflicts)
        self.assertIsNone(obs.offer.expires_at)


class ScheduleTest(unittest.TestCase):
    def test_backfill_push_and_retry_are_not_scheduled_samples(self):
        runs = [{"id": 1, "event": "schedule", "created_at": "2026-10-01T00:30:00Z", "verified": True},
                {"id": 1, "event": "schedule", "created_at": "2026-10-01T00:30:00Z", "verified": True, "attempt": 2},
                {"id": 2, "event": "workflow_dispatch", "created_at": "2026-10-01T04:20:00Z", "verified": True},
                {"id": 3, "event": "push", "created_at": "2026-10-01T08:20:00Z", "verified": True},
                {"id": 4, "event": "schedule", "created_at": "2026-10-01T08:20:00Z", "verified": False}]
        report = schedule_report(runs, "2026-10-01T00:00:00Z", "2026-10-01T10:00:00Z")
        self.assertEqual(1, report["genuine_scheduled_runs"])
        self.assertEqual(2, report["missing_slots"])
        self.assertIsNone(report["runs"][0]["scheduled_at"])
        self.assertFalse(report["scheduled_time_verified"])


class QueueComponentTest(unittest.TestCase):
    def task(self, url, age="2000-01-01", requested=False):
        return {"lab_store": "ark", "url": url, "created_at": age, "lab_status": "pending", "requested": requested}

    def test_shared_wait_reports_individual_task_count_without_mutation(self):
        tasks = {"one": self.task("https://www.ark-pc.co.jp/i/1/"), "two": self.task("https://www.ark-pc.co.jp/i/2/")}
        before = deepcopy(tasks)
        result = select_resource(tasks, {"www.ark-pc.co.jp": {"until": 120}}, 100)
        self.assertEqual(2, result["waiting_task_count"])
        self.assertEqual([], result["task_ids"])
        self.assertEqual(before, tasks)

    def test_same_page_one_resource_preserves_both_dependency_tasks(self):
        tasks = {"one": self.task("https://www.ark-pc.co.jp/i/1/"), "two": self.task("https://www.ark-pc.co.jp/i/1/", "2001-01-01")}
        result = select_resource(tasks, {}, 100)
        self.assertEqual({"one", "two"}, set(result["task_ids"]))
        self.assertEqual(2, result["remaining_task_count"])

    def test_old_work_gets_every_fourth_turn_and_cursor_survives_restart(self):
        tasks = {"old": self.task("https://www.ark-pc.co.jp/i/1/", "2000-01-01")}
        for i in range(6):
            tasks[str(i)] = self.task(f"https://www.ark-pc.co.jp/i/{i+10}/", "2026-10-01", True)
        cursor, selected = 0, []
        for _ in range(4):
            result = select_resource(tasks, {}, 100, cursor)
            selected += result["task_ids"]
            cursor = result["next_cursor"]
            for key in result["task_ids"]:
                tasks[key]["lab_status"] = "complete"
        self.assertEqual("old", selected[3])
        self.assertEqual(4, cursor)

    def test_shared_list_children_inherit_oldest_age_and_all_parents(self):
        tasks = [self.task("https://www.ark-pc.co.jp/search/", "2000-01-01"), self.task("https://www.ark-pc.co.jp/search/", "2026-10-01")]
        tasks[0]["lab_dependencies"], tasks[1]["lab_dependencies"] = ["candidate1"], ["candidate2"]
        page = Page(tasks[0]["url"], b'<a href="/i/12201487/">Motherboard</a>', NOW.isoformat())
        children, evidence = expand_shared_list(tasks, page, {"product_patterns": [r"/i/\d+/"]})
        self.assertEqual(1, len(children))
        self.assertEqual("2000-01-01", children[0]["created_at"])
        self.assertEqual(["candidate1", "candidate2"], children[0]["lab_dependencies"])
        self.assertEqual(2, evidence["shared_source_tasks"])

    def test_unrecognized_empty_page_remains_failure(self):
        task = self.task("https://www.ark-pc.co.jp/search/")
        with self.assertRaises(ValueError):
            expand_shared_list([task], Page(task["url"], b"<h1>error</h1>", NOW.isoformat()), {"product_patterns": [r"/i/\d+/"]})

    def test_unknown_search_uses_verified_form_and_preserves_age(self):
        task = {**self.task("https://www.ark-pc.co.jp/"), "query": "0195553309745"}
        page = Page(task["url"], b'<form action="/search/"><input name="keyword" type="search"></form>', NOW.isoformat())
        result = expand_unknown_search(task, page, {})
        self.assertIn("0195553309745", result["url"])
        self.assertEqual("2000-01-01", result["created_at"])


if __name__ == "__main__":
    unittest.main()

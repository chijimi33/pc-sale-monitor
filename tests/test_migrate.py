from copy import deepcopy
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

from sale_monitor.migrate import apply, fingerprint, revert
from sale_monitor.routes import RouteState, load_config
from sale_monitor.storage import Store


CONFIG = Path(__file__).resolve().parents[1] / "config/routes.json"


class MigrationTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.disk = Store(self.root)
        self.routes = RouteState(self.disk, load_config(CONFIG), now=1000)
        self.originals = {}
        for store in ("ark", "tsukumo", "joshin", "yahoo", "amazon"):
            queue = {kind: {"type": "product", "kind": kind, "created_at": "2026-01-01T00:00:00+00:00",
                            "attempts": 7, "last_error": "http_503", "extra": {"nested": [1, 2]}}
                     for kind in ("sale", "comparison")}
            state = {"store": store, "queue": queue, "pending_count": 2,
                     "waiting_dependencies": [{"host": "old"}], "offers": {"one": {"price_yen": 1}},
                     "journal": [{"observation_id": "fixed"}], "done": ["done"], "errors": [{"reason": "old"}],
                     "comparison_index": {"identity": "retained"}, "custom_history": [1, 2]}
            self.disk.save(f"stores/{store}.json", state)
            self.originals[store] = deepcopy(state)
        for name in ("events/registry.json", "events/delivery.json", "history/ark/old.json",
                     "flyers/asset.json", "requests/pending.json"):
            self.disk.save(name, {"unchanged": name})

    def test_archive_preserves_task_and_protected_data(self):
        before = fingerprint(self.root)
        untouched = {path: path.read_bytes() for name in ("events", "history", "flyers", "requests")
                     for path in (self.root / name).rglob("*") if path.is_file()}
        result = apply(self.root, self.routes, input_sha="fixed-sha", now=1000)
        self.assertTrue(result["fingerprint_match"])
        self.assertEqual(before, fingerprint(self.root))
        for path, content in untouched.items():
            self.assertEqual(path.read_bytes(), content)
        for store, original in self.originals.items():
            state = self.disk.load(f"stores/{store}.json", {})
            archive = self.disk.load(f"archive/{store}.json", {})
            expected = {"comparison"} if store == "ark" else {"sale", "comparison"}
            self.assertEqual(set(archive["entries"]), expected)
            for task_id, entry in archive["entries"].items():
                self.assertEqual(entry["task"], original["queue"][task_id])
                self.assertEqual(entry["task_id"], task_id)
                self.assertEqual(entry["migration_version"], 1)
                self.assertEqual(entry["input_sha"], "fixed-sha")
            for key in original.keys() - {"queue", "pending_count", "waiting_dependencies"}:
                self.assertEqual(state[key], original[key])
            self.assertEqual(state["waiting_dependencies"], [])
            self.assertEqual(state["pending_count"], 1 if store == "ark" else 0)
        self.assertEqual(result["archived"]["ark"], {"comparison_debt_abolished": 1})
        self.assertEqual(result["archived"]["tsukumo"]["route_paused:paused_maintenance"], 2)
        self.assertEqual(result["remaining"]["ark"], 1)

    def test_second_application_is_byte_identical(self):
        apply(self.root, self.routes, now=1000)
        before = {path: path.read_bytes() for path in self.root.rglob("*.json")}
        result = apply(self.root, self.routes, now=99999)
        self.assertEqual(sum(sum(counts.values()) for counts in result["archived"].values()), 0)
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*.json")})
        self.assertEqual(len(self.disk.load("schema.json", {})["migrations"]), 1)

    def test_existing_archive_entry_is_never_overwritten(self):
        record = {"task": {"created_at": "original"}, "archived_at": "original", "task_id": "comparison"}
        self.disk.save("archive/ark.json", {"schema_version": 1, "entries": {"comparison": record}})
        apply(self.root, self.routes, now=1000)
        self.assertEqual(self.disk.load("archive/ark.json", {})["entries"]["comparison"], record)

    def test_revert_restores_queues_and_is_idempotent(self):
        apply(self.root, self.routes, now=1000)
        result = revert(self.root)
        self.assertEqual(result["restored"]["ark"], 1)
        for store, state in self.originals.items():
            self.assertEqual(self.disk.load(f"stores/{store}.json", {})["queue"], state["queue"])
        self.assertEqual(self.disk.load("schema.json", {})["state_schema_version"], 1)
        self.assertEqual(sum(revert(self.root)["restored"].values()), 0)
        self.assertEqual(sum(sum(counts.values()) for counts in apply(self.root, self.routes)["archived"].values()), 9)

    def test_revert_does_not_overwrite_existing_task(self):
        apply(self.root, self.routes)
        state = self.disk.load("stores/ark.json", {})
        state["queue"]["comparison"] = {"manual": True}
        self.disk.save("stores/ark.json", state)
        revert(self.root)
        self.assertEqual(self.disk.load("stores/ark.json", {})["queue"]["comparison"], {"manual": True})

    def test_fingerprint_failure_rolls_back_every_written_file(self):
        before = {path: path.read_bytes() for path in self.root.rglob("*.json")}
        with patch("sale_monitor.migrate.fingerprint", side_effect=[{"before": True}, {"before": False}]):
            with self.assertRaisesRegex(RuntimeError, "migration_fingerprint_mismatch"):
                apply(self.root, self.routes)
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*.json")})

    def test_write_failure_restores_original_bytes_and_removes_new_files(self):
        before = {path: path.read_bytes() for path in self.root.rglob("*.json")}
        save = Store.save
        calls = []

        def fail_after_write(disk, name, value):
            save(disk, name, value)
            calls.append(name)
            if len(calls) == 3:
                raise OSError("synthetic failure")

        with patch.object(Store, "save", fail_after_write):
            with self.assertRaisesRegex(OSError, "synthetic failure"):
                apply(self.root, self.routes)
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*.json")})

    def test_normalized_store_fingerprint_ignores_json_formatting(self):
        before = fingerprint(self.root)
        path = self.root / "stores/ark.json"
        path.write_text(path.read_text(encoding="utf-8") + " \n", encoding="utf-8")
        self.assertEqual(before, fingerprint(self.root))

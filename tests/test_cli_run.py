"""Offline CLI contracts for migration, stages, probes and orchestration."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from sale_monitor import cli, pipeline
from sale_monitor.models import Offer, iso
from sale_monitor.planner import update_tracking
from sale_monitor.storage import read_json
from test_pipeline import Fixture, NOW, RUN, item


class CliRunTests(Fixture):
    def call_cli(self, *arguments):
        output = StringIO()
        with redirect_stdout(output):
            cli.main([*arguments, "--state", str(self.root), "--public", str(self.public),
                      "--config", str(self.config_path), "--routes", str(self.routes_path), "--run-id", RUN])
        return json.loads(output.getvalue())

    def test_migrate_archives_and_updates_schema_then_revert_restores_it(self):
        sale = item("ark")
        state = self.save_offer(sale)
        task = {"type": "search", "query": "MODEL-A", "kind": "comparison", "created_at": iso(NOW), "attempts": 7}
        state["queue"] = {"legacy": task}
        self.disk.save("stores/ark.json", state)
        self.disk.save("schema.json", {"state_schema_version": 1})
        result = self.call_cli("migrate", "--input-sha", "fixture-sha")
        self.assertEqual(result["archived"]["ark"], {"comparison_debt_abolished": 1})
        self.assertEqual(self.disk.load("schema.json", {})["state_schema_version"], 2)
        self.assertEqual(self.disk.load("stores/ark.json", {})["queue"], {})
        archive = self.disk.load("archive/ark.json", {})["entries"]["legacy"]
        self.assertEqual(archive["task"], task)
        self.assertEqual(archive["input_sha"], "fixture-sha")
        result = self.call_cli("migrate", "--revert")
        self.assertEqual(result["restored"]["ark"], 1)
        self.assertEqual(self.disk.load("schema.json", {})["state_schema_version"], 1)
        self.assertEqual(self.disk.load("stores/ark.json", {})["queue"], state["queue"])
        self.assertEqual(self.disk.load("stores/ark.json", {})["offers"], state["offers"])

    def test_run_schema_mismatch_exits_nonzero_without_network_or_state_writes(self):
        self.disk.save("schema.json", {"state_schema_version": 1})
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = subprocess.run([sys.executable, "-m", "sale_monitor.cli", "run", "--state", str(self.root),
            "--public", str(self.public), "--routes", str(self.routes_path), "--config", str(self.config_path),
            "--run-id", RUN], capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("state_migration_required", result.stderr)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assertFalse(self.public.exists())

    def test_run_prints_requested_summary_from_real_aggregate(self):
        original = pipeline.run
        with patch("sale_monitor.pipeline.run", side_effect=lambda *a, **k: original(
                *a, **k, now=NOW, spawn=lambda *a, **k: type("Done", (), {"poll": lambda s: 0})())):
            result = self.call_cli("run")
        self.assertEqual(set(result), {"run_id", "run_outcome", "notification_count", "stage_results"})
        self.assertEqual(result["run_id"], RUN)
        self.assertEqual(set(result["stage_results"]), {"ark", "dospara", "sofmap"})
        self.assertEqual(read_json(self.public / "latest.json", {})["run"]["stage_results"], result["stage_results"])

    def test_paused_stage_prints_skipped_without_client(self):
        with patch("sale_monitor.pipeline.Client", side_effect=AssertionError):
            result = self.call_cli("stage", "--phase", "discovery", "--store", "tsukumo")
        self.assertEqual(result["status"], "skipped")

    def test_manual_probe_runs_refused_route_once_and_records_evidence(self):
        route = self.route_config["routes"]["tsukumo:direct"]
        route["probe"] = {"url": "https://example.invalid/", "interval_hours": 24}
        self.save_config()
        probe_result = {"ok": True, "explicit_refusal": False, "summary": "probe_ok"}
        with patch("sale_monitor.routes.probe", return_value=probe_result) as probe:
            result = self.call_cli("probe", "--route", "tsukumo:direct")
        self.assertEqual(result, probe_result)
        probe.assert_called_once()
        saved = self.disk.load("routes.json", {})["routes"]["tsukumo:direct"]
        self.assertEqual(saved["last_probe"]["summary"], "probe_ok")
        self.assertEqual(saved["functions"]["sale_discovery"]["status"], "paused_refused")

    def test_manual_refused_probe_can_use_seed_url_without_automatic_probe_config(self):
        self.config["stores"]["tsukumo"]["seed_urls"] = ["https://shop.tsukumo.co.jp/"]
        self.save_config()
        with patch("sale_monitor.routes.probe", return_value={"ok": False, "explicit_refusal": True, "summary": "explicit_refusal"}) as probe:
            self.call_cli("probe", "--route", "tsukumo:direct")
        self.assertEqual(probe.call_args.args[2]["probe"]["url"], "https://shop.tsukumo.co.jp/")


class TrackingOfferKeys(unittest.TestCase):
    def test_redirected_offer_key_records_success_for_same_store(self):
        peer = item("sofmap")
        plan = {"run_id": RUN, "confirm": {peer.store: [{"url": "https://www.sofmap.com/old-url",
                 "offer_key": peer.key, "identity": peer.identity}]}}
        result = update_tracking({}, plan, [peer], {}, {}, NOW)
        self.assertEqual(result[peer.identity][peer.store]["last_success_at"], iso(NOW))

    def test_offer_key_in_another_store_or_run_does_not_satisfy_request(self):
        peer = item("sofmap")
        plan = {"run_id": RUN, "confirm": {peer.store: [{"url": "https://www.sofmap.com/old-url",
                 "offer_key": peer.key, "identity": peer.identity}]}}
        for updates in ({"store": "ark"}, {"observed_run_id": "old"}, {"product_id": "another"}):
            observed = Offer.from_dict({**peer.to_dict(), **updates})
            self.assertEqual(update_tracking({}, plan, [observed], {}, {}, NOW), {})


if __name__ == "__main__":
    unittest.main()

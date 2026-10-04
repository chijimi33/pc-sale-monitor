"""Offline collection durability and publication boundaries.

Set CHECKPOINT_TEST_TMP_DIR to put all runtime fixtures on a dedicated volume.
Git tests use disposable local bare repositories and allow only file transport.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from sale_monitor.models import STORES, Offer, digest
from sale_monitor.reporting import checkpoint
from sale_monitor.runner import Collector
from sale_monitor.storage import Store, atomic_json, read_json


REPO = Path(__file__).resolve().parents[1]
RUN = "100-1"
OLD_RUN = "99-1"
OLD_DATE = "2026-09-01T01:02:03+00:00"


def snapshot(store="ark", *, run_id=RUN, status="partial"):
    offer = Offer(store, "checkpoint-item", f"https://example.invalid/{store}/item",
                  model="MODEL-1", brand="Fixture", observed_at="2026-10-04T01:00:00+00:00",
                  observed_run_id=run_id, price_yen=9000, shipping_yen=0,
                  condition="new", stock="in_stock", seller_id=store, verified=True)
    row = offer.to_dict()
    old_row = {**deepcopy(row), "observed_at": OLD_DATE, "observed_run_id": OLD_RUN}
    old_receipt = {"observation_id": "older-receipt", "run_id": OLD_RUN, "offer": old_row}
    task_id = digest(["product", offer.url, None, None, "sale", None])[:24]
    task = {"type": "product", "kind": "sale", "url": offer.url, "created_at": OLD_DATE,
            "attempts": 17, "last_error": "http_403", "last_attempt_run_id": OLD_RUN,
            "last_attempt_at": "2026-10-03T01:02:03+00:00", "priority": 1,
            "retry_at_epoch_seconds": 1792000000,
            "discovery_origin": {"created_at": OLD_DATE, "page": 7, "raw": ["old", "手動"]}}
    return {"schema_version": 1, "store": store, "run_id": run_id, "status": status,
            "queue": {task_id: task}, "done": ["already-finished"], "offers": {offer.key: row},
            "journal": [old_receipt, deepcopy(old_receipt),
                        {"observation_id": "current-receipt", "run_id": run_id, "offer": deepcopy(row)},
                        {"observation_id": "unarchivable", "run_id": OLD_RUN,
                         "offer": {"observed_at": "invalid-date", "raw": {"keep": [1, None, "原文"]}}}],
            "cycle_complete": False, "scheduler": {"cursor": 9},
            "custom_metadata": {"keep": ["unrecognized", {"nested": True}]}}


def file_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def isolated_env(root):
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith("GIT_")
           and k.upper() not in {"GITHUB_TOKEN", "GH_TOKEN", "GITHUB_REPOSITORY"}}
    env.update(GITHUB_REPOSITORY="fixture/repo", GIT_ALLOW_PROTOCOL="file",
               GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=str(root / "empty-git-config"),
               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", PYTHONPATH=str(REPO))
    return env


class RuntimeFixture(unittest.TestCase):
    def setUp(self):
        parent = Path(os.environ.get("CHECKPOINT_TEST_TMP_DIR", tempfile.gettempdir())).resolve()
        self.temporary = tempfile.TemporaryDirectory(prefix="collection-checkpoint-", dir=parent)
        self.base = Path(self.temporary.name).resolve()
        # Cleanup is restricted to this newly created fixture, never the checkout.
        self.assertEqual(self.base.parent, parent)
        self.assertTrue(self.base.name.startswith("collection-checkpoint-"))
        self.addCleanup(self.temporary.cleanup)
        self.env = isolated_env(self.base)


class CollectionCheckpointTests(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.root = self.base / "state"
        self.incoming = self.base / "incoming"
        self.public = self.base / "public"

    def artifact(self, value, store=None, incoming=None):
        atomic_json((incoming or self.incoming) / "stores" / ((store or value["store"]) + ".json"), value)

    def assert_manifest(self, result, root=None):
        self.assertEqual(set(result), {"run_id", "accepted_stores", "missing_stores",
                                       "rejected_stores", "journal_rows", "flyer_errors"})
        self.assertEqual(result["run_id"], RUN)
        self.assertEqual(read_json((root or self.root) / "checkpoints" / (RUN + ".json"), None), result)

    def seed_protected_files(self):
        historical = snapshot()["journal"]
        for name, value in {
            "history/ark/2026-09-01.json": historical,
            "events/registry.json": {"states": {"old": {"accepted": True}},
                                     "events": {"old": {"delivery_status": "delivered"}}},
            "metrics/previous.json": {"run_id": OLD_RUN, "samples": 23},
            "requests/ark.json": [], "requests/candidate_identities.json": [],
            "comparison_plan.json": {"run_id": OLD_RUN, "plan": ["unchanged"]},
            "validation/run_provenance.json": {"old": {"event": "schedule"}},
            "validation/manual_review.json": {"reviewer": "existing", "notes": ["keep"]},
        }.items():
            atomic_json(self.root / name, value)
        for name in ("latest.json", "notifications.json", "evidence.json", "validation.json"):
            atomic_json(self.public / name, {"run_id": OLD_RUN, "sentinel": name})

    def test_interruption_then_next_collector_preserves_queue_and_whole_journal(self):
        self.seed_protected_files()
        protected, public = file_bytes(self.root), file_bytes(self.public)
        value = snapshot()
        self.artifact(value)
        incoming = file_bytes(self.incoming)
        # Aggregation never completes after the durable collection save.
        with self.assertRaisesRegex(RuntimeError, "simulated_aggregate_timeout"):
            result = checkpoint(self.root, self.incoming, RUN)
            raise RuntimeError("simulated_aggregate_timeout")
        self.assertEqual(Store(self.root).load("stores/ark.json", None), value)
        self.assertEqual(result["journal_rows"], {"ark": len(value["journal"])})
        self.assert_manifest(result)
        client = SimpleNamespace(count=0, retry_after={}, transport_retry_after={})
        resumed = Collector(self.root, "ark", {"stores": {"ark": {"adapter": "html", "seed_urls": []}}},
                            "101-1", client=client)
        resumed.seed()
        saved = Store(self.root).load("stores/ark.json", None)
        self.assertEqual(saved["queue"], value["queue"])
        self.assertEqual(saved["journal"], value["journal"])
        self.assertEqual(saved["offers"], value["offers"])
        allowed = {"stores/ark.json", "checkpoints/" + RUN + ".json"}
        self.assertEqual({p: data for p, data in file_bytes(self.root).items() if p not in allowed}, protected)
        self.assertEqual(file_bytes(self.public), public)
        self.assertEqual(file_bytes(self.incoming), incoming)

    def test_replaying_same_artifacts_is_byte_identical(self):
        self.seed_protected_files()
        for store in ("koubou", "ark"):
            self.artifact(snapshot(store))
        atomic_json(self.incoming / "flyers/latest.json", {"edition": "fixture", "assets": []})
        first = checkpoint(self.root, self.incoming, RUN)
        before = file_bytes(self.base)
        second = checkpoint(self.root, self.incoming, RUN)
        self.assertEqual(second, first)
        self.assertEqual(file_bytes(self.base), before)
        self.assertEqual(first["accepted_stores"], ["ark", "koubou"])
        for store in first["accepted_stores"]:
            self.assertEqual(Store(self.root).load(f"stores/{store}.json", None), snapshot(store))
        self.assert_manifest(first)

    def test_partial_missing_wrong_attempt_and_malformed_artifacts_are_isolated(self):
        prior = {}
        for store in STORES:
            path = self.root / "stores" / (store + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(snapshot(store, run_id=OLD_RUN), indent=4), encoding="utf-8")
            prior[store] = path.read_bytes()
        healthy = {"amazon": snapshot("amazon", status="access_required"),
                   "yahoo": snapshot("yahoo", status="configuration_needed"),
                   "ark": snapshot(), "dospara": snapshot("dospara", status="running")}
        healthy["amazon"]["access_block"] = {"run_id": RUN, "reason": "amazon_access_challenge"}
        del healthy["yahoo"]["done"]  # Optional; do not invent or normalize it.
        for value in healthy.values():
            self.artifact(value)
        self.artifact(snapshot("tsukumo", run_id="100-2"))
        self.artifact(snapshot("ark"), store="sofmap")
        (self.incoming / "stores/joshin.json").write_text('{"store":', encoding="utf-8")
        result = checkpoint(self.root, self.incoming, RUN)
        accepted = [s for s in STORES if s in healthy]
        self.assertEqual(result["accepted_stores"], accepted)
        self.assertEqual(result["missing_stores"], ["bic", "koubou", "yodobashi"])
        self.assertEqual([r["store"] for r in result["rejected_stores"]], ["tsukumo", "sofmap", "joshin"])
        self.assertEqual(result["journal_rows"], {s: len(healthy[s]["journal"]) for s in accepted})
        self.assertEqual(result["flyer_errors"], [])
        for rejected in result["rejected_stores"]:
            self.assertEqual(set(rejected), {"store", "reason"})
            self.assertTrue(rejected["reason"])
        for store in STORES:
            path = self.root / "stores" / (store + ".json")
            if store in healthy:
                self.assertEqual(read_json(path, None), healthy[store])
            else:
                self.assertEqual(path.read_bytes(), prior[store])
        self.assert_manifest(result)

    def test_invalid_objects_and_containers_do_not_replace_prior_or_block_healthy_store(self):
        cases = [("null", None), ("list", []), ("scalar", 1)]
        for field, bad in (("queue", []), ("offers", []), ("journal", {}), ("done", {}), ("done", None)):
            value = snapshot()
            value[field] = bad
            cases.append((field + "-" + type(bad).__name__, value))
        for field in ("queue", "offers", "journal"):
            value = snapshot()
            del value[field]
            cases.append(("missing-" + field, value))
        for label, value in cases:
            with self.subTest(case=label):
                root, incoming = self.base / label / "state", self.base / label / "incoming"
                atomic_json(root / "stores/ark.json", snapshot(run_id=OLD_RUN))
                before = file_bytes(root)
                self.artifact(value, store="ark", incoming=incoming)
                good = snapshot("amazon", status="access_required")
                self.artifact(good, incoming=incoming)
                result = checkpoint(root, incoming, RUN)
                self.assertEqual(result["accepted_stores"], ["amazon"])
                self.assertEqual([r["store"] for r in result["rejected_stores"]], ["ark"])
                self.assertTrue(result["rejected_stores"][0]["reason"])
                self.assertEqual((root / "stores/ark.json").read_bytes(), before["stores/ark.json"])
                self.assertEqual(read_json(root / "stores/amazon.json", None), good)
                self.assertEqual(result["journal_rows"], {"amazon": len(good["journal"])})
                self.assert_manifest(result, root)

    def test_missing_incoming_preserves_state_and_records_all_missing(self):
        self.seed_protected_files()
        atomic_json(self.root / "stores/ark.json", snapshot(run_id=OLD_RUN))
        before = file_bytes(self.root)
        result = checkpoint(self.root, self.incoming, RUN)
        self.assertEqual(result, {"run_id": RUN, "accepted_stores": [], "missing_stores": list(STORES),
                                  "rejected_stores": [], "journal_rows": {}, "flyer_errors": []})
        self.assertEqual({p: data for p, data in file_bytes(self.root).items()
                          if p != "checkpoints/" + RUN + ".json"}, before)
        self.assertFalse(self.incoming.exists())
        self.assert_manifest(result)

    def test_flyers_are_ignored_without_accepted_koubou(self):
        for mode in ("missing", "wrong-attempt", "malformed"):
            with self.subTest(mode=mode):
                root, incoming = self.base / mode / "state", self.base / mode / "incoming"
                atomic_json(root / "flyers/latest.json", {"edition": "old", "review": "retain"})
                atomic_json(incoming / "flyers/latest.json", {"edition": "unaccepted"})
                atomic_json(incoming / "flyers/assets/new.json", {"text": "unaccepted"})
                self.artifact(snapshot(), incoming=incoming)
                if mode != "missing":
                    self.artifact(snapshot("koubou", run_id="100-2"), incoming=incoming)
                if mode == "malformed":
                    (incoming / "stores/koubou.json").write_text("not json", encoding="utf-8")
                before = file_bytes(root / "flyers")
                result = checkpoint(root, incoming, RUN)
                self.assertEqual(result["accepted_stores"], ["ark"])
                self.assertEqual(file_bytes(root / "flyers"), before)
                self.assertEqual(result["flyer_errors"], [])

    def test_accepted_koubou_copies_good_flyers_and_retains_prior_for_malformed_files(self):
        self.artifact(snapshot("koubou"))
        atomic_json(self.root / "flyers/latest.json", {"edition": "old"})
        atomic_json(self.root / "flyers/assets/bad.json", {"text": "prior evidence"})
        before = file_bytes(self.root / "flyers")
        atomic_json(self.incoming / "flyers/assets/good.json", {"text": "チラシ", "candidates": []})
        # Creation order differs from report order.
        for relative in ("flyers/latest.json", "flyers/assets/bad.json"):
            (self.incoming / relative).write_text("{", encoding="utf-8")
        result = checkpoint(self.root, self.incoming, RUN)
        self.assertEqual(result["accepted_stores"], ["koubou"])
        self.assertEqual([r["path"] for r in result["flyer_errors"]],
                         ["flyers/assets/bad.json", "flyers/latest.json"])
        for error in result["flyer_errors"]:
            self.assertEqual(set(error), {"path", "reason"})
            self.assertTrue(error["reason"])
        for name, data in before.items():
            self.assertEqual((self.root / "flyers" / name).read_bytes(), data)
        self.assertEqual(read_json(self.root / "flyers/assets/good.json", None),
                         {"text": "チラシ", "candidates": []})
        self.assert_manifest(result)

    def test_cli_requires_incoming_and_honors_exact_run_id(self):
        args = [sys.executable, "-B", "-m", "sale_monitor.cli", "checkpoint",
                "--state", str(self.root), "--public", str(self.public), "--run-id", RUN]
        missing = subprocess.run(args, cwd=REPO, env=self.env, capture_output=True,
                                 text=True, encoding="utf-8", timeout=30)
        self.assertEqual(missing.returncode, 2, missing.stdout + missing.stderr)
        self.assertIn("checkpoint requires --incoming", missing.stderr)
        self.assertFalse(self.root.exists())
        self.artifact(snapshot())
        ok = subprocess.run(args + ["--incoming", str(self.incoming)], cwd=REPO, env=self.env,
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        result = json.loads(ok.stdout)
        self.assertEqual(result["accepted_stores"], ["ark"])
        self.assert_manifest(result)
        self.assertFalse(self.public.exists())


class StateOnlyPublicationTests(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.remote = self.base / "remote.git"
        self.work = self.base / "writer"
        self.git("init", "--bare", "--initial-branch=monitor-data", str(self.remote), cwd=self.base)
        self.git("clone", "--quiet", str(self.remote), str(self.work), cwd=self.base)
        self.assertEqual(Path(self.git("remote", "get-url", "origin").stdout.strip()).resolve(), self.remote)

    def git(self, *args, cwd=None, check=True):
        return subprocess.run(["git", *args], cwd=cwd or self.work, env=self.env,
                              capture_output=True, text=True, encoding="utf-8", timeout=30, check=check)

    def publish(self, *, state_only=True, work=None, success=True):
        args = [sys.executable, "-B", str(REPO / "scripts/state_branch.py"),
                "publish", "--path", str(work or self.work)]
        if state_only:
            args.append("--state-only")
        result = subprocess.run(args, cwd=self.base, env=self.env, capture_output=True,
                                text=True, encoding="utf-8", timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def remote_head(self):
        return self.git("rev-parse", "refs/heads/monitor-data", cwd=self.remote).stdout.strip()

    def test_fresh_state_only_clone_needs_no_public_directory(self):
        incoming = self.base / "incoming"
        value = snapshot()
        atomic_json(incoming / "stores/ark.json", value)
        checkpoint(self.work / "state", incoming, RUN)
        self.assertFalse((self.work / "public").exists())
        self.publish()
        first = self.remote_head()
        remote_value = json.loads(self.git("show", first + ":state/stores/ark.json", cwd=self.remote).stdout)
        self.assertEqual(remote_value, value)
        files = self.git("ls-tree", "-r", "--name-only", first, cwd=self.remote).stdout.splitlines()
        self.assertEqual(set(files), {"state/stores/ark.json", "state/checkpoints/" + RUN + ".json"})
        checkpoint(self.work / "state", incoming, RUN)
        self.publish()
        self.assertEqual(self.remote_head(), first)
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_normal_followup_pushes_second_commit_then_both_modes_are_noops(self):
        atomic_json(self.work / "state/stores/ark.json", snapshot())
        atomic_json(self.work / "public/latest.json", {"run_id": OLD_RUN})
        self.publish()
        first = self.remote_head()
        self.assertEqual(self.git("ls-tree", "--name-only", first, cwd=self.remote).stdout.splitlines(), ["state"])
        final = {"run_id": RUN, "notification_count": 0}
        atomic_json(self.work / "public/latest.json", final)
        atomic_json(self.work / "state/events/registry.json", {"states": {}, "events": {}})
        self.publish(state_only=False)
        second = self.remote_head()
        self.assertNotEqual(second, first)
        self.assertEqual(self.git("rev-parse", second + "^", cwd=self.remote).stdout.strip(), first)
        self.assertEqual(json.loads(self.git("show", second + ":public/latest.json", cwd=self.remote).stdout), final)
        self.assertIn("state/events/registry.json", self.git("ls-tree", "-r", "--name-only", second, cwd=self.remote).stdout)
        self.publish(state_only=False)
        self.publish()
        self.assertEqual(self.remote_head(), second)
        self.assertEqual(self.git("rev-list", "--count", second, cwd=self.remote).stdout.strip(), "2")

    def test_state_only_rejects_staged_non_state_paths_before_commit_or_push(self):
        atomic_json(self.work / "state/stores/ark.json", snapshot())
        self.publish()
        first = self.remote_head()
        for relative in ("public/latest.json", "outside-state.json"):
            with self.subTest(path=relative):
                atomic_json(self.work / relative, {"must_not_publish": True})
                self.git("add", relative)
                result = self.publish(success=False)
                self.assertIn("checkpoint_refuses_staged_non_state_files", result.stderr)
                self.assertEqual(self.remote_head(), first)
                self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), first)
                self.assertIn(relative, self.git("diff", "--cached", "--name-only").stdout)
                self.git("restore", "--staged", "--", relative)

    def test_non_fast_forward_is_rejected_without_forcing_either_publish_mode(self):
        atomic_json(self.work / "state/stores/ark.json", snapshot())
        self.publish()
        initial = self.remote_head()
        stale = self.base / "stale-normal-writer"
        rival = self.base / "rival"
        for work in (stale, rival):
            self.git("clone", "--quiet", str(self.remote), str(work), cwd=self.base)
        atomic_json(rival / "state/stores/ark.json", snapshot(run_id="102-1"))
        self.publish(work=rival)
        winner = self.remote_head()
        self.assertNotEqual(winner, initial)
        for work, state_only in ((self.work, True), (stale, False)):
            with self.subTest(state_only=state_only):
                pending = snapshot(run_id="101-1")
                atomic_json(work / "state/stores/ark.json", pending)
                if not state_only:
                    atomic_json(work / "public/latest.json", {"run_id": pending["run_id"]})
                self.publish(work=work, state_only=state_only, success=False)
                self.assertEqual(self.remote_head(), winner)
                self.assertEqual(read_json(work / "state/stores/ark.json", None), pending)
                local = self.git("rev-parse", "HEAD", cwd=work).stdout.strip()
                self.assertNotEqual(local, winner)
                self.assertEqual(self.git("rev-parse", local + "^", cwd=work).stdout.strip(), initial)


if __name__ == "__main__":
    unittest.main()

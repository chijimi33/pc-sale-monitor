from copy import deepcopy
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from io import BytesIO

from sale_monitor.http import Client, FetchError, Page, SafeRedirect
from sale_monitor.routes import RouteState, load_config, probe
from sale_monitor.storage import Store
from urllib.request import Request


CONFIG = Path(__file__).resolve().parents[1] / "config/routes.json"
OK = {"ok": True, "explicit_refusal": False, "http_status": 200, "summary": "ok", "retry_after_until": None}


class RouteTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.disk = Store(Path(self.tmp.name))
        self.config = load_config(CONFIG)
        self.routes = RouteState(self.disk, self.config, now=1000)

    def test_active_stores_and_scope(self):
        self.assertEqual(set(self.routes.active_stores("sale_discovery")), {"ark", "koubou", "dospara", "sofmap"})
        self.assertFalse(self.routes.is_active("tsukumo", "sale_discovery"))
        self.assertEqual(self.routes.status("absent", "absent"), "not_implemented")
        self.assertEqual(set(self.routes.scope()["in_scope_stores"]), {"ark", "koubou", "dospara", "sofmap"})
        self.assertEqual(self.routes.scope()["out_of_scope"]["joshin:direct"]["status"]["product_verify"], "paused_refused")

    def test_scope_membership_is_fixed_when_verification_is_paused(self):
        before = self.routes.scope()
        self.routes.routes["sofmap:direct"]["functions"]["product_verify"]["status"] = "paused_maintenance"
        after = self.routes.scope()
        self.assertEqual(after["version"], before["version"])
        self.assertEqual(after["in_scope_stores"], before["in_scope_stores"])
        self.assertNotIn("sofmap", after["verifiable_stores"])
        self.assertIn("sofmap", after["in_scope_stores"])

    def test_refused_and_configuration_never_probed_even_with_definition(self):
        for route_id in ("joshin:direct", "yahoo:api"):
            self.routes.routes[route_id]["probe"] = {"interval_hours": 1}
        due = {row["route_id"] for row in self.routes.probes_due(1000)}
        self.assertEqual(due, {"tsukumo:direct", "bic:direct", "yodobashi:direct"})

    def test_maintenance_requires_consecutive_successes_and_persists(self):
        route_id = "tsukumo:direct"
        self.routes.record_probe(route_id, OK, 1000)
        self.assertEqual(self.routes.status(route_id, "sale_discovery"), "paused_maintenance")
        self.assertIsNone(self.routes.public_summary()[route_id]["recovery_candidate"])
        self.routes.record_probe(route_id, {**OK, "ok": False}, 2000)
        self.assertEqual(self.routes.routes[route_id]["consecutive_successes"], 0)
        self.routes.record_probe(route_id, OK, 3000)
        self.routes.save()
        resumed = RouteState(self.disk, self.config, now=4000)
        resumed.record_probe(route_id, OK, 4000)
        self.assertEqual(resumed.status(route_id, "sale_discovery"), "paused_maintenance")
        resumed.save()
        reloaded = RouteState(self.disk, self.config)
        self.assertEqual(reloaded.status(route_id, "sale_discovery"), "paused_maintenance")
        summary = resumed.public_summary()[route_id]
        self.assertEqual(summary["last_probe"]["summary"], "ok")
        self.assertEqual(summary["functions"]["sale_discovery"]["reason"], self.config["routes"][route_id]["reason"])
        candidate = summary["recovery_candidate"]
        self.assertEqual(candidate, {"since": summary["last_probe"]["checked_at"], "consecutive_successes": 2,
                                     "last_ok_at": summary["last_probe"]["checked_at"], "evidence": OK})
        self.assertEqual(reloaded.public_summary()[route_id]["recovery_candidate"], candidate)

    def test_successes_only_publish_candidates_for_every_paused_function(self):
        for route_id, status in (("tsukumo:direct", "paused_maintenance"), ("bic:direct", "paused_unreachable")):
            with self.subTest(route_id=route_id):
                self.routes.record_probe(route_id, OK, 1000)
                self.assertIsNone(self.routes.public_summary()[route_id]["recovery_candidate"])
                self.routes.record_probe(route_id, OK, 2000)
                first = self.routes.public_summary()[route_id]["recovery_candidate"]
                evidence = {**OK, "summary": "latest success"}
                self.routes.record_probe(route_id, evidence, 3000)
                summary = self.routes.public_summary()[route_id]
                candidate = summary["recovery_candidate"]
                self.assertEqual(candidate["since"], first["since"])
                self.assertEqual(candidate["consecutive_successes"], 3)
                self.assertEqual(candidate["last_ok_at"], summary["last_probe"]["checked_at"])
                self.assertEqual(candidate["evidence"], evidence)
                self.assertEqual({row["status"] for row in summary["functions"].values()}, {status})
                self.assertNotIn(self.routes.routes[route_id]["store"], self.routes.active_stores("sale_discovery"))
                candidate["evidence"]["summary"] = "mutated public copy"
                self.assertEqual(self.routes.routes[route_id]["recovery_candidate"]["evidence"], evidence)
                self.routes.record_probe(route_id, {**OK, "ok": False}, 4000)
                self.assertIsNone(self.routes.public_summary()[route_id]["recovery_candidate"])
                self.assertEqual(self.routes.routes[route_id]["consecutive_successes"], 0)
                self.routes.record_probe(route_id, OK, 5000)
                self.assertIsNone(self.routes.public_summary()[route_id]["recovery_candidate"])
                self.routes.record_probe(route_id, OK, 6000)
                self.assertNotEqual(self.routes.public_summary()[route_id]["recovery_candidate"]["since"], first["since"])

    def test_candidate_threshold_uses_configured_success_count(self):
        self.routes.routes["tsukumo:direct"]["probe"]["successes_to_resume"] = 3
        for moment in (1000, 2000):
            self.routes.record_probe("tsukumo:direct", OK, moment)
            self.assertIsNone(self.routes.public_summary()["tsukumo:direct"]["recovery_candidate"])
        self.routes.record_probe("tsukumo:direct", OK, 3000)
        self.assertEqual(self.routes.public_summary()["tsukumo:direct"]["recovery_candidate"]["consecutive_successes"], 3)

    def test_unreachable_refusal_is_permanent_until_revision(self):
        for moment in (100, 200):
            self.routes.record_probe("bic:direct", OK, moment)
        self.routes.record_probe("bic:direct", {**OK, "ok": False, "explicit_refusal": True}, 1000)
        self.assertIsNone(self.routes.public_summary()["bic:direct"]["recovery_candidate"])
        self.assertEqual(self.routes.status("bic:direct", "product_verify"), "paused_refused")
        self.assertNotIn("bic:direct", [row["route_id"] for row in self.routes.probes_due(10000000)])
        self.routes.save()
        resumed = RouteState(self.disk, self.config)
        self.assertEqual(resumed.status("bic:direct", "product_verify"), "paused_refused")
        config = deepcopy(self.config)
        config["config_revision"] += 1
        self.assertEqual(RouteState(self.disk, config).status("bic:direct", "product_verify"), "paused_unreachable")

    def test_maintenance_refusal_also_stops_automatic_probing(self):
        # An explicit refusal is respected whatever the earlier pause reason.
        self.routes.record_probe("tsukumo:direct", {**OK, "ok": False, "explicit_refusal": True}, 1000)
        self.assertEqual(self.routes.status("tsukumo:direct", "sale_discovery"), "paused_refused")
        self.assertNotIn("tsukumo:direct", [row["route_id"] for row in self.routes.probes_due(10000000)])
        self.routes.save()
        self.assertEqual(RouteState(self.disk, self.config).status("tsukumo:direct", "sale_discovery"), "paused_refused")

    def test_revision_resets_every_route_and_probe_streak(self):
        for moment in (1000, 2000):
            self.routes.record_probe("tsukumo:direct", {**OK, "retry_after_until": 999999}, moment)
        self.routes.save()
        same_revision = RouteState(self.disk, self.config)
        for key in ("recovery_candidate", "last_probe", "probe_log", "consecutive_successes", "retry_after_until"):
            self.assertEqual(same_revision.routes["tsukumo:direct"][key], self.routes.routes["tsukumo:direct"][key])
        self.config["config_revision"] += 1
        reset = RouteState(self.disk, self.config)
        self.assertEqual(reset.status("tsukumo:direct", "sale_discovery"), "paused_maintenance")
        for key in ("recovery_candidate", "last_probe", "probe_log", "consecutive_successes", "retry_after_until"):
            self.assertNotIn(key, reset.routes["tsukumo:direct"])

    def test_manual_revision_can_activate_a_recovery_candidate(self):
        for moment in (1000, 2000):
            self.routes.record_probe("tsukumo:direct", OK, moment)
        self.routes.save()
        config = deepcopy(self.config)
        config["config_revision"] += 1
        for function in config["routes"]["tsukumo:direct"]["functions"].values():
            function["status"] = "active"
        revised = RouteState(self.disk, config)
        self.assertEqual(revised.status("tsukumo:direct", "sale_discovery"), "active")
        self.assertIn("tsukumo", revised.active_stores("sale_discovery"))
        self.assertIsNone(revised.public_summary()["tsukumo:direct"]["recovery_candidate"])

    def test_old_automatic_active_transitions_are_ignored(self):
        for route_id in ("tsukumo:direct", "bic:direct"):
            for function in self.routes.routes[route_id]["functions"].values():
                function.update(config_status=function["status"], status="active", automatic=True,
                                reason="legacy auto recovery", since="legacy timestamp")
        self.routes.save()
        resumed = RouteState(self.disk, self.config, now=2000)
        for route_id in ("tsukumo:direct", "bic:direct"):
            for name, configured in self.config["routes"][route_id]["functions"].items():
                self.assertEqual(resumed.status(route_id, name), configured["status"])
                self.assertNotIn("automatic", resumed.routes[route_id]["functions"][name])
            self.assertNotIn(resumed.routes[route_id]["store"], resumed.active_stores("sale_discovery"))

    def test_probe_state_is_not_inherited_from_a_newer_revision(self):
        for moment in (1000, 2000):
            self.routes.record_probe("bic:direct", {**OK, "retry_after_until": 999999}, moment)
        self.routes.save()
        saved = self.disk.load("routes.json", {})
        saved["config_revision"] += 1
        self.disk.save("routes.json", saved)
        reset = RouteState(self.disk, self.config)
        for key in ("recovery_candidate", "last_probe", "probe_log", "consecutive_successes", "retry_after_until"):
            self.assertNotIn(key, reset.routes["bic:direct"])

    def test_probe_frequency_remains_daily_or_weekly_after_candidate(self):
        for route_id in ("tsukumo:direct", "bic:direct", "yodobashi:direct"):
            self.routes.record_probe(route_id, OK, 1000)
            self.routes.record_probe(route_id, OK, 2000)
        self.assertEqual(self.routes.probes_due(2001), [])
        self.assertEqual({row["route_id"] for row in self.routes.probes_due(2000 + 24 * 3600)}, {"tsukumo:direct"})
        self.assertEqual({row["route_id"] for row in self.routes.probes_due(2000 + 7 * 24 * 3600)},
                         {"tsukumo:direct", "bic:direct", "yodobashi:direct"})

    def test_arbitrary_saved_override_is_ignored(self):
        self.routes.routes["joshin:direct"]["functions"]["sale_discovery"]["status"] = "active"
        self.routes.save()
        self.assertEqual(RouteState(self.disk, self.config).status("joshin:direct", "sale_discovery"), "paused_refused")

    def test_probe_interval_retry_after_and_log_bound(self):
        result = {**OK, "ok": False, "retry_after_until": 200000}
        for moment in range(1000, 1025):
            self.routes.record_probe("tsukumo:direct", result, moment)
        self.assertEqual(len(self.routes.routes["tsukumo:direct"]["probe_log"]), 20)
        self.assertNotIn("tsukumo:direct", [row["route_id"] for row in self.routes.probes_due(100000)])
        self.assertIn("tsukumo:direct", [row["route_id"] for row in self.routes.probes_due(200000)])

    def test_config_validation(self):
        variants = []
        for change in (lambda cfg: cfg.update(schema_version=2),
                       lambda cfg: cfg["scope"]["in_scope"].append("missing"),
                       lambda cfg: cfg["routes"]["ark:direct"]["functions"]["sale_discovery"].update(status="bad")):
            cfg = deepcopy(self.config)
            change(cfg)
            variants.append(cfg)
        for cfg in variants:
            self.disk.save("bad.json", cfg)
            with self.assertRaises(ValueError):
                load_config(self.disk.root / "bad.json")


class ProbeTests(TestCase):
    cfg = {"probe": {"url": "https://example.test/", "must_not_contain": ["maintenance"]}}

    def test_synthetic_responses_and_single_call(self):
        for code, body, ok, refused in ((200, "ok", True, False), (200, "maintenance", False, False),
                                        (403, "no", False, True), (200, "Access Denied", False, True),
                                        (503, "offline", False, False)):
            with self.subTest(code=code, body=body):
                client = Mock()
                client.get.return_value = Page("https://example.test/", body.encode(), "now", status=code)
                result = probe(client, "test:direct", self.cfg)
                self.assertEqual((result["ok"], result["explicit_refusal"]), (ok, refused))
                client.get.assert_called_once_with(self.cfg["probe"]["url"])

    def test_transport_failure(self):
        client = Mock()
        client.get.side_effect = TimeoutError()
        self.assertFalse(probe(client, "test:direct", self.cfg)["ok"])
        client.get.assert_called_once()

    def test_real_client_does_not_retry_and_keeps_retry_after(self):
        client = Client(delay=0)
        url = self.cfg["probe"]["url"]
        with patch.object(client.opener, "open", side_effect=HTTPError(url, 503, "", {"Retry-After": "60"}, BytesIO(b"offline"))) as op:
            with patch("sale_monitor.routes.time.time", return_value=1000):
                result = probe(client, "test:direct", self.cfg)
        op.assert_called_once()
        self.assertEqual(result["http_status"], 503)
        self.assertEqual(result["retry_after_until"], 1060)

    def test_probe_refuses_redirect_without_a_second_request(self):
        request = Request(self.cfg["probe"]["url"])
        request.single_request = True
        callback = Mock()
        with self.assertRaisesRegex(FetchError, "^probe_redirect$"):
            SafeRedirect(callback).redirect_request(request, None, 302, "", {}, "https://example.test/other")
        callback.assert_not_called()

    def test_retry_after_http_date_and_missing_error_body(self):
        client = Client(delay=0)
        url = self.cfg["probe"]["url"]
        with patch.object(client.opener, "open", side_effect=HTTPError(url, 403, "", {
                "Retry-After": "Thu, 01 Jan 1970 00:20:00 GMT"}, None)) as op:
            result = probe(client, "test:direct", self.cfg)
        op.assert_called_once()
        self.assertEqual(result["retry_after_until"], 1200)
        self.assertTrue(result["explicit_refusal"])

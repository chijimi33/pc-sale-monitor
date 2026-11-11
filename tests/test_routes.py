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

    def test_refused_and_configuration_never_probed_even_with_definition(self):
        for route_id in ("joshin:direct", "yahoo:api"):
            self.routes.routes[route_id]["probe"] = {"interval_hours": 1}
        due = {row["route_id"] for row in self.routes.probes_due(1000)}
        self.assertEqual(due, {"tsukumo:direct", "bic:direct", "yodobashi:direct"})

    def test_maintenance_requires_consecutive_successes_and_persists(self):
        route_id = "tsukumo:direct"
        self.routes.record_probe(route_id, OK, 1000)
        self.assertEqual(self.routes.status(route_id, "sale_discovery"), "paused_maintenance")
        self.routes.record_probe(route_id, {**OK, "ok": False}, 2000)
        self.assertEqual(self.routes.routes[route_id]["consecutive_successes"], 0)
        self.routes.record_probe(route_id, OK, 3000)
        self.routes.save()
        resumed = RouteState(self.disk, self.config, now=4000)
        resumed.record_probe(route_id, OK, 4000)
        self.assertEqual(resumed.status(route_id, "sale_discovery"), "active")
        resumed.save()
        self.assertEqual(RouteState(self.disk, self.config).status(route_id, "sale_discovery"), "active")
        summary = resumed.public_summary()[route_id]
        self.assertEqual(summary["last_probe"]["summary"], "ok")
        self.assertEqual(summary["functions"]["sale_discovery"]["reason"], "ok")

    def test_unreachable_refusal_is_permanent_until_revision(self):
        self.routes.record_probe("bic:direct", {**OK, "ok": False, "explicit_refusal": True}, 1000)
        self.assertEqual(self.routes.status("bic:direct", "product_verify"), "paused_refused")
        self.assertNotIn("bic:direct", [row["route_id"] for row in self.routes.probes_due(10000000)])
        self.routes.save()
        resumed = RouteState(self.disk, self.config)
        self.assertEqual(resumed.status("bic:direct", "product_verify"), "paused_refused")
        config = deepcopy(self.config)
        config["config_revision"] += 1
        self.assertEqual(RouteState(self.disk, config).status("bic:direct", "product_verify"), "paused_unreachable")

    def test_revision_resets_every_route_and_probe_streak(self):
        for moment in (1000, 2000):
            self.routes.record_probe("tsukumo:direct", OK, moment)
        self.routes.save()
        self.config["config_revision"] += 1
        reset = RouteState(self.disk, self.config)
        self.assertEqual(reset.status("tsukumo:direct", "sale_discovery"), "paused_maintenance")
        self.assertNotIn("consecutive_successes", reset.routes["tsukumo:direct"])

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

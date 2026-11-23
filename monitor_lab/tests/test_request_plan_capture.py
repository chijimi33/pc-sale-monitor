"""Four offline integration checks sharing one completed request-plan capture.

Only two study invocations: a mixed two-method run and one interrupted run.
All evidence stays under allowed_root()/tests; no temporary C-drive fixtures.
"""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from sale_monitor.models import STORES
from monitor_lab.acquire import Coordinator, TRANSPORTS
from monitor_lab.capture import replay_capture, verify_capture
from monitor_lab.evidence import normalize
from monitor_lab.safety import allowed_root, atomic_bytes, digest, read, write
from monitor_lab.study import study
from monitor_lab.tests.test_lab import FakeClock


NOW = 1791000000.0
METHODS = ("urllib", "pooled")
RECEIPT_METHODS = tuple(TRANSPORTS[method].method for method in METHODS)
HOME = "https://www.pc-koubou.jp/"
LIST = "https://www.pc-koubou.jp/goods/parts_goods_tokusen.php"
PRODUCT = "https://www.pc-koubou.jp/products/detail.php?product_id=1"
MISSING = "https://www.pc-koubou.jp/products/detail.php?product_id=2"
ARK_HOME = "https://www.ark-pc.co.jp/"
ARK_PRODUCT = "https://www.ark-pc.co.jp/i/1/"
TSUKUMO_LIST = "https://shop.tsukumo.co.jp/special/fixture/"
TSUKUMO_PRODUCT = "https://shop.tsukumo.co.jp/goods/4711289500124/"
# Home/list bodies deliberately contain a price accepted by the product parser.
BODY = b'<html><h1>Request plan fixture</h1><input id="priceIncTax" value="10980"></html>'
RESOURCES = [
    {"store": "koubou", "url": HOME, "kind": "home"},
    {"store": "koubou", "url": LIST, "kind": "list"},
    {"store": "koubou", "url": PRODUCT, "kind": "product"},
    {"store": "koubou", "url": MISSING, "kind": "product"},
    {"store": "ark", "url": ARK_HOME, "kind": "home"},
    {"store": "ark", "url": ARK_PRODUCT, "kind": "product"},
    {"store": "tsukumo", "url": TSUKUMO_LIST, "kind": "list"},
    {"store": "tsukumo", "url": TSUKUMO_PRODUCT, "kind": "product"},
]


def make_plan(resources):
    selected = {resource["store"] for resource in resources}
    plan = {
        "format": "pc-sale-monitor-request-plan-v1",
        "created_at": "2026-10-02T09:00:00+00:00",
        "source_data_sha": "0123456789abcdef0123456789abcdef01234567",
        "resources": deepcopy(resources),
        "not_requested": [{"store": store, "reason": "Outside this bounded offline fixture"}
                          for store in STORES if store not in selected],
    }
    plan["plan_hash"] = digest(plan)
    return plan


class Clock(FakeClock):
    def __init__(self):
        self.now = NOW

    def sleep(self, seconds):
        if not 0 <= seconds <= 180 or self.now + seconds > NOW + 180:
            raise AssertionError("Fixture exceeded its bounded virtual time")
        super().sleep(seconds)


@contextmanager
def no_network():
    with ExitStack() as stack:
        for target in ("socket.socket", "socket.create_connection", "urllib.request.OpenerDirector.open"):
            stack.enter_context(patch(target, side_effect=AssertionError("Fixture must remain offline")))
        yield


@contextmanager
def fake_study(scripts):
    """Inject only the transport and clock; keep real coordination/capture policy."""
    clock, transports, clients, calls = Clock(), [], [], []

    class Transport:
        def __init__(self, method):
            self.method, self.pending, self.closed = TRANSPORTS[method].method, dict(scripts[method]), False
            transports.append(self)

        def get(self, url, timeout):
            calls.append((self.method, url))
            if self.closed or url not in self.pending or not 0 < timeout <= 25:
                raise AssertionError("Unexpected fixture request: " + url)
            clock.sleep(.25)
            return self.pending.pop(url)

        def close(self):
            self.closed = True

    def coordinator(transport, **kwargs):
        kwargs.update(clock=clock, monotonic=clock, sleep=clock.sleep)
        client = Coordinator(transport, **kwargs)
        clients.append(client)
        return client

    factories = {method: (lambda method=method: Transport(method)) for method in scripts}
    with no_network(), patch("monitor_lab.study.TRANSPORTS", factories), \
            patch("monitor_lab.study.Coordinator", coordinator), \
            patch("monitor_lab.study.time", SimpleNamespace(monotonic=clock, perf_counter=clock)):
        yield SimpleNamespace(clock=clock, transports=transports, clients=clients, calls=calls)


def bundle_hashes(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def copy_published_capture(source, output):
    """Copy only the authoritative snapshot, not every historical generation."""
    manifest = read(source / "capture-manifest.json")
    for entry in manifest["files"]:
        atomic_bytes(output / entry["path"], (source / entry["path"]).read_bytes())
    write(output / "capture-manifest.json", manifest)


def rehash_manifest(root):
    manifest = read(root / "capture-manifest.json")
    for entry in manifest["files"]:
        body = (root / entry["path"]).read_bytes()
        entry.update(bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
    manifest["files_sha256"] = digest(manifest["files"])
    write(root / "capture-manifest.json", manifest)
    return manifest


class RequestPlanCaptureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = allowed_root() / "tests" / ("request-plan-capture-" + uuid.uuid4().hex)
        cls.source = cls.root / "mixed"
        cls.plan = make_plan(RESOURCES)
        cls.plan_file = cls.root / "request-plan.json"
        write(cls.plan_file, cls.plan)
        successful = {url: (200, {}, BODY) for url in (HOME, LIST, PRODUCT)}
        responses = {**successful, MISSING: (404, {}, b"Fixture product missing")}
        scripts = {
            "urllib": {**responses, ARK_HOME: (403, {}, b"Access denied"),
                       TSUKUMO_LIST: (429, {"Retry-After": "120"}, b"Fixture rate limit")},
            "pooled": responses,
        }
        cls.emitted = []
        with fake_study(scripts) as cls.mixed_run:
            cls.result = study(cls.source, methods=METHODS, emit=cls.emitted.append,
                               plan=cls.plan_file, budget=120)
            cls.verified = verify_capture(cls.source)
        cls.source_hashes = bundle_hashes(cls.source)

    def setUp(self):
        self.enterContext(no_network())

    def test_mixed_kinds_keep_shared_gates_and_ten_store_scope_across_methods(self):
        receipts = self.verified["receipts"]
        expected_order = [(method, resource["url"], resource["store"], resource["kind"])
                          for method in RECEIPT_METHODS for resource in RESOURCES]
        self.assertEqual(expected_order, [(r["method"], r["url"], r["store"], r["resource_kind"])
                                          for r in receipts])
        self.assertEqual(receipts, self.emitted)
        self.assertEqual(receipts, self.result["receipts"])
        self.assertEqual(self.plan, self.verified["metadata"]["scope_plan"])
        self.assertEqual(self.plan, read(self.plan_file))
        self.assertEqual([("urllib", url) for url in (HOME, LIST, PRODUCT, MISSING, ARK_HOME, TSUKUMO_LIST)]
                         + [("pooled_http11", url) for url in (HOME, LIST, PRODUCT, MISSING)], self.mixed_run.calls)
        self.assertEqual(1, len(self.mixed_run.clients))
        self.assertEqual(NOW + 120, self.mixed_run.clients[0].deadline)
        self.assertEqual(list(RECEIPT_METHODS), [transport.method for transport in self.mixed_run.transports])
        self.assertTrue(all(t.closed and not t.pending for t in self.mixed_run.transports))
        by_key = {(r["method"], r["url"]): r for r in receipts}
        for method, url, error in (("urllib", ARK_HOME, "authentication_or_challenge"),
                                   ("urllib", TSUKUMO_LIST, "retry_after"),
                                   ("urllib", MISSING, "http_404"), ("pooled_http11", MISSING, "http_404")):
            self.assertEqual(error, by_key[method, url]["error"])
            self.assertEqual(1, len(by_key[method, url]["attempts"]))
        skipped = [("urllib", ARK_PRODUCT), ("urllib", TSUKUMO_PRODUCT)] + [
            ("pooled_http11", url) for url in (ARK_HOME, ARK_PRODUCT, TSUKUMO_LIST, TSUKUMO_PRODUCT)]
        for key in skipped:
            receipt = by_key[key]
            self.assertEqual("shared_host_wait", receipt["error"])
            self.assertEqual([], receipt["attempts"])
            self.assertIsNone(receipt["status"])
            self.assertIsNone(receipt["body_file"])
        self.assertTrue(self.verified["hosts"]["www.ark-pc.co.jp"]["blocked"])
        self.assertGreater(self.verified["hosts"]["shop.tsukumo.co.jp"]["until"], self.mixed_run.clock())
        self.assertEqual(10, self.result["http_navigation_attempts"])
        self.assertEqual(6, self.result["successful_pages"])
        scope = self.result["scope"]
        self.assertEqual(10, scope["monitored_store_count"])
        self.assertEqual(set(STORES), set(scope["stores"]))
        self.assertIs(False, scope["full_store_coverage_proven"])
        reasons = {r["store"]: r["reason"] for r in self.plan["not_requested"]}
        for store, row in scope["stores"].items():
            expected = {kind: sorted(r["url"] for r in RESOURCES if r["store"] == store and r["kind"] == kind)
                        for kind in ("home", "list", "product")}
            self.assertEqual(expected, row["permitted_resources"])
            counts = {"koubou": (8, 6, 2), "ark": (1, 0, 0), "tsukumo": (1, 0, 0)}.get(store, (0, 0, 0))
            self.assertEqual(counts, (row["confirmed_http_attempts"], row["successful_responses"],
                                      row["successful_product_responses"]))
            self.assertIn("not_requested_reason", row)
            if store in reasons:
                self.assertEqual(reasons[store], row["not_requested_reason"])
            else:
                self.assertFalse(row["not_requested_reason"])

    def test_offline_replay_never_normalizes_home_or_list_and_preserves_evidence(self):
        normalized = []

        def product_only(store, page, settings, run_id, receipt):
            self.assertEqual("product", receipt["resource_kind"])
            self.assertEqual(PRODUCT, page.url)
            self.assertEqual(receipt["observed_at"], page.observed_at)
            self.assertEqual(BODY, page.body)
            self.assertEqual(len(BODY), receipt["body_bytes"])
            self.assertEqual(hashlib.sha256(BODY).hexdigest(), receipt["body_sha256"])
            normalized.append((receipt["method"], page.url))
            return normalize(store, page, settings, run_id, receipt)

        with patch("monitor_lab.evidence.normalize", side_effect=product_only):
            result = replay_capture(self.source, self.root / "replay")
        self.assertEqual([(method, PRODUCT) for method in RECEIPT_METHODS], normalized)
        self.assertEqual(0, result["http_requests"])
        self.assertEqual(0, result["formal_audits_added"])
        self.assertEqual(0, result["production_prices_added"])
        self.assertEqual(2, result["parsed_pages"])
        self.assertEqual(4, result["non_product_pages"])
        self.assertEqual(self.verified["receipts"], [row["receipt"] for row in result["observations"]])
        for row in result["observations"]:
            receipt = row["receipt"]
            if receipt["error"]:
                self.assertFalse(row["parsed"])
                self.assertIsNone(row["observation"])
            elif receipt["resource_kind"] in {"home", "list"}:
                self.assertFalse(row["parsed"])
                self.assertEqual("non_product_resource", row["skip_reason"])
                self.assertIsNone(row["observation"])
            else:
                self.assertTrue(row["parsed"])
                self.assertEqual(10980, row["observation"]["offer"]["price_yen"])
                self.assertEqual(receipt["observed_at"], row["observation"]["offer"]["observed_at"])
        self.assertEqual(self.source_hashes, bundle_hashes(self.source))

    def test_rehashed_receipt_kind_url_store_and_scope_plan_tampering_are_rejected(self):
        for change in ("kind", "url", "store", "scope_plan", "cli_method", "receipt_method"):
            with self.subTest(change=change):
                target = self.root / ("tampered-" + change)
                copy_published_capture(self.source, target)
                verified = verify_capture(target)
                records = verified["manifest"]["records"]
                receipts, result = verified["receipts"], verified["result"]
                if change == "scope_plan":
                    metadata = verified["metadata"]
                    plan = metadata["scope_plan"]
                    plan["not_requested"] = [r for r in plan["not_requested"] if r["store"] != "amazon"]
                    plan["plan_hash"] = digest({key: value for key, value in plan.items() if key != "plan_hash"})
                    write(target / "study.json", metadata)
                    if "scope_plan" in result:
                        result["scope_plan"] = deepcopy(plan)
                    if "plan_hash" in result:
                        result["plan_hash"] = plan["plan_hash"]
                    if "plan_hash" in metadata:
                        metadata["plan_hash"] = plan["plan_hash"]
                        write(target / "study.json", metadata)
                elif change == "cli_method":
                    metadata = verified['metadata']
                    metadata['methods'][1] = 'pooled_http11'
                    write(target / 'study.json', metadata)
                elif change == "receipt_method":
                    receipts[len(RESOURCES)]['method'] = 'pooled'
                elif change == "kind":
                    receipts[0]["resource_kind"] = "product"
                elif change == "url":
                    url = "https://www.pc-koubou.jp/goods/unplanned-fixture.php"
                    receipts[0].update(url=url, response_url=url)
                    receipts[0]["attempts"][0]["url"] = url
                else:
                    receipts[0]["store"] = "ark"
                # Match the result to the forged receipts so the old generic
                # result/receipt equality check cannot explain the rejection.
                result["receipts"] = deepcopy(receipts)
                write(target / records["receipts"], receipts)
                write(target / records["result"], result)
                manifest = rehash_manifest(target)
                self.assertEqual(digest(manifest["files"]), manifest["files_sha256"])
                for entry in manifest["files"]:
                    body = (target / entry["path"]).read_bytes()
                    self.assertEqual((len(body), hashlib.sha256(body).hexdigest()),
                                     (entry["bytes"], entry["sha256"]))
                with self.assertRaises(ValueError):
                    verify_capture(target)
                replay_output = self.root / ("rejected-replay-" + change)
                with patch("monitor_lab.evidence.normalize", side_effect=AssertionError("Invalid capture parsed")):
                    with self.assertRaises(ValueError):
                        replay_capture(target, replay_output)
                self.assertFalse(replay_output.exists())
        self.assertEqual(self.source_hashes, bundle_hashes(self.source))

    def test_interruption_preserves_resource_kinds_in_verified_partial_capture(self):
        plan = make_plan(RESOURCES[:3])
        plan_file, output = self.root / "partial-plan.json", self.root / "interrupted"
        write(plan_file, plan)
        emitted = []

        def interrupt_after_list(row):
            emitted.append(row)
            if len(emitted) == 2:
                raise RuntimeError("planned interruption after list")

        with fake_study({"urllib": {url: (200, {}, BODY) for url in (HOME, LIST, PRODUCT)}}) as run:
            with self.assertRaisesRegex(RuntimeError, "planned interruption"):
                study(output, methods=("urllib",), emit=interrupt_after_list, plan=plan_file, budget=120)
        self.assertEqual([("urllib", HOME), ("urllib", LIST)], run.calls)
        self.assertEqual(1, len(run.transports))
        self.assertTrue(run.transports[0].closed)
        self.assertEqual({PRODUCT}, set(run.transports[0].pending))
        with self.assertRaisesRegex(ValueError, "incomplete"):
            verify_capture(output)
        partial = verify_capture(output, allow_partial=True)
        self.assertIs(False, partial["manifest"]["complete"])
        self.assertEqual(plan, partial["metadata"]["scope_plan"])
        self.assertEqual(emitted, partial["receipts"])
        self.assertEqual(["home", "list"], [r["resource_kind"] for r in partial["receipts"]])
        self.assertEqual("RuntimeError", read(output / "failure.json")["type"])
        self.assertEqual(2, read(output / "failure.json")["completed_receipts"])
        self.assertFalse((output / "result.json").exists())
        before = bundle_hashes(output)
        with patch("monitor_lab.evidence.normalize", side_effect=AssertionError("Non-product partial capture parsed")):
            replayed = replay_capture(output, self.root / "partial-replay", allow_partial=True)
        self.assertIs(False, replayed["source_complete"])
        self.assertEqual(0, replayed["http_requests"])
        self.assertEqual(0, replayed["parsed_pages"])
        self.assertEqual(2, replayed["non_product_pages"])
        self.assertEqual(partial["receipts"], [row["receipt"] for row in replayed["observations"]])
        for row in replayed["observations"]:
            self.assertFalse(row["parsed"])
            self.assertEqual("non_product_resource", row["skip_reason"])
            self.assertIsNone(row["observation"])
        self.assertEqual(before, bundle_hashes(output))


if __name__ == "__main__":
    unittest.main()

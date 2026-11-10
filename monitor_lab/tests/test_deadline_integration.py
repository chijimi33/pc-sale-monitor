"""Offline request deadlines across real HTTP transports, capture and restart."""
from contextlib import contextmanager, ExitStack
from dataclasses import asdict
import hashlib
from http.client import HTTPResponse
from io import BytesIO
import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import uuid

from sale_monitor.http import Page
from monitor_lab.acquire import Coordinator, PooledTransport, UrllibTransport
from monitor_lab.capture import Capture, replay_capture, verify_capture
from monitor_lab.evidence import normalize
from monitor_lab.pipeline import make_task
from monitor_lab.safety import allowed_root
from monitor_lab.scheduler import collect
from monitor_lab.stores import BACKENDS


NOW = 1791000000.0
PRODUCT = "https://www.pc-koubou.jp/products/detail.php?product_id=1"
OTHER = "https://www.ark-pc.co.jp/i/1/"
DEFERRED = "https://www.pc-koubou.jp/products/detail.php?product_id=2"
BODY = (b'<html><h1>Deadline fixture</h1><input id="priceIncTax" value="10980">'
        b'<p>' + b'x' * 160 + b'</p></html>')
CONTENT_TYPE = "text/html; charset=utf-8"
KINDS = ("urllib", "pooled")
CONFIG = {"default_condition": "new"}


class Clock:
    def __init__(self):
        self.now = NOW
        self.waits = []

    def time(self):
        return self.now

    def advance(self, seconds):
        if not math.isfinite(seconds) or seconds < 0 or self.now + seconds > NOW + 300:
            raise AssertionError("Fixture exceeded its bounded virtual time")
        self.now += seconds

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.advance(seconds)


class PlannedStop(BaseException):
    pass


def after_checkpoint(stage):
    if stage == "after_checkpoint":
        raise PlannedStop()


class DripBytesIO(BytesIO):
    def __init__(self, data, clock, per_byte):
        super().__init__(data)
        self.clock, self.per_byte, self.size = clock, per_byte, len(data)

    def read1(self, amount=-1):
        # Model time spent inside the actual I/O operation, after any wrapper's
        # pre-read deadline check; header readline is covered by opening time.
        if amount != 0 and self.tell() < self.size:
            self.clock.advance(self.per_byte)
        return super().read1(1 if amount < 0 else min(1, amount))


class WireSocket:
    """HTTPResponse's file boundary, with no socket or network activity."""
    def __init__(self, body, status, headers, clock, per_byte):
        lines = [f"HTTP/1.1 {status} Fixture", f"Content-Length: {len(body)}",
                 f"Content-Type: {CONTENT_TYPE}", "Connection: close"]
        lines.extend(f"{key}: {value}" for key, value in headers)
        self.file = DripBytesIO(("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body, clock, per_byte)

    def makefile(self, mode):
        if mode != "rb":
            raise AssertionError("Unexpected response file mode")
        return self.file


class DripResponse(HTTPResponse):
    def __init__(self, url, clock, *, status=200, headers=(), per_byte=0, close_delay=0):
        self.clock, self.close_delay = clock, close_delay
        self.delivered = b""
        super().__init__(WireSocket(BODY, status, headers, clock, per_byte), method="GET", url=url)
        self.begin()

    def read1(self, amount=-1):
        # Observe bytes returned through HTTPResponse's real framing/EOF logic.
        chunk = super().read1(1 if amount < 0 else min(1, amount))
        self.delivered += chunk
        return chunk

    def close(self):
        # A fully read response may still return late from boundary cleanup.
        # This leaves the actual transport result complete and exercises the
        # Coordinator's independent deadline check without replacing get().
        delay, self.close_delay = self.close_delay, 0
        if self.length == 0:
            self.clock.advance(delay)
        super().close()


@contextmanager
def transport_for(kind, clock, responses):
    pending = {url: list(plans) for url, plans in responses.items()}
    calls = []

    def response_for(url, timeout):
        # Record even forbidden attempts: Coordinator catches boundary errors.
        call = {"url": url, "started_at": clock.time(), "timeout": timeout}
        calls.append(call)
        if not pending.get(url):
            raise AssertionError("Unexpected fixture request: " + url)
        plan = dict(pending[url].pop(0))
        clock.advance(plan.pop("opening", 0))
        call["response"] = DripResponse(url, clock, **plan)
        return call["response"]

    class Connection:
        sock = None

        def __init__(self, host, port=None, timeout=None):
            self.host, self.timeout = host, timeout

        def request(self, method, path, headers):
            if method != "GET":
                raise AssertionError("Unexpected fixture method")
            self.url = "https://" + self.host + path

        def getresponse(self):
            return response_for(self.url, self.timeout)

        def close(self):
            pass

    with ExitStack() as stack:
        if kind == "urllib":
            transport = UrllibTransport()
            transport.opener = SimpleNamespace(open=lambda request, timeout: response_for(request.full_url, timeout))
        elif kind == "pooled":
            stack.enter_context(patch("monitor_lab.acquire.http.client.HTTPSConnection", Connection))
            transport = PooledTransport()
        else:
            raise AssertionError("Unknown fixture transport")
        transport.monotonic = clock.time
        try:
            yield transport, calls
        finally:
            transport.close()


class DeadlineIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.root = allowed_root() / "tests" / ("deadline-integration-" + uuid.uuid4().hex)
        for target in ("socket.socket", "socket.create_connection", "time.sleep"):
            self.enterContext(patch(target, side_effect=AssertionError("Real I/O/wait is forbidden in this fixture")))

    def client(self, transport, clock, *, budget=120, capture=None):
        return Coordinator(transport, budget=budget, delay=0, clock=clock.time, monotonic=clock.time,
                           sleep=clock.sleep, capture=capture.body if capture else None)

    def capture(self, name):
        return Capture(self.root / name, {"experiment_id": "lab-deadline-integration", "mode": "fixture"},
                       {"stores": {"koubou": CONFIG}})

    def assert_body(self, row, body, *, complete):
        self.assertEqual(len(body), row["body_bytes"])
        self.assertEqual(hashlib.sha256(body).hexdigest(), row["body_sha256"])
        self.assertIs(not complete, row["body_incomplete"])
        self.assertEqual(CONTENT_TYPE, row["content_type"])
        details = row["http_body"]
        self.assertEqual(1, details["schema"])
        self.assertEqual("content_length", details["framing"])
        self.assertEqual([["content-length", str(len(BODY))]], details["headers"])
        self.assertEqual(len(BODY), details["expected_bytes"])
        self.assertEqual(len(body), details["received_bytes"])
        self.assertIs(complete, details["complete"])
        self.assertEqual(None if complete else "transport:TimeoutError", details["error"])

    def assert_replay_skips(self, capture, name, *, partial=False):
        verified = verify_capture(capture.root, allow_partial=partial)
        self.assertEqual(capture.receipts, verified["receipts"])
        replay = replay_capture(capture.root, self.root / name, allow_partial=partial)
        self.assertEqual(0, replay["http_requests"])
        self.assertEqual(0, replay["parsed_pages"])
        self.assertEqual(0, replay["production_prices_added"])
        self.assertEqual(capture.receipts, [row["receipt"] for row in replay["observations"]])
        for row in replay["observations"]:
            self.assertIs(False, row["parsed"])
            self.assertIsNone(row["observation"])

    def tasks(self):
        tasks = {
            "failed": make_task("koubou", PRODUCT, "2020-01-01T00:00:00+00:00", original={"attempts": 7}),
            "other": make_task("ark", OTHER, "2000-01-01T00:00:00+00:00", original={"attempts": 4}),
            "deferred": make_task("koubou", DEFERRED, "2021-01-01T00:00:00+00:00", original={"attempts": 9}),
        }
        tasks["failed"]["requested"] = True
        for task in tasks.values():
            task["lab_attempts"] = [{"error": "prior_fixture", "fixture": True}]
        return tasks

    def assert_lineage(self, originals, current):
        for key, original in originals.items():
            self.assertEqual(original["created_at"], current[key]["created_at"])
            self.assertEqual(original["attempts"], current[key]["attempts"])
            self.assertEqual(original["lab_attempts"][0], current[key]["lab_attempts"][0])

    def on_page(self, task, page, receipt, *_):
        self.assertEqual(BODY, page.body)
        self.assertIsNone(receipt["error"])
        self.assert_body(receipt, BODY, complete=True)
        observation = normalize(task["lab_store"], page, CONFIG, "lab-deadline-integration", receipt)
        if task["lab_store"] == "koubou":
            self.assertEqual(10980, observation.offer.price_yen)
        return [("observations", task["url"], observation.to_dict())]

    def test_partial_timeout_capture_retains_prefix_and_policy_but_replay_never_parses(self):
        for kind in KINDS:
            for status, headers, error in ((200, (), "transport:TimeoutError"),
                                          (403, (), "authentication_or_challenge"),
                                          (200, (("Retry-After", "90"),), "retry_after")):
                with self.subTest(transport=kind, policy=error):
                    name = kind + "-" + error.replace(":", "-")
                    clock, capture = Clock(), self.capture(name)
                    plan = {"opening": .5, "per_byte": 1 / 64, "status": status, "headers": headers}
                    with transport_for(kind, clock, {PRODUCT: [plan]}) as (transport, calls):
                        client = self.client(transport, clock, budget=2, capture=capture)
                        page, receipt = client.fetch(PRODUCT)
                        self.assertIsNone(page)
                        self.assertEqual(error, receipt.error)
                        self.assertEqual(status, receipt.status)
                        self.assertEqual(2, calls[0]["timeout"])
                        self.assertEqual(2, receipt.elapsed_seconds)
                        # Header acquisition uses .5 s of the same 2 s budget.
                        prefix = BODY[:96]
                        self.assertEqual(prefix, calls[0]["response"].delivered)
                        self.assert_body(asdict(receipt), prefix, complete=False)
                        self.assertEqual(prefix, (capture.root / receipt.body_file).read_bytes())
                        # The prefix already parses to a price with the real parser.
                        # Replay must exclude it on provenance, not parse failure.
                        parsed = normalize("koubou", Page(PRODUCT, prefix, receipt.observed_at), CONFIG,
                                           "prefix-control", asdict(receipt))
                        self.assertEqual(10980, parsed.offer.price_yen)
                        capture.append({"store": "koubou", **asdict(receipt)}, client.hosts)
                        gate = client.hosts[urlsplit(PRODUCT).hostname]
                        if status == 403:
                            self.assertTrue(gate["blocked"])
                        else:
                            self.assertFalse(gate.get("blocked", False))
                            self.assertEqual(90 if headers else 60, gate["until"] - clock.time())
                        self.assertEqual(error, gate["reason"])
                        page, waiting = client.fetch(DEFERRED)
                        self.assertIsNone(page)
                        self.assertEqual("shared_host_wait", waiting.error)
                        self.assertEqual(1, len(calls))
                        self.assertEqual([], clock.waits)
                    self.assert_replay_skips(capture, name + "-partial-replay", partial=True)
                    capture.checkpoint(result={**capture.metadata, "receipts": capture.receipts})
                    self.assert_replay_skips(capture, name + "-final-replay")

    def test_restart_preserves_25_second_timeout_wait_budget_history_and_cap_on_all_backends(self):
        for backend, cls in BACKENDS.items():
            for kind in KINDS:
                with self.subTest(backend=backend, transport=kind):
                    root, clock, tasks = self.root / backend / kind, Clock(), self.tasks()
                    urls, calls = {task["url"] for task in tasks.values()}, []
                    options = dict(architecture="B", cycles=1, max_tasks=3, budget=120)
                    with cls(root) as store:
                        store.commit("import", [("tasks", key, value) for key, value in tasks.items()])
                        with transport_for(kind, clock, {PRODUCT: [{"opening": 1, "per_byte": .25}]}) as (transport, first):
                            with self.assertRaises(PlannedStop):
                                collect(store, self.client(transport, clock), "deadline-restart", urls, self.on_page,
                                        hook=after_checkpoint, **options)
                            calls.extend(first)
                        checkpoint = store.snapshot()
                    records = checkpoint["records"]
                    failed = records["tasks"]["failed"]["lab_attempts"][-1]
                    self.assertEqual("transport:TimeoutError", failed["error"])
                    self.assertEqual(200, failed["status"])
                    self.assertEqual(25, failed["elapsed_seconds"])
                    self.assertEqual(25, calls[0]["timeout"])
                    self.assertEqual(BODY[:96], calls[0]["response"].delivered)
                    self.assert_body(failed, BODY[:96], complete=False)
                    self.assertEqual("waiting", records["tasks"]["failed"]["lab_status"])
                    self.assertEqual({}, records.get("observations", {}))
                    self.assert_lineage(tasks, records["tasks"])
                    control = records["scheduler"]["collection"]
                    self.assertEqual(NOW, control["started_at_epoch"])
                    self.assertEqual(NOW + 120, control["deadline_epoch"])
                    self.assertEqual(1, control["reserved_resources"])
                    self.assertEqual(1, control["confirmed_http_requests"])
                    self.assertIsNone(control["active"])
                    until = records["hosts"][urlsplit(PRODUCT).hostname]["until"]
                    self.assertEqual(NOW + 85, until)
                    clock.advance(5)  # Process downtime must consume the original budget.
                    with cls(root) as store:
                        self.assertEqual(checkpoint, store.snapshot())
                        with transport_for(kind, clock, {OTHER: [{"opening": 1}], PRODUCT: [{"opening": 1}]}) as (transport, retry):
                            result = collect(store, self.client(transport, clock), "deadline-restart", urls,
                                             self.on_page, **options)
                            calls.extend(retry)
                        exhausted = store.snapshot()
                    state = exhausted["records"]
                    self.assertEqual([PRODUCT, OTHER, PRODUCT], [call["url"] for call in calls])
                    self.assertEqual([25, 25, 25], [call["timeout"] for call in calls])
                    self.assertLess(calls[1]["started_at"], until)
                    self.assertEqual(until, calls[2]["started_at"])
                    self.assertEqual([54], clock.waits)
                    self.assertEqual({PRODUCT, OTHER}, set(state["observations"]))
                    self.assertEqual("resource_limit", result["scheduling"]["stop_reason"])
                    self.assertEqual(["completed"], [wait["state"] for wait in result["scheduling"]["waits"]])
                    self.assertEqual(2, result["scheduling"]["waits"][0]["waiting_tasks"])
                    final = state["scheduler"]["collection"]
                    for key in ("started_at_epoch", "deadline_epoch", "budget_seconds", "options"):
                        self.assertEqual(control[key], final[key])
                    self.assertEqual(3, final["reserved_resources"])
                    self.assertEqual(3, final["confirmed_http_requests"])
                    self.assertEqual(3, len(state["dispatches"]))
                    self.assertEqual([PRODUCT, OTHER, PRODUCT], final["request_sequence"])
                    self.assertLess(clock.time(), final["deadline_epoch"])
                    self.assert_lineage(tasks, state["tasks"])
                    self.assertEqual("complete", state["tasks"]["failed"]["lab_status"])
                    self.assertEqual("complete", state["tasks"]["other"]["lab_status"])
                    self.assertEqual(tasks["deferred"], state["tasks"]["deferred"])
                    self.assertEqual(["prior_fixture", "transport:TimeoutError", None],
                                     [row["error"] for row in state["tasks"]["failed"]["lab_attempts"]])
                    with cls(root) as store:
                        with transport_for(kind, clock, {}) as (transport, forbidden):
                            stopped = collect(store, self.client(transport, clock), "deadline-restart", urls,
                                              self.on_page, **options)
                            self.assertEqual([], forbidden)
                        self.assertEqual("resource_limit", stopped["scheduling"]["stop_reason"])
                        self.assertEqual(exhausted, store.snapshot())

    def test_resume_after_overall_deadline_never_fetches_or_completes_timed_out_work(self):
        for backend, cls in BACKENDS.items():
            for kind in KINDS:
                with self.subTest(backend=backend, transport=kind):
                    root, clock, tasks = self.root / backend / kind, Clock(), self.tasks()
                    urls = {task["url"] for task in tasks.values()}
                    options = dict(architecture="B", cycles=1, max_tasks=3, budget=2)
                    with cls(root) as store:
                        store.commit("import", [("tasks", key, value) for key, value in tasks.items()])
                        with transport_for(kind, clock, {PRODUCT: [{"opening": .5, "per_byte": 1 / 64}]}) as (transport, first):
                            with self.assertRaises(PlannedStop):
                                collect(store, self.client(transport, clock), "deadline-expired", urls, self.on_page,
                                        hook=after_checkpoint, **options)
                            self.assertEqual(2, first[0]["timeout"])
                        checkpoint = store.snapshot()
                    records = checkpoint["records"]
                    failed = records["tasks"]["failed"]
                    self.assertEqual("waiting", failed["lab_status"])
                    self.assertEqual("transport:TimeoutError", failed["lab_last_error"])
                    self.assert_body(failed["lab_attempts"][-1], BODY[:96], complete=False)
                    self.assert_lineage(tasks, records["tasks"])
                    self.assertEqual({}, records.get("observations", {}))
                    control = records["scheduler"]["collection"]
                    self.assertEqual(NOW + 2, control["deadline_epoch"])
                    self.assertEqual(1, control["reserved_resources"])
                    self.assertEqual(1, control["confirmed_http_requests"])
                    # The shared host wait has also expired; only the persisted
                    # overall deadline can prohibit another request now.
                    clock.advance(61)
                    self.assertGreater(clock.time(), records["hosts"][urlsplit(PRODUCT).hostname]["until"])
                    for _ in range(2):
                        with cls(root) as store:
                            self.assertEqual(checkpoint, store.snapshot())
                            with transport_for(kind, clock, {}) as (transport, forbidden):
                                client = self.client(transport, clock, budget=120)
                                result = collect(store, client, "deadline-expired", urls, self.on_page, **options)
                                self.assertEqual([], forbidden)
                                self.assertEqual(0, client.requests)
                                self.assertEqual(clock.time(), client.deadline)
                            self.assertEqual("budget_exhausted", result["scheduling"]["stop_reason"])
                            self.assertEqual(control, result["scheduling"]["control"])
                            self.assertEqual([], result["receipts"])
                            self.assertEqual({}, result["completed_task_seconds"])
                            self.assertEqual(checkpoint, store.snapshot())
                    self.assertEqual([], clock.waits)

    def test_coordinator_rejects_late_complete_results_but_keeps_full_capture_and_policy(self):
        for kind in KINDS:
            for budget in (2, 120):
                for status, headers, error in ((200, (), "transport:TimeoutError"),
                                              (403, (), "authentication_or_challenge"),
                                              (200, (("Retry-After", "90"),), "retry_after")):
                    with self.subTest(transport=kind, budget=budget, policy=error):
                        name = f"late-{kind}-{budget}-" + error.replace(":", "-")
                        clock, capture = Clock(), self.capture(name)
                        timeout = min(25, budget)
                        plan = {"opening": .25, "close_delay": timeout + 1, "status": status, "headers": headers}
                        with transport_for(kind, clock, {PRODUCT: [plan]}) as (transport, calls):
                            client = self.client(transport, clock, budget=budget, capture=capture)
                            page, receipt = client.fetch(PRODUCT)
                            self.assertIsNone(page)
                            self.assertEqual(error, receipt.error)
                            self.assertEqual(status, receipt.status)
                            self.assertEqual(timeout, calls[0]["timeout"])
                            self.assertEqual(timeout + 1.25, receipt.elapsed_seconds)
                            if budget == 120:
                                self.assertLess(clock.time(), client.deadline)
                            self.assertEqual(BODY, calls[0]["response"].delivered)
                            self.assert_body(asdict(receipt), BODY, complete=True)
                            self.assertEqual(BODY, (capture.root / receipt.body_file).read_bytes())
                            gate = client.hosts[urlsplit(PRODUCT).hostname]
                            self.assertEqual(error, gate["reason"])
                            if status == 403:
                                self.assertTrue(gate["blocked"])
                            else:
                                self.assertEqual(90 if headers else 60, gate["until"] - clock.time())
                            capture.append({"store": "koubou", **asdict(receipt)}, client.hosts)
                        capture.checkpoint(result={**capture.metadata, "receipts": capture.receipts})
                        self.assert_replay_skips(capture, name + "-replay")


if __name__ == "__main__":
    unittest.main()

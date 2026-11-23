"""Offline HTTP framing regressions across acquisition, evidence and scheduling."""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
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

from monitor_lab.acquire import Coordinator, PooledTransport, UrllibTransport
from monitor_lab.capture import Capture, replay_capture, verify_capture
from monitor_lab.evidence import normalize
from monitor_lab.pipeline import make_task
from monitor_lab.safety import allowed_root, digest, read, write
from monitor_lab.scheduler import collect
from monitor_lab.stores import BACKENDS


NOW = 1791000000.0
PRODUCT = "https://www.pc-koubou.jp/products/detail.php?product_id=1"
OTHER = "https://www.ark-pc.co.jp/i/1/"
DEFERRED = "https://www.pc-koubou.jp/products/detail.php?product_id=2"
BODY = b'<html><h1>Body integration fixture</h1><input id="priceIncTax" value="10980"></html>'
# Already contains a parseable price: silently accepting this prefix is unsafe.
PREFIX = BODY[:-7]
KINDS = ("urllib", "pooled")


class Clock:
    def __init__(self):
        self.now = NOW

    def time(self):
        return self.now

    def sleep(self, seconds):
        if not math.isfinite(seconds) or seconds < 0 or self.now + seconds > NOW + 180:
            raise AssertionError("Fixture exceeded its bounded virtual time")
        self.now += seconds


class PlannedStop(BaseException):
    pass


def wire(body, *, expected=None, status=200, headers=()):
    expected = len(body) if expected is None else expected
    lines = [f"HTTP/1.1 {status} Fixture", f"Content-Length: {expected}",
             "Content-Type: text/html; charset=utf-8", "Connection: close"]
    lines.extend(f"{key}: {value}" for key, value in headers)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


class WireSocket:
    """Only the file interface HTTPResponse requires; no real socket exists."""
    def __init__(self, data):
        self.file = BytesIO(data)

    def makefile(self, mode):
        if mode != "rb":
            raise AssertionError("Unexpected response file mode")
        return self.file


@contextmanager
def transport_for(kind, clock, responses):
    pending = {url: list(items) for url, items in responses.items()}
    calls = []

    def response_for(url, timeout):
        if not pending.get(url):
            raise AssertionError("Unexpected fixture request: " + url)
        calls.append({"url": url, "started_at": clock.time(), "timeout": timeout})
        clock.sleep(1)
        response = HTTPResponse(WireSocket(pending[url].pop(0)), method="GET", url=url)
        response.begin()
        return response

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

    # Patch only the I/O boundary; both transports and HTTPResponse parsing are real.
    with ExitStack() as stack:
        if kind == "urllib":
            transport = UrllibTransport()
            transport.opener = SimpleNamespace(open=lambda request, timeout: response_for(request.full_url, timeout))
        elif kind == "pooled":
            stack.enter_context(patch("monitor_lab.acquire.http.client.HTTPSConnection", Connection))
            transport = PooledTransport()
        else:
            raise AssertionError("Unknown fixture transport")
        try:
            yield transport, calls
        finally:
            transport.close()


class HTTPBodyIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.root = allowed_root() / "tests" / ("http-body-integration-" + uuid.uuid4().hex)
        for target in ("socket.socket", "socket.create_connection"):
            self.enterContext(patch(target, side_effect=AssertionError("Network is forbidden in this fixture")))

    def capture(self, name):
        return Capture(self.root / name, {"experiment_id": "lab-http-body-integration", "mode": "fixture"},
                       {"stores": {"koubou": {"default_condition": "new"}}})

    def client(self, transport, clock, capture=None):
        return Coordinator(transport, budget=120, delay=0, clock=clock.time, monotonic=clock.time,
                           sleep=clock.sleep, capture=capture.body if capture else None)

    def record(self, capture, client):
        page, receipt = client.fetch(PRODUCT)
        capture.append({"store": "koubou", **asdict(receipt)}, client.hosts)
        return page, receipt

    def finish(self, capture):
        capture.checkpoint(result={**capture.metadata, "receipts": capture.receipts})

    def assert_body(self, row, *, complete):
        body = BODY if complete else PREFIX
        self.assertEqual(len(body), row["body_bytes"])
        self.assertEqual(hashlib.sha256(body).hexdigest(), row["body_sha256"])
        self.assertEqual(not complete, row["body_incomplete"])
        framing = row["http_body"]
        self.assertEqual(1, framing["schema"])
        self.assertEqual(len(BODY), framing["expected_bytes"])
        self.assertEqual(len(body), framing["received_bytes"])
        self.assertIs(complete, framing["complete"])
        if complete:
            self.assertFalse(framing["error"])
        else:
            self.assertTrue(framing["error"])

    def test_capture_retains_real_truncation_and_replays_only_the_complete_retry(self):
        for kind in KINDS:
            with self.subTest(transport=kind):
                clock, capture = Clock(), self.capture(kind)
                with transport_for(kind, clock, {PRODUCT: [wire(PREFIX, expected=len(BODY)), wire(BODY)]}) as (transport, calls):
                    client = self.client(transport, clock, capture)
                    page, receipt = self.record(capture, client)
                    self.assertIsNone(page)
                    self.assertEqual(200, receipt.status)
                    self.assertEqual("transport:IncompleteRead", receipt.error)
                    self.assert_body(asdict(receipt), complete=False)
                    self.assertEqual(PREFIX, (capture.root / receipt.body_file).read_bytes())
                    gate = client.hosts[urlsplit(PRODUCT).hostname]
                    self.assertFalse(gate.get("blocked", False))
                    self.assertTrue(math.isfinite(gate["until"]))
                    self.assertGreaterEqual(gate["until"] - clock.time(), 60)
                    partial = verify_capture(capture.root, allow_partial=True)
                    self.assertEqual(asdict(receipt), {k: v for k, v in partial["receipts"][0].items() if k != "store"})
                    with patch("monitor_lab.evidence.normalize", wraps=normalize) as parser:
                        replay = replay_capture(capture.root, self.root / (kind + "-partial-replay"), allow_partial=True)
                        self.assertEqual(0, replay["parsed_pages"])
                        self.assertEqual(0, replay["http_requests"])
                        parser.assert_not_called()
                    clock.sleep(gate["until"] - clock.time())
                    page, complete = self.record(capture, client)
                    self.assertEqual(BODY, page.body)
                    self.assertEqual(200, complete.status)
                    self.assertIsNone(complete.error)
                    self.assert_body(asdict(complete), complete=True)
                    self.assertEqual(BODY, (capture.root / complete.body_file).read_bytes())
                    self.assertEqual(2, len(calls))
                self.finish(capture)
                verified = verify_capture(capture.root)
                self.assertEqual([200, 200], [row["status"] for row in verified["receipts"]])
                with patch("monitor_lab.evidence.normalize", wraps=normalize) as parser:
                    replay = replay_capture(capture.root, self.root / (kind + "-complete-replay"))
                    self.assertEqual([False, True], [row["parsed"] for row in replay["observations"]])
                    self.assertEqual(1, replay["parsed_pages"])
                    self.assertEqual(0, replay["http_requests"])
                    parser.assert_called_once()
                    self.assertEqual(BODY, parser.call_args.args[1].body)
                    self.assertEqual(complete.observed_at, parser.call_args.args[1].observed_at)

    def test_checkpoint_restart_preserves_wait_history_and_cap_on_every_backend(self):
        for backend, cls in BACKENDS.items():
            for kind in KINDS:
                with self.subTest(backend=backend, transport=kind):
                    root = self.root / backend / kind
                    clock, parsed, calls = Clock(), [], []
                    tasks = {
                        "failed": make_task("koubou", PRODUCT, "2020-01-01T00:00:00+00:00", original={"attempts": 7}),
                        "other": make_task("ark", OTHER, "2000-01-01T00:00:00+00:00", original={"attempts": 4}),
                        "deferred": make_task("koubou", DEFERRED, "2021-01-01T00:00:00+00:00", original={"attempts": 9}),
                    }
                    tasks["failed"]["requested"] = True
                    for task in tasks.values():
                        task["lab_attempts"] = [{"error": "prior_fixture", "fixture": True}]
                    options = dict(architecture="B", cycles=1, max_tasks=3, budget=120)
                    urls = {task["url"] for task in tasks.values()}

                    def on_page(task, page, receipt, *_):
                        self.assertEqual(BODY, page.body)
                        self.assertIsNone(receipt["error"])
                        self.assert_body(receipt, complete=True)
                        parsed.append(page.url)
                        return [("observations", task["url"], {"body_sha256": receipt["body_sha256"]})]

                    def interrupt(stage):
                        if stage == "after_checkpoint":
                            raise PlannedStop()

                    with cls(root) as store:
                        store.commit("import", [("tasks", key, value) for key, value in tasks.items()])
                        with transport_for(kind, clock, {PRODUCT: [wire(PREFIX, expected=len(BODY))]}) as (transport, first_calls):
                            with self.assertRaises(PlannedStop):
                                collect(store, self.client(transport, clock), "body-restart", urls, on_page,
                                        hook=interrupt, **options)
                            calls.extend(first_calls)
                        checkpoint = store.snapshot()
                    first = checkpoint["records"]
                    failed = first["tasks"]["failed"]["lab_attempts"][-1]
                    self.assertEqual(200, failed["status"])
                    self.assertEqual("transport:IncompleteRead", failed["error"])
                    self.assert_body(failed, complete=False)
                    self.assertEqual([], parsed)
                    self.assertEqual({}, first.get("observations", {}))
                    control = first["scheduler"]["collection"]
                    self.assertEqual(1, control["reserved_resources"])
                    self.assertEqual(1, control["confirmed_http_requests"])
                    self.assertIsNone(control["active"])
                    until = first["hosts"][urlsplit(PRODUCT).hostname]["until"]
                    self.assertTrue(math.isfinite(until))
                    self.assertGreaterEqual(until - clock.time(), 60)

                    with cls(root) as store:
                        self.assertEqual(checkpoint, store.snapshot())
                        with transport_for(kind, clock, {OTHER: [wire(BODY)], PRODUCT: [wire(BODY)]}) as (transport, retry_calls):
                            result = collect(store, self.client(transport, clock), "body-restart", urls, on_page, **options)
                            calls.extend(retry_calls)
                        exhausted = store.snapshot()
                    state = exhausted["records"]
                    self.assertEqual([PRODUCT, OTHER, PRODUCT], [call["url"] for call in calls])
                    self.assertLess(calls[1]["started_at"], until)
                    self.assertGreaterEqual(calls[2]["started_at"], until)
                    self.assertEqual([OTHER, PRODUCT], parsed)
                    self.assertEqual({PRODUCT, OTHER}, set(state["observations"]))
                    self.assertEqual("resource_limit", result["scheduling"]["stop_reason"])
                    self.assertEqual(["completed"], [wait["state"] for wait in result["scheduling"]["waits"]])
                    final_control = state["scheduler"]["collection"]
                    for key in ("started_at_epoch", "deadline_epoch", "options"):
                        self.assertEqual(control[key], final_control[key])
                    self.assertEqual(3, final_control["reserved_resources"])
                    self.assertEqual(3, final_control["confirmed_http_requests"])
                    self.assertEqual(3, len(state["dispatches"]))
                    self.assertLess(clock.time(), final_control["deadline_epoch"])
                    for key, original in tasks.items():
                        current = state["tasks"][key]
                        self.assertEqual(original["created_at"], current["created_at"])
                        self.assertEqual(original["attempts"], current["attempts"])
                        self.assertEqual(original["lab_attempts"][0], current["lab_attempts"][0])
                    self.assertEqual("complete", state["tasks"]["failed"]["lab_status"])
                    self.assertEqual("complete", state["tasks"]["other"]["lab_status"])
                    self.assertEqual("pending", state["tasks"]["deferred"]["lab_status"])
                    self.assertEqual(["prior_fixture", "transport:IncompleteRead", None],
                                     [row["error"] for row in state["tasks"]["failed"]["lab_attempts"]])
                    # Unfinished work is still eligible, but a fresh client cannot reset the cap.
                    with cls(root) as store:
                        with transport_for(kind, clock, {}) as (transport, forbidden_calls):
                            stopped = collect(store, self.client(transport, clock), "body-restart", urls, on_page, **options)
                            self.assertEqual([], forbidden_calls)
                        self.assertEqual("resource_limit", stopped["scheduling"]["stop_reason"])
                        self.assertEqual(exhausted, store.snapshot())

    def test_semantic_tampering_cannot_promote_a_partial_body_after_rehashing(self):
        for kind in KINDS:
            for mutation in ("complete", "received_bytes", "clear_failure"):
                with self.subTest(transport=kind, mutation=mutation):
                    name = kind + "-" + mutation
                    capture, clock = self.capture(name), Clock()
                    with transport_for(kind, clock, {PRODUCT: [wire(PREFIX, expected=len(BODY))]}) as (transport, _):
                        page, receipt = self.record(capture, self.client(transport, clock, capture))
                        self.assertIsNone(page)
                        self.assert_body(asdict(receipt), complete=False)
                    self.finish(capture)
                    verify_capture(capture.root)
                    manifest = read(capture.root / "capture-manifest.json")
                    receipts = read(capture.root / manifest["records"]["receipts"])
                    row = receipts[0]
                    if mutation == "complete":
                        row["http_body"]["complete"] = True
                    elif mutation == "received_bytes":
                        row["http_body"]["received_bytes"] += 1
                    else:
                        row.update(error=None, body_incomplete=False)
                    write(capture.root / manifest["records"]["receipts"], receipts)
                    result_file = capture.root / manifest["records"]["result"]
                    result = read(result_file)
                    result["receipts"] = deepcopy(receipts)
                    write(result_file, result)
                    # Keep all ordinary checksums and receipt/result equality valid.
                    for entry in manifest["files"]:
                        body = (capture.root / entry["path"]).read_bytes()
                        entry.update(bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
                    manifest["files_sha256"] = digest(manifest["files"])
                    write(capture.root / manifest["records"]["receipts"].rsplit("/", 1)[0] / "manifest.json", manifest)
                    write(capture.root / "capture-manifest.json", manifest)
                    with self.assertRaises(ValueError):
                        verify_capture(capture.root)
                    output = self.root / (name + "-replay")
                    with patch("monitor_lab.evidence.normalize", wraps=normalize) as parser:
                        with self.assertRaises(ValueError):
                            replay_capture(capture.root, output)
                        parser.assert_not_called()
                    self.assertFalse(output.exists())

    def test_authentication_and_retry_after_precede_truncation_in_saved_evidence(self):
        for kind in KINDS:
            for status, headers, error in ((403, (), "authentication_or_challenge"),
                                            (200, (("Retry-After", "120"),), "retry_after")):
                with self.subTest(transport=kind, policy=error):
                    name = kind + "-" + error
                    capture, clock = self.capture(name), Clock()
                    response = wire(PREFIX, expected=len(BODY), status=status, headers=headers)
                    with transport_for(kind, clock, {PRODUCT: [response]}) as (transport, calls):
                        client = self.client(transport, clock, capture)
                        page, receipt = self.record(capture, client)
                        self.assertIsNone(page)
                        self.assertEqual(status, receipt.status)
                        self.assertEqual(error, receipt.error)
                        self.assert_body(asdict(receipt), complete=False)
                        self.assertEqual(PREFIX, (capture.root / receipt.body_file).read_bytes())
                        gate = client.hosts[urlsplit(PRODUCT).hostname]
                        if status == 403:
                            self.assertTrue(gate["blocked"])
                        else:
                            self.assertFalse(gate.get("blocked", False))
                            self.assertTrue(math.isfinite(gate["until"]))
                            self.assertGreaterEqual(gate["until"] - clock.time(), 120)
                        page, waiting = client.fetch(DEFERRED)
                        capture.append({"store": "koubou", **asdict(waiting)}, client.hosts)
                        self.assertIsNone(page)
                        self.assertEqual("shared_host_wait", waiting.error)
                        self.assertEqual(1, len(calls))
                    self.finish(capture)
                    self.assertEqual(error, verify_capture(capture.root)["receipts"][0]["error"])
                    with patch("monitor_lab.evidence.normalize", wraps=normalize) as parser:
                        replay = replay_capture(capture.root, self.root / (name + "-replay"))
                        self.assertEqual(0, replay["parsed_pages"])
                        self.assertEqual(0, replay["http_requests"])
                        parser.assert_not_called()


if __name__ == "__main__":
    unittest.main()

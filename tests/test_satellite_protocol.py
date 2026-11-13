from copy import deepcopy
import unittest

from sale_monitor.satellite.protocol import (
    ValidationError, ack_window_end, build_echo, build_manifest, build_ticket,
    sha256_bytes, stage_deadlines, stage_status, time_string, validate_echo,
    validate_manifest, validate_ticket,
)

COMMIT = "a" * 40
NONCE = "unique_nonce_123456"
NOW = 1_800_000_000
URL = "https://shop.tsukumo.co.jp/goods/1"


def ticket_fixture(**changes):
    fields = dict(run_id="run-1", phase="discovery", store="tsukumo",
                  issued_at=time_string(NOW), collect_until=time_string(NOW + 500),
                  accept_until=time_string(NOW + 710), seconds=590,
                  code_fingerprint="b" * 64, config_revision=1, state_sha=COMMIT,
                  input_sha256={"input/stores/tsukumo.json": "c" * 64,
                                "input/routes.json": "d" * 64, "input/config.json": "e" * 64},
                  cleared_refusals=[])
    fields.update(changes)
    return build_ticket(**fields)


def manifest_fixture(ticket=None, **changes):
    ticket = ticket or ticket_fixture()
    fields = dict(kind="result", ticket_commit=COMMIT, run_id=ticket["run_id"],
                  phase=ticket["phase"], store=ticket["store"], status="completed",
                  started_at=time_string(NOW), finished_at=time_string(NOW + 50),
                  code_sha=COMMIT, code_fingerprint=ticket["code_fingerprint"],
                  dependencies={"ok": True, "declared": {}, "installed": {}, "mismatches": []},
                  input_sha256=deepcopy(ticket["input_sha256"]),
                  config_sha256=ticket["input_sha256"]["input/config.json"],
                  output_sha256="f" * 64, active_refusals=[],
                  counters=dict.fromkeys(("requests", "ordinary_attempted", "ordinary_succeeded", "no_response", "refusal"), 0))
    fields.update(changes)
    return build_manifest(**fields)


def refusal_fixture():
    return dict(refusal_id="refusal-1", at=time_string(NOW), kind="http_403",
                url_sha256=sha256_bytes(URL.encode()), store="tsukumo")


class ProtocolTests(unittest.TestCase):
    def test_builders_roundtrip(self):
        ticket = ticket_fixture()
        self.assertEqual(validate_ticket(ticket), ticket)
        self.assertEqual(validate_manifest(manifest_fixture()), manifest_fixture())
        ack = manifest_fixture(kind="ack", nonce=NONCE, output_sha256=None, finished_at=None, status="acknowledged")
        self.assertEqual(validate_manifest(ack), ack)
        echo = build_echo(ticket_commit=COMMIT, nonce=NONCE, actions_time=time_string(NOW))
        self.assertEqual(validate_echo(echo), echo)

    def test_strict_fields_and_types(self):
        for key, value in (("extra", 1), ("seconds", True), ("store", "ark"),
                           ("run_id", "x" * 101), ("input_sha256", {"../x": "a" * 64}),
                           ("issued_at", "2026-01-01"), ("code_fingerprint", "A" * 64)):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                validate_ticket({**ticket_fixture(), key: value})
        with self.assertRaises(ValidationError):
            validate_echo({"ticket_commit": COMMIT, "nonce": NONCE, "actions_time": time_string(NOW), "extra": 1})
        for key, value in (("extra", 0), ("counters", {}), ("active_refusals", [{**refusal_fixture(), "extra": 1}]),
                           ("nonce", NONCE), ("finished_at", None), ("dependencies", {})):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                validate_manifest({**manifest_fixture(), key: value})

    def test_verification_requires_discovery(self):
        with self.assertRaises(ValidationError):
            validate_ticket({**ticket_fixture(), "phase": "verification"})
        hashes = {**ticket_fixture()["input_sha256"], "input/plan.json": "a" * 64}
        self.assertEqual(ticket_fixture(phase="verification", input_sha256=hashes,
                         accepted_discovery={"satellite_commit": COMMIT, "state_sha256": "c" * 64})["phase"], "verification")

    def test_raw_hash(self):
        self.assertNotEqual(sha256_bytes(b'{"a":1}'), sha256_bytes(b'{ "a": 1 }'))

    def test_deadline_arithmetic(self):
        self.assertEqual(stage_deadlines(2000, 100, 1000, 120, 300),
                         {"S": 1100, "collect_until": 1010, "accept_until": 1220})
        self.assertEqual(stage_deadlines(1000, 100, 1000, 120, 300),
                         {"S": 580, "collect_until": 490, "accept_until": 700})
        self.assertIsNone(stage_deadlines(610, 100, 1000, 120, 300))
        self.assertEqual(ack_window_end(100, 200), 200)
        self.assertEqual(ack_window_end(100, 500), 280)
        self.assertEqual(stage_status("satellite_declined", "refused"), "satellite_declined:refused")
        self.assertEqual(stage_status("completed"), "completed")

    def test_size_depth_and_nonfinite(self):
        for value in ("x" * 9000, float("nan"), [[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[1]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]):
            with self.assertRaises(ValidationError):
                validate_ticket({**ticket_fixture(), "extra": value})

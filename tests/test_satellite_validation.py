from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sale_monitor.http import Page
from sale_monitor.models import Offer, digest
from sale_monitor.runner import Collector
from sale_monitor.satellite.protocol import ValidationError, sha256_bytes, time_string
from sale_monitor.satellite.validation import (
    ALLOWED_STATE_KEYS, MAX_STATE_BYTES, build_effective_config, extract_refusals,
    validate_effective_config, validate_result,
)
from sale_monitor.storage import Store
from test_satellite_protocol import COMMIT, NOW, URL, manifest_fixture, refusal_fixture, ticket_fixture


def encode(value):
    return json.dumps(value, ensure_ascii=False).encode()


def config_fixture():
    return dict(schema_version=1, config_revision=1,
                stores={"tsukumo": dict(name="Tsukumo", adapter="html", seed_urls=[URL],
                                        product_patterns=[r"/goods/\d+"], sale_patterns=["/special/"],
                                        default_condition="new", pc_only=True, browser_fallback=False)},
                seller_aliases={}, route=dict(store="tsukumo", channel="direct", executor="satellite",
                    functions={"sale_discovery": {"status": "active"}, "product_verify": {"status": "active"},
                               "comparison_lookup": {"status": "paused_configuration"}}),
                hosts={"default": {"min_interval_seconds": 2}, "shop.tsukumo.co.jp": {}})


def offer_fixture(**changes):
    return Offer(**{**dict(store="tsukumo", product_id="1", url=URL, seller_id="tsukumo",
                          observed_at=time_string(NOW - 100), observed_run_id="discovery-previous",
                          price_yen=10000, shipping_yen=0, condition="new", verified=True), **changes})


class ValidationTests(unittest.TestCase):
    def setUp(self):
        offer = offer_fixture()
        self.before = dict(schema_version=1, store="tsukumo", run_id="discovery-previous", phase="discovery",
            queue={}, done=[], cycle_complete=True, offers={offer.key: offer.to_dict()},
            journal=[dict(observation_id=digest([offer.key, offer.observed_at, offer.to_dict()]),
                          run_id="discovery-previous", offer=offer.to_dict())])
        self.after = {**deepcopy(self.before), "run_id": "run-1", "phase": "verification"}

    def artifacts(self, before=None, after=None):
        before = self.before if before is None else before
        after = self.after if after is None else after
        input_bytes, output_bytes = encode(before), encode(after)
        hashes = {**ticket_fixture()["input_sha256"], "input/stores/tsukumo.json": sha256_bytes(input_bytes), "input/plan.json": "a" * 64}
        ticket = ticket_fixture(phase="verification", input_sha256=hashes,
            accepted_discovery={"satellite_commit": "d" * 40, "state_sha256": sha256_bytes(input_bytes)})
        manifest = manifest_fixture(ticket, output_sha256=sha256_bytes(output_bytes))
        return input_bytes, output_bytes, ticket, manifest

    def validate(self, before=None, after=None, **kwargs):
        return validate_result(*self.artifacts(before, after), received_at=NOW + 100, ticket_commit=COMMIT, **kwargs)

    def add_observation(self, **changes):
        offer = offer_fixture(observed_at=time_string(NOW + 10), observed_run_id="run-1", **changes)
        row = offer.to_dict()
        self.after["offers"][offer.key] = row
        self.after["journal"].append(dict(observation_id=digest([offer.key, offer.observed_at, row]), run_id="run-1", offer=deepcopy(row)))
        return offer

    def test_config_shape_and_floor(self):
        config = config_fixture()
        self.assertEqual(validate_effective_config(config, "tsukumo"), 5)
        sources = {"schema_version": 1, "stores": {**config["stores"], "ark": {}}, "seller_aliases": {}}
        routes = {"config_revision": 1, "hosts": config["hosts"], "routes": {"tsukumo:direct": config["route"]}}
        effective = build_effective_config(sources, routes, "tsukumo")
        self.assertEqual(effective, config)
        effective["hosts"]["shop.tsukumo.co.jp"]["min_interval_seconds"] = 9
        self.assertEqual(validate_effective_config(effective, "tsukumo"), 9)
        self.assertEqual(sources["stores"], {**config["stores"], "ark": {}})

    def test_config_unknown_keys_browser_url_and_store(self):
        mutations = [lambda c: c.update(extra=1), lambda c: c["stores"]["tsukumo"].update(extra=True),
            lambda c: c["stores"]["tsukumo"].update(browser_fallback=True),
            lambda c: c["stores"]["tsukumo"].update(seed_urls=["https://example.com/"]),
            lambda c: c["route"].update(extra=True),
            lambda c: c["hosts"]["default"].update(extra=True),
            lambda c: c["route"]["functions"]["sale_discovery"].update(extra=True),
            lambda c: c["route"]["functions"]["comparison_lookup"].update(status="active"),
            lambda c: c["seller_aliases"].update(alias="ark")]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                config = config_fixture()
                mutate(config)
                with self.assertRaises(ValidationError):
                    validate_effective_config(config, "tsukumo")
        with self.assertRaises(ValidationError):
            validate_effective_config(config_fixture(), "ark")

    def test_normal_verification_includes_discovery_observations(self):
        self.assertIsNone(self.validate())
        self.add_observation(price_yen=9000)
        self.assertIsNone(self.validate())

    def test_real_collector_record_and_failure_mark_accepted(self):
        with tempfile.TemporaryDirectory() as root:
            disk = Store(Path(root))
            disk.save("stores/tsukumo.json", self.before)
            with patch("sale_monitor.runner.iso", return_value=time_string(NOW + 1)):
                collector = Collector(Path(root), "tsukumo", config_fixture(), "run-1", phase="verification")
                offer = offer_fixture(observed_at=time_string(NOW + 10), price_yen=8000)
                collector.record(offer)
                collector.state["offers"][offer.key]["issues"] = ["latest_fetch_failed"]
                collector.state["verification"]["results"]["task"] = dict(type="product", url=URL,
                    result="failed", reason="TimeoutError")
                self.assertIsNone(self.validate(after=collector.state))
                self.assertTrue(set(collector.state) <= ALLOWED_STATE_KEYS)

    def test_new_observation_then_same_canonical_url_failure(self):
        offer = self.add_observation(price_yen=9000)
        self.after["offers"][offer.key]["issues"] = ["latest_fetch_failed"]
        self.after["verification"] = dict(run_id="run-1", results={"task": dict(url=URL + "#fragment", type="product", result="failed", reason="TimeoutError")})
        self.assertIsNone(self.validate())

    def test_old_offer_failure_mark_requires_this_run_record(self):
        key = next(iter(self.after["offers"]))
        self.after["offers"][key]["issues"] = ["latest_fetch_failed"]
        with self.assertRaisesRegex(ValidationError, "offer_tampering"):
            self.validate()
        self.after["queue"]["task"] = dict(type="product", url=URL, last_attempt_run_id="old", last_error="TimeoutError")
        with self.assertRaises(ValidationError):
            self.validate()
        self.after["queue"]["task"]["last_attempt_run_id"] = "run-1"
        self.assertIsNone(self.validate())

    def test_old_offer_price_seller_run_time_and_deletion_rejected(self):
        for key, value in (("price_yen", 10), ("seller_id", "ark"), ("observed_run_id", "run-1"),
                           ("observed_at", time_string(NOW + 10)), ("title", "changed")):
            with self.subTest(key=key):
                after = deepcopy(self.after)
                next(iter(after["offers"].values()))[key] = value
                with self.assertRaises(ValidationError):
                    self.validate(after=after)
        after = deepcopy(self.after)
        after["offers"] = {}
        with self.assertRaisesRegex(ValidationError, "deleted_offer"):
            self.validate(after=after)

    def test_journal_prefix_tampering_or_shortening(self):
        for mutate in (lambda rows: rows.clear(), lambda rows: rows[0].update(run_id="changed")):
            after = deepcopy(self.after)
            mutate(after["journal"])
            with self.assertRaisesRegex(ValidationError, "journal_prefix"):
                self.validate(after=after)

    def test_latest_journal_must_back_offer(self):
        first = self.add_observation(price_yen=9000).to_dict()
        self.add_observation(price_yen=8000)
        key = next(iter(self.after["offers"]))
        self.after["offers"][key] = first
        with self.assertRaisesRegex(ValidationError, "offer_tampering"):
            self.validate()

    def test_observation_id_run_store_seller_and_time(self):
        for field, value in (("observed_at", time_string(NOW - 1)), ("observed_at", time_string(NOW + 101)),
                              ("observed_run_id", "old"), ("store", "ark"), ("seller_id", "ark")):
            with self.subTest(field=field):
                self.after = {**deepcopy(self.before), "run_id": "run-1", "phase": "verification"}
                self.add_observation()
                row = self.after["journal"][-1]
                row["offer"][field] = value
                with self.assertRaises(ValidationError):
                    self.validate()
        self.after = {**deepcopy(self.before), "run_id": "run-1", "phase": "verification"}
        self.add_observation()
        for field, value in (("observation_id", "x" * 64), ("run_id", "old")):
            after = deepcopy(self.after)
            after["journal"][-1][field] = value
            with self.assertRaises(ValidationError):
                self.validate(after=after)

    def test_foreign_urls_in_keys_nested_evidence_or_queue_rejected(self):
        for extra in ({"queue": {"x": {"url": "https://example.com/"}}},
                      {"comparison_searches": {"https://example.com/": {}}},
                      {"errors": [{"nested": ["see https://example.com/a"]}]}):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValidationError, "foreign_url"):
                self.validate(after={**self.after, **extra})

    def test_unknown_top_level_type_schema_and_size(self):
        for extra in ({"mystery": 1}, {"offers": []}, {"request_count": True},
                      {"schema_version": 2}, {"done": ["x"] * 100001}):
            with self.subTest(extra=list(extra)), self.assertRaises(ValidationError):
                self.validate(after={**self.after, **extra})
        args = list(self.artifacts())
        args[1] = b" " * (MAX_STATE_BYTES + 1)
        args[3]["output_sha256"] = sha256_bytes(args[1])
        with self.assertRaisesRegex(ValidationError, "state_size"):
            validate_result(*args, NOW + 100, ticket_commit=COMMIT)

    def test_manifest_correspondence_and_hashes(self):
        for key, value in (("ticket_commit", "d" * 40), ("run_id", "other"), ("phase", "discovery"),
                           ("code_fingerprint", "0" * 64), ("config_sha256", "0" * 64), ("output_sha256", "0" * 64)):
            with self.subTest(key=key):
                args = list(self.artifacts())
                args[3][key] = value
                with self.assertRaises(ValidationError):
                    validate_result(*args, NOW + 100, ticket_commit=COMMIT)
        args = list(self.artifacts())
        args[0] += b"\n"
        with self.assertRaisesRegex(ValidationError, "input_hash"):
            validate_result(*args, NOW + 100, ticket_commit=COMMIT)
        with self.assertRaisesRegex(ValidationError, "deadline"):
            validate_result(*self.artifacts(), NOW + 711, ticket_commit=COMMIT)
        self.assertIsNone(validate_result(*self.artifacts(), NOW + 710, ticket_commit=COMMIT))

    def test_discovery_provenance_and_raw_bytes(self):
        args = list(self.artifacts())
        args[2]["accepted_discovery"]["state_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValidationError, "discovery_hash"):
            validate_result(*args, NOW + 100, ticket_commit=COMMIT)
        args = list(self.artifacts())
        args[1] += b"\r\n"
        args[3]["output_sha256"] = sha256_bytes(args[1])
        self.assertIsNone(validate_result(*args, NOW + 100, ticket_commit=COMMIT))

    def test_extract_refusals_even_when_body_invalid(self):
        args = list(self.artifacts())
        args[1] = b"not json"
        args[3]["active_refusals"] = [refusal_fixture()]
        self.assertEqual(extract_refusals(args[3]), [refusal_fixture()])
        with self.assertRaises(ValidationError):
            validate_result(*args, NOW + 100, ticket_commit=COMMIT)
        self.assertEqual(extract_refusals({"active_refusals": [refusal_fixture()], "unrelated": object()}), [refusal_fixture()])
        for value in ([{**refusal_fixture(), "extra": 1}], [{**refusal_fixture(), "url_sha256": "bad"}], [refusal_fixture()] * 2):
            with self.assertRaises(ValidationError):
                extract_refusals({"active_refusals": value})

    def test_duplicate_json_keys_rejected(self):
        args = list(self.artifacts())
        args[1] = args[1][:-1] + b', "store": "tsukumo"}'
        args[3]["output_sha256"] = sha256_bytes(args[1])
        with self.assertRaisesRegex(ValidationError, "duplicate_key"):
            validate_result(*args, NOW + 100, ticket_commit=COMMIT)

    def test_config_filters_shared_aliases_and_nested_unknowns(self):
        config = config_fixture()
        sources = dict(schema_version=1, stores=config["stores"],
                       seller_aliases={"tsukumo:shop": "tsukumo", "ark:shop": "ark"})
        routes = dict(config_revision=1, hosts=config["hosts"], routes={"tsukumo:direct": config["route"]})
        result = build_effective_config(sources, routes, "tsukumo")
        self.assertEqual(result["seller_aliases"], {"tsukumo:shop": "tsukumo"})
        config["route"]["functions"]["sale_discovery"]["evidence"] = {"extra": 1}
        with self.assertRaises(ValidationError):
            validate_effective_config(config, "tsukumo")

    def test_malformed_nested_state_is_validation_error(self):
        for extra in ({"verification": {"results": []}}, {"queue": {"x": []}},
                      {"errors": [{"url": "javascript:bad"}]}, {"errors": [{"url": "/relative"}]}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                self.validate(after={**self.after, **extra})

    def test_ticket_context_and_received_time_are_trusted_inputs(self):
        with self.assertRaisesRegex(ValidationError, "ticket_commit_required"):
            validate_result(*self.artifacts(), NOW + 100)
        for received in (float("nan"), float("inf"), NOW - 1, True):
            with self.subTest(received=received), self.assertRaises(ValidationError):
                validate_result(*self.artifacts(), received, ticket_commit=COMMIT)

    def test_new_offer_field_type_is_checked(self):
        self.add_observation()
        for field, value in (("verified", 1), ("channel", []), ("evidence", "bad")):
            after = deepcopy(self.after)
            after["journal"][-1]["offer"][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.validate(after=after)

    def test_json_boolean_integer_equality_cannot_hide_tampering(self):
        after = deepcopy(self.after)
        after["journal"][0]["offer"]["verified"] = 1
        with self.assertRaisesRegex(ValidationError, "journal_prefix"):
            self.validate(after=after)
        key = next(iter(self.before["offers"]))
        self.before["offers"][key]["coupon"] = {"verified": True}
        self.after = {**deepcopy(self.before), "run_id": "run-1", "phase": "verification"}
        self.after["offers"][key]["coupon"]["verified"] = 1
        with self.assertRaisesRegex(ValidationError, "offer_tampering"):
            self.validate()

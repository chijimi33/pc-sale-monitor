from copy import deepcopy
from datetime import datetime, timedelta
import json
import unittest

from sale_monitor.models import UTC, Offer, iso
from sale_monitor.planner import plan, update_tracking

NOW = datetime(2026, 9, 14, tzinfo=UTC)
RUN = "run1"


def offer(store="ark", price=9000, **kwargs):
    data = dict(store=store, product_id="one", url=f"https://{store}.example/item/one",
                model="MODEL-A", brand="Brand", seller_id=store, condition="new",
                price_yen=price, shipping_yen=0, stock="in_stock", observed_at=iso(NOW),
                observed_run_id=RUN, verified=True, evidence=[{"url": f"https://{store}.example/item/one"}])
    data.update(kwargs)
    return Offer(**data)


def build(candidates=None, peers=None, **kwargs):
    candidates = [offer()] if candidates is None else candidates
    peers = [offer("bic", 10000)] if peers is None else peers
    known = {}
    for other in peers:
        known.setdefault(other.identity, []).append(other)
    args = dict(candidates=candidates, current=candidates, known=known, history_by_identity={},
                scope_stores=["ark", "bic", "sofmap", "tsukumo"], lookup_stores=[],
                budgets={"ark": 10, "bic": 10, "sofmap": 10, "tsukumo": 10}, tracking={},
                changes={}, now=NOW, run_id=RUN, scope_version=7)
    args.update(kwargs)
    return plan(**args)


class PlannerTests(unittest.TestCase):
    def test_unverifiable_peer_blocks_whole_bundle_without_reserving(self):
        result = build(peers=[offer("bic"), offer("sofmap")], verifiable_stores=["ark", "bic"])
        row = result["candidates"][0]
        self.assertEqual(row["status"], "comparator_unverifiable")
        self.assertEqual(row["reasons"], ["comparator_unverifiable"])
        self.assertEqual(next(i["state"] for i in row["bundle"] if i["store"] == "sofmap"), "unverifiable")
        self.assertEqual(result["confirm"]["bic"], [])
        self.assertNotIn("sofmap", result["summary"]["confirm_budget"])
        self.assertNotIn("sofmap", result["explore"])

    def test_already_observed_unverifiable_peer_does_not_block(self):
        peer = offer("sofmap")
        result = build(peers=[offer("bic"), peer], current=[offer(), peer], verifiable_stores=["ark", "bic"])
        self.assertEqual(result["candidates"][0]["status"], "planned")
        self.assertEqual(len(result["confirm"]["bic"]), 1)

    def test_unverified_urls_are_confirmed_as_complete_bundle(self):
        candidate = offer()
        tracking = {candidate.identity: {"bic": {"unverified_urls": ["https://bic.example/new"]}}}
        before = deepcopy(tracking)
        for budget, status in ((0, "skipped_budget"), (2, "planned")):
            with self.subTest(budget=budget):
                result = build(peers=[], tracking=tracking, budgets={"bic": budget})
                self.assertEqual(result["candidates"][0]["status"], status)
                self.assertEqual(result["candidates"][0]["bundle"], [{"store": "bic", "offer_key": None,
                    "url": "https://bic.example/new", "state": "to_verify"}])
                if budget:
                    self.assertIsNone(result["confirm"]["bic"][0]["offer_key"])
        self.assertEqual(tracking, before)

    def test_unverified_url_backoff_scope_and_unverifiable(self):
        candidate = offer()
        for values, verifiable, status in (({}, ["ark"], "comparator_unverifiable"),
                ({"consecutive_failures": 1, "last_attempt_at": iso(NOW)}, ["ark", "bic"], "comparator_backoff")):
            with self.subTest(status=status):
                tracking = {candidate.identity: {"bic": {**values, "unverified_urls": ["https://bic.example/new"]}}}
                result = build(peers=[], tracking=tracking, verifiable_stores=verifiable)
                self.assertEqual(result["candidates"][0]["status"], status)
                self.assertEqual(sum(result["summary"]["reserved_requests"].values()), 0)
        tracking = {candidate.identity: {s: {"unverified_urls": ["https://example.test/new"]} for s in ("ark", "yahoo")}}
        self.assertEqual(build(peers=[], tracking=tracking)["candidates"][0]["status"], "no_known_comparator")

    def test_unverified_url_already_observed_and_duplicate_known_url(self):
        candidate, peer = offer(), offer("bic")
        tracking = {candidate.identity: {"bic": {"unverified_urls": [peer.url, peer.url]}}}
        result = build(peers=[peer], tracking=tracking)
        self.assertEqual(len(result["candidates"][0]["bundle"]), 1)
        self.assertEqual(len(result["confirm"]["bic"]), 1)
        result = build(peers=[], current=[candidate, peer], tracking=tracking, verifiable_stores=["ark"])
        self.assertEqual(result["candidates"][0]["status"], "already_confirmed")

    def test_not_met_and_expensive_candidates_are_planned_after_others(self):
        candidates = [offer(price=11000, product_id="expensive"), offer(product_id="new")]
        rows = [{"offer": offer(price=p, observed_at=iso(NOW - timedelta(days=d))).to_dict()}
                for d, p in [(31, 11000), (20, 11000), (10, 9000)]]
        result = build(candidates, history_by_identity={candidates[0].identity: rows})
        self.assertEqual([r["offer_key"] for r in result["candidates"]], [candidates[1].key, candidates[0].key])
        self.assertEqual([r["status"] for r in result["candidates"]], ["planned", "planned"])
        self.assertEqual([r["history_status"] for r in result["candidates"]], ["met", "not_met"])
        self.assertEqual(result["candidates"][1]["priority"][2], 3)

    def test_change_priority(self):
        candidates = [offer(product_id=str(i)) for i in range(4)]
        changes = dict(zip([o.key for o in candidates], ["unchanged", "restocked", "price_down", "new"]))
        result = build(candidates, changes=changes)
        self.assertEqual([r["offer_key"] for r in result["candidates"]], [o.key for o in reversed(candidates)])

    def test_payment_priority_and_exact_threshold(self):
        candidates = [offer(price=p, product_id=str(p)) for p in (11000, 10000, 9001, 9000)]
        result = build(candidates)
        self.assertEqual([r["priority"][2] for r in result["candidates"]], [0, 1, 1, 3])
        self.assertEqual(result["candidates"][0]["offer_key"], candidates[-1].key)
        self.assertTrue(all(r["status"] == "planned" for r in result["candidates"]))

    def test_unknown_saved_payment_is_only_a_priority(self):
        result = build(peers=[offer("bic", shipping_yen=None)])
        self.assertEqual(result["candidates"][0]["priority"][2], 2)
        self.assertEqual(result["candidates"][0]["status"], "planned")

    def test_oldest_success_priority_with_never_checked_first(self):
        candidates = [offer(model="MODEL-" + str(i), product_id=str(i)) for i in range(3)]
        peers = [offer("bic", 10000, model=o.model, product_id=o.product_id) for o in candidates]
        tracking = {candidates[i].identity: {"bic": {"last_success_at": iso(NOW - timedelta(days=i + 1))}} for i in (0, 1)}
        result = build(candidates, peers, tracking=tracking)
        self.assertEqual([r["offer_key"] for r in result["candidates"]], [candidates[i].key for i in (2, 1, 0)])

    def test_complete_bundle_or_no_reservation(self):
        candidate = offer()
        peers = [offer("bic"), offer("sofmap")]
        result = build([candidate], peers, budgets={"bic": 10, "sofmap": 0})
        self.assertEqual(result["candidates"][0]["status"], "skipped_budget")
        self.assertEqual(result["confirm"]["bic"], [])
        self.assertEqual(sum(result["summary"]["reserved_requests"].values()), 0)

    def test_skip_large_bundle_and_continue_with_smaller_bundle(self):
        first = offer(model="FIRST", product_id="first")
        second = offer(model="SECOND", product_id="second")
        peers = [offer("bic", model="FIRST"), offer("sofmap", model="FIRST"), offer("bic", model="SECOND")]
        result = build([first, second], peers, budgets={"bic": 2, "sofmap": 0}, changes={first.key: "new"})
        self.assertEqual([r["status"] for r in result["candidates"]], ["skipped_budget", "planned"])
        self.assertEqual([r["for_candidates"] for r in result["confirm"]["bic"]], [[second.key]])

    def test_shared_url_costs_one_request(self):
        first, second = offer(), offer("sofmap")
        result = build([first, second], budgets={"bic": 2})
        self.assertEqual([r["status"] for r in result["candidates"]], ["planned", "planned"])
        self.assertEqual(result["summary"]["reserved_requests"]["bic"], 1)
        self.assertEqual(set(result["confirm"]["bic"][0]["for_candidates"]), {first.key, second.key})

    def test_same_url_under_different_offer_keys_still_costs_one_request(self):
        peer = offer("bic")
        duplicate = offer("bic", product_id="second", url=peer.url)
        result = build(peers=[peer, duplicate], budgets={"bic": 2})
        self.assertEqual(len(result["candidates"][0]["bundle"]), 2)
        self.assertEqual(len(result["confirm"]["bic"]), 1)

    def test_confirmed_this_run_costs_nothing(self):
        candidate, peer = offer(), offer("bic")
        result = build([candidate], [peer], current=[candidate, peer], budgets={"bic": 0})
        self.assertEqual(result["candidates"][0]["status"], "already_confirmed")
        self.assertEqual(result["candidates"][0]["bundle"][0]["state"], "confirmed_this_run")
        self.assertEqual(result["confirm"]["bic"], [])

    def test_confirmed_members_of_partial_bundle_are_not_reserved(self):
        first, second = offer("bic"), offer("sofmap")
        result = build(peers=[first, second], current=[offer(), first])
        self.assertEqual(result["candidates"][0]["status"], "planned")
        self.assertEqual(result["confirm"]["bic"], [])
        self.assertEqual(len(result["confirm"]["sofmap"]), 1)

    def test_old_run_does_not_confirm_comparator(self):
        peer = offer("bic", observed_run_id="old")
        result = build(peers=[peer], current=[offer(), peer])
        self.assertEqual(result["candidates"][0]["status"], "planned")

    def test_bundle_excludes_outside_scope_same_seller_and_nononline(self):
        peers = [offer("bic"), offer("sofmap", seller_id="ark"), offer("tsukumo", channel="store_flyer"),
                 offer("ark", model="DIFFERENT"), offer("ark", variant="two")]
        result = build(peers=peers, scope_stores=["ark", "sofmap", "tsukumo"])
        self.assertEqual(result["candidates"][0]["status"], "no_known_comparator")
        self.assertEqual(result["candidates"][0]["bundle"], [])

    def test_duplicate_store_and_offer_key_are_one_bundle_member(self):
        peer = offer("bic")
        result = build(peers=[peer, deepcopy(peer)])
        self.assertEqual(len(result["candidates"][0]["bundle"]), 1)

    def test_backoff_blocks_entire_bundle(self):
        candidate = offer()
        tracking = {candidate.identity: {"bic": {"consecutive_failures": 2, "last_attempt_at": iso(NOW - timedelta(hours=7))}}}
        result = build([candidate], [offer("bic"), offer("sofmap")], tracking=tracking)
        self.assertEqual(result["candidates"][0]["status"], "comparator_backoff")
        self.assertEqual(result["candidates"][0]["bundle"][0]["state"], "backoff")
        self.assertEqual(sum(result["summary"]["reserved_requests"].values()), 0)

    def test_backoff_boundaries_and_seven_day_cap(self):
        candidate = offer()
        for failures, elapsed, status in ((1, 3.99, "comparator_backoff"), (1, 4, "planned"),
                                           (2, 8, "planned"), (1000, 167, "comparator_backoff"),
                                           (1000, 168, "planned"), (1, None, "planned")):
            with self.subTest(failures=failures, elapsed=elapsed):
                tracking = {candidate.identity: {"bic": {"consecutive_failures": failures,
                            "last_attempt_at": iso(NOW - timedelta(hours=elapsed)) if elapsed is not None else None}}}
                self.assertEqual(build(tracking=tracking)["candidates"][0]["status"], status)

    def test_confirmed_member_ignores_backoff(self):
        candidate, peer = offer(), offer("bic")
        tracking = {candidate.identity: {"bic": {"consecutive_failures": 1, "last_attempt_at": iso(NOW)}}}
        self.assertEqual(build(current=[candidate, peer], tracking=tracking)["candidates"][0]["status"], "already_confirmed")

    def test_invalid_candidates_remain_visible(self):
        candidates = [offer(shipping_yen=None), offer("sofmap", brand=None, model=None)]
        result = build(candidates, lookup_stores=["bic"])
        self.assertEqual(len(result["candidates"]), 2)
        for row in result["candidates"]:
            self.assertEqual(row["status"], "candidate_ineligible")
            self.assertEqual(row["history_status"], "insufficient")
            self.assertTrue(row["reasons"])
        self.assertEqual(sum(result["summary"]["reserved_requests"].values()), 0)
        self.assertEqual(sum(result["summary"]["explore_requests"].values()), 0)

    def test_exploration_budget_and_query(self):
        candidates = [offer(product_id=str(i), model="MODEL-" + str(i)) for i in range(3)]
        result = build(candidates, [], lookup_stores=["bic"], budgets={"bic": 21})
        self.assertEqual(result["summary"]["confirm_budget"]["bic"], 16)
        self.assertEqual(result["summary"]["explore_budget"]["bic"], 5)
        self.assertEqual(result["summary"]["explore_requests"]["bic"], 4)
        self.assertEqual(len(result["explore"]["bic"]), 2)
        self.assertTrue(all(r["query"].startswith("Brand MODEL-") for r in result["explore"]["bic"]))

    def test_exploration_uses_jan_and_shares_identity_search(self):
        candidates = [offer(jan="4526541047763"), offer("sofmap", jan="4526541047763")]
        result = build(candidates, [], lookup_stores=["bic"], budgets={"bic": 10})
        self.assertEqual(result["explore"]["bic"], [{"query": "4526541047763", "identity": candidates[0].identity,
                                                     "for_candidates": [r["offer_key"] for r in result["candidates"]]}])
        self.assertEqual(result["summary"]["explore_requests"]["bic"], 2)

    def test_exploration_excludes_own_store_inactive_lookup_and_recent_search(self):
        candidate = offer()
        for hours, count in ((23, 0), (24, 0), (24.01, 1)):
            with self.subTest(hours=hours):
                tracking = {candidate.identity: {"bic": {"last_search_at": iso(NOW - timedelta(hours=hours))}}}
                result = build([candidate], [], lookup_stores=["ark", "bic", "yodobashi"], tracking=tracking)
                self.assertEqual(len(result["explore"]["bic"]), count)
                self.assertEqual(result["explore"]["ark"], [])
                self.assertNotIn("yodobashi", result["explore"])
                self.assertEqual(result["explore"]["sofmap"], [])

    def test_known_comparator_candidate_does_not_explore(self):
        result = build(lookup_stores=["sofmap"])
        self.assertEqual(result["explore"]["sofmap"], [])

    def test_all_budgets_are_respected_for_many_candidates(self):
        candidates = [offer(model="MODEL-" + str(i), product_id=str(i)) for i in range(20)]
        peers = [offer(s, model=o.model, product_id=o.product_id) for o in candidates[:10] for s in ("bic", "sofmap")]
        for share in (0, 0.2, 0.5, 1):
            with self.subTest(share=share):
                result = build(candidates, peers, budgets={"bic": 7, "sofmap": 9},
                               lookup_stores=["bic", "sofmap"], exploration_share=share)
                summary = result["summary"]
                for store in summary["confirm_budget"]:
                    self.assertLessEqual(summary["reserved_requests"][store], summary["confirm_budget"][store])
                    self.assertLessEqual(summary["explore_requests"][store], summary["explore_budget"][store])
                    self.assertEqual(summary["reserved_requests"][store], len(result["confirm"][store]))
                    self.assertEqual(summary["explore_requests"][store], 2 * len(result["explore"][store]))
                self.assertEqual(sum(summary["by_status"].values()), len(candidates))

    def test_same_inputs_are_deterministic_and_are_not_mutated(self):
        candidates = [offer(), offer("sofmap")]
        peers = [offer("bic"), offer("tsukumo")]
        inputs = deepcopy((candidates, peers))
        first = build(candidates, peers)
        self.assertEqual(json.dumps(first), json.dumps(build(candidates, peers)))
        self.assertEqual(json.dumps(first), json.dumps(build(list(reversed(candidates)), list(reversed(peers)))))
        self.assertEqual((candidates, peers), inputs)
        self.assertEqual((first["run_id"], first["scope_version"], first["generated_at"]), (RUN, 7, iso(NOW)))

    def test_empty_plan(self):
        result = build([], [], budgets={})
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["summary"]["candidates"], 0)

    def test_invalid_budgets_and_share(self):
        for kwargs in ({"budgets": {"bic": -1}}, {"budgets": {"bic": 1.5}},
                       {"exploration_share": -0.1}, {"exploration_share": 1.1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                build(**kwargs)


class TrackingTests(unittest.TestCase):
    def track_observations(self, observations, *, requested=None, receipts=None):
        candidate = offer()
        url = requested or "https://bic.example/found"
        tracking = {candidate.identity: {"bic": {"unverified_urls": [url]}}}
        discoveries = {"bic": {"verified": receipts or []}}
        before = deepcopy((tracking, observations, discoveries))
        result = update_tracking(tracking, build(peers=[]), [candidate, *observations], {}, {}, NOW, discoveries)
        self.assertEqual((tracking, observations, discoveries), before)
        return result[candidate.identity]["bic"]

    def test_all_observations_at_shared_url_are_considered_in_any_order(self):
        url = "https://bic.example/found"
        unresolved = offer("bic", url=url, shipping_yen=None)
        mismatch = offer("bic", url=url, model="MODEL-B", product_id="other")
        for observations in ([unresolved, mismatch], [mismatch, unresolved]):
            with self.subTest(order=[o.model for o in observations]):
                self.assertEqual(self.track_observations(observations)["unverified_urls"], [url])
        for observations, reason in (([mismatch, mismatch], "identity_mismatch"),
                                     ([offer("bic", url=url)] * 2, "became_known_comparator")):
            with self.subTest(reason=reason):
                values = self.track_observations(observations)
                self.assertEqual(values["unverified_urls"], [])
                self.assertEqual(values["settled_urls"][0]["reason"], reason)

    def test_unknown_or_different_identity_kind_in_any_observation_prevents_release(self):
        url = "https://bic.example/found"
        for extra in (offer("bic", url=url, model=None), offer("bic", url=url, jan="4943765048398")):
            for found in (offer("bic", url=url), offer("bic", url=url, model="MODEL-B"),
                          offer("bic", url=url, stock="out_of_stock")):
                with self.subTest(extra=extra.identity, found=found.stock):
                    values = self.track_observations([found, extra])
                    self.assertEqual(values["unverified_urls"], [url])
                    self.assertEqual(values["ended_urls"], {})

    def test_every_matching_observation_must_meet_the_same_release_condition(self):
        url = "https://bic.example/found"
        good = offer("bic", url=url)
        ended = offer("bic", url=url, stock="out_of_stock")
        for observations in ([good, offer("bic", url=url, shipping_yen=None)],
                             [good, offer("bic", url=url, variant="bundle")],
                             [ended, offer("bic", url=url, stock="out_of_stock", verified=False)],
                             [ended, offer("bic", url=url, stock="out_of_stock", issues=["unknown"])],
                             [good, ended]):
            with self.subTest(observations=observations):
                values = self.track_observations(observations)
                self.assertEqual(values["unverified_urls"], [url])
                self.assertEqual(values["ended_urls"], {})
        values = self.track_observations([ended, ended])
        self.assertEqual(values["unverified_urls"], [url])
        self.assertEqual(values["ended_urls"], {url: RUN})

    def test_redirect_receipts_resolve_only_current_observations_for_that_identity_and_store(self):
        requested = "https://bic.example/requested"
        for changes, reason in (({}, "became_known_comparator"), ({"model": "MODEL-B"}, "identity_mismatch"),
                                ({"stock": "out_of_stock"}, "ended_this_run")):
            for valid in (True, False):
                with self.subTest(changes=changes, valid=valid):
                    found = offer("bic", url="https://bic.example/redirected", observed_run_id=RUN if valid else "old", **changes)
                    receipt = {"identity": offer().identity, "url": requested, "offer_key": found.key}
                    values = self.track_observations([found], requested=requested, receipts=[receipt])
                    if not valid:
                        self.assertEqual(values["unverified_urls"], [requested])
                        self.assertEqual(values["ended_urls"], {})
                    elif reason == "ended_this_run":
                        self.assertEqual(values["ended_urls"], {requested: RUN})
                    else:
                        self.assertEqual(values["unverified_urls"], [])
                        self.assertEqual(values["settled_urls"][0]["reason"], reason)
        found = offer("bic", url="https://bic.example/redirected")
        for receipt in ({"identity": offer().identity, "url": requested},
                        {"identity": offer(model="MODEL-B").identity, "url": requested, "offer_key": found.key},
                        {"identity": offer().identity, "url": requested, "offer_key": "missing"}):
            with self.subTest(receipt=receipt):
                values = self.track_observations([found], requested=requested, receipts=[receipt])
                self.assertEqual(values["unverified_urls"], [requested])

    def test_url_and_redirect_observations_are_combined_without_losing_unresolved_evidence(self):
        url = "https://bic.example/found"
        found = offer("bic", url="https://bic.example/redirected", product_id="redirected")
        unresolved = offer("bic", url=url, shipping_yen=None)
        receipt = {"identity": offer().identity, "url": url, "offer_key": found.key}
        values = self.track_observations([unresolved, found], receipts=[receipt])
        self.assertEqual(values["unverified_urls"], [url])

    def test_discoveries_merge_without_mutating_inputs_and_fetching_alone_settles_nothing(self):
        candidate = offer()
        tracking = {candidate.identity: {"bic": {"unverified_urls": ["done", "pending"]},
                                       "sofmap": {"unverified_urls": ["done"]}}}
        discoveries = {"bic": {"unverified": [{"identity": candidate.identity, "url": url} for url in ("new", "new")],
                               "verified": [{"identity": candidate.identity, "url": "done"}]}}
        before = deepcopy((tracking, discoveries))
        result = update_tracking(tracking, build(peers=[]), [candidate], {}, {}, NOW, discoveries)
        # "done" was fetched, but no observation settles what it is.
        self.assertEqual(result[candidate.identity]["bic"]["unverified_urls"], ["done", "new", "pending"])
        self.assertEqual(result[candidate.identity]["sofmap"]["unverified_urls"], ["done"])
        self.assertEqual((tracking, discoveries), before)

    def settle(self, found, candidate=None, url="https://bic.example/found"):
        candidate = candidate or offer()
        tracking = {candidate.identity: {"bic": {"unverified_urls": [url]}}}
        current = [candidate] + ([found] if found is not None else [])
        result = update_tracking(tracking, build(peers=[], tracking=tracking), current, {}, {}, NOW,
                                 {"bic": {"verified": [{"identity": candidate.identity, "url": url}]}})
        return result[candidate.identity]["bic"]

    def test_matching_usable_same_product_becomes_a_known_comparator(self):
        url = "https://bic.example/found"
        values = self.settle(offer("bic", 10000, product_id="found", url=url))
        self.assertEqual(values["unverified_urls"], [])
        self.assertEqual(values["settled_urls"][0]["reason"], "became_known_comparator")

    def test_same_kind_of_identifier_with_another_value_is_a_confirmed_mismatch(self):
        url = "https://bic.example/found"
        values = self.settle(offer("bic", 10000, product_id="found", url=url, model="MODEL-B"))
        self.assertEqual(values["unverified_urls"], [])
        self.assertEqual(values["settled_urls"][0]["reason"], "identity_mismatch")

    def test_another_kind_of_identifier_or_none_stays_open(self):
        url = "https://bic.example/found"
        candidate = offer(jan="4943765048398")
        for found in (offer("bic", 10000, product_id="found", url=url),            # model only
                      offer("bic", 10000, product_id="found", url=url, model=None)):  # unidentified
            with self.subTest(identity=found.identity):
                self.assertEqual(self.settle(found, candidate)["unverified_urls"], [url])

    def test_matching_identifier_with_unknown_configuration_stays_open(self):
        url = "https://bic.example/found"
        candidate = offer(variant="2枚組")
        found = offer("bic", 10000, product_id="found", url=url)
        self.assertEqual(self.settle(found, candidate)["unverified_urls"], [url])

    def test_matching_offer_with_unknown_conditions_stays_open(self):
        url = "https://bic.example/found"
        found = offer("bic", 10000, product_id="found", url=url, shipping_yen=None)
        self.assertEqual(self.settle(found)["unverified_urls"], [url])

    def test_verified_sell_out_answers_this_run_but_keeps_the_obligation(self):
        url = "https://bic.example/found"
        found = offer("bic", 10000, product_id="found", url=url, stock="out_of_stock")
        values = self.settle(found)
        self.assertEqual(values["unverified_urls"], [url])
        self.assertEqual(values["ended_urls"], {url: RUN})
        self.assertNotIn("settled_urls", values)

    def test_sell_out_mark_expires_when_the_next_run_does_not_verify_it(self):
        url = "https://bic.example/found"
        candidate = offer(observed_run_id="run2")
        tracking = {candidate.identity: {"bic": {"unverified_urls": [url], "ended_urls": {url: RUN}}}}
        planned = build([candidate], [], tracking=tracking, run_id="run2")
        self.assertEqual(planned["candidates"][0]["bundle"][0]["state"], "to_verify")
        result = update_tracking(tracking, planned, [candidate], {}, {}, NOW)
        self.assertEqual(result[candidate.identity]["bic"]["unverified_urls"], [url])
        self.assertEqual(result[candidate.identity]["bic"]["ended_urls"], {})

    def test_restocked_obligation_becomes_a_known_comparator(self):
        url = "https://bic.example/found"
        candidate = offer(observed_run_id="run2")
        tracking = {candidate.identity: {"bic": {"unverified_urls": [url], "ended_urls": {url: RUN}}}}
        planned = build([candidate], [], tracking=tracking, run_id="run2")
        restocked = offer("bic", 8000, product_id="found", url=url, observed_run_id="run2")
        result = update_tracking(tracking, planned, [candidate, restocked], {}, {}, NOW)
        self.assertEqual(result[candidate.identity]["bic"]["unverified_urls"], [])
        self.assertEqual(result[candidate.identity]["bic"]["settled_urls"][0]["reason"], "became_known_comparator")

    def test_fetched_page_without_an_observation_stays_open(self):
        self.assertEqual(self.settle(None)["unverified_urls"], ["https://bic.example/found"])

    def test_without_a_current_candidate_nothing_is_handed_over(self):
        url = "https://bic.example/found"
        candidate = offer()
        tracking = {candidate.identity: {"bic": {"unverified_urls": [url]}}}
        found = offer("bic", 10000, product_id="found", url=url)
        result = update_tracking(tracking, build(peers=[], tracking=tracking), [found], {}, {}, NOW,
                                 {"bic": {"verified": [{"identity": candidate.identity, "url": url}]}})
        self.assertEqual(result[candidate.identity]["bic"]["unverified_urls"], [url])

    def test_success_failure_and_unreached_are_distinct_without_mutation(self):
        candidate = offer()
        peers = [offer(s) for s in ("bic", "sofmap", "tsukumo")]
        tracking = {candidate.identity: {s: {"last_attempt_at": None, "last_success_at": None,
                                             "consecutive_failures": 2, "last_search_at": None}
                                         for s in ("bic", "sofmap", "tsukumo")}}
        before = deepcopy(tracking)
        planned = build([candidate], peers, tracking=tracking)
        inputs = deepcopy((planned, peers))
        result = update_tracking(tracking, planned, [peers[0], offer("tsukumo", observed_run_id="old")],
                                 {"sofmap": {peers[1].url}}, {"bic": {candidate.identity}}, NOW)
        success, failure, unreached = (result[candidate.identity][s] for s in ("bic", "sofmap", "tsukumo"))
        self.assertEqual(success, {"last_attempt_at": iso(NOW), "last_success_at": iso(NOW),
                                   "consecutive_failures": 0, "last_search_at": iso(NOW)})
        self.assertEqual(failure, {"last_attempt_at": iso(NOW), "last_success_at": None,
                                   "consecutive_failures": 3, "last_search_at": None})
        self.assertEqual(unreached, before[candidate.identity]["tsukumo"])
        self.assertEqual(tracking, before)
        self.assertEqual((planned, peers), inputs)

    def test_unreached_requests_do_not_create_entries(self):
        planned = build()
        self.assertEqual(update_tracking({}, planned, [], {}, {}, NOW), {})

    def test_success_takes_precedence_for_same_url(self):
        peer = offer("bic")
        result = update_tracking({}, build(), [peer], {"bic": {peer.url}}, {}, NOW)
        self.assertEqual(result[peer.identity]["bic"]["consecutive_failures"], 0)

    def test_search_is_recorded_even_without_confirmation(self):
        candidate = offer()
        result = update_tracking({}, build([candidate], []), [], {}, {"bic": {candidate.identity}}, NOW)
        self.assertEqual(result[candidate.identity]["bic"], {"last_attempt_at": None, "last_success_at": None,
                                                            "consecutive_failures": 0, "last_search_at": iso(NOW)})

    def test_multiple_url_failures_for_one_identity_store_are_counted(self):
        peers = [offer("bic", product_id=str(i), url=f"https://bic.example/{i}") for i in range(2)]
        result = update_tracking({}, build(peers=peers), [], {"bic": {o.url for o in peers}}, {}, NOW)
        self.assertEqual(result[peers[0].identity]["bic"]["consecutive_failures"], 2)

    def test_shared_url_success_is_not_lost_when_offer_key_differs(self):
        peer = offer("bic")
        observed = offer("bic", product_id="other", url=peer.url)
        result = update_tracking({}, build(peers=[peer]), [observed], {}, {}, NOW)
        self.assertEqual(result[peer.identity]["bic"]["last_success_at"], iso(NOW))

    def test_same_offer_at_a_changed_url_completes_request(self):
        # A redirect can change the URL spelling of the planned offer.
        peer = offer("bic", url="https://bic.example/redirected")
        result = update_tracking({}, build(), [peer], {}, {}, NOW)
        self.assertEqual(result[peer.identity]["bic"]["last_success_at"], iso(NOW))

    def test_observation_of_another_offer_and_url_does_not_complete_request(self):
        peer = offer("bic", product_id="unrelated", url="https://bic.example/not-requested")
        self.assertEqual(update_tracking({}, build(), [peer], {}, {}, NOW), {})

    def test_partial_success_retains_failure_for_incomplete_bundle(self):
        peers = [offer("bic", product_id=str(i), url=f"https://bic.example/{i}") for i in range(2)]
        planned = build(peers=peers)
        first = update_tracking({}, planned, [peers[0]], {"bic": {peers[1].url}}, {}, NOW)
        planned["confirm"]["bic"].reverse()
        second = update_tracking({}, planned, [peers[0]], {"bic": {peers[1].url}}, {}, NOW)
        self.assertEqual(first, second)
        self.assertEqual(first[peers[0].identity]["bic"]["last_success_at"], iso(NOW))
        self.assertEqual(first[peers[0].identity]["bic"]["consecutive_failures"], 1)


if __name__ == "__main__":
    unittest.main()

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

    def test_observation_of_different_url_does_not_complete_request(self):
        peer = offer("bic", url="https://bic.example/not-requested")
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

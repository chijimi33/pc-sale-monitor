"""Offline routing regressions; all persisted data is disposable QAONLY state."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sale_monitor import comparison_routing
from sale_monitor.http import FetchError, Page
from sale_monitor.models import Offer, iso, same_product
from sale_monitor.parsing import parse_product
from sale_monitor.reporting import health
from sale_monitor.runner import Collector
from sale_monitor.scheduling import plan_comparisons
from sale_monitor.storage import Store


NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
OLD = iso(NOW - timedelta(days=30))
RUN = "QAONLY-routing-current"


def offer(store="ark", product="known", **changes):
    url = f"https://{store}.example/item/{product}"
    values = dict(store=store, product_id=product, url=url,
                  title="QAONLY Part", brand="Fixture", model="PART-1",
                  seller_id=store, condition="new", price_yen=12000,
                  shipping_yen=0, points_yen=0, stock="in_stock", verified=True,
                  observed_at=OLD, observed_run_id="QAONLY-prior",
                  discovery_kind="comparison", discovery_url=f"https://{store}.example/search/old",
                  evidence=[{"url": url, "checked_at": OLD, "content_hash": "QAONLY-old"}])
    values.update(changes)
    return Offer(**values)


def candidate(product="candidate", **changes):
    return offer("sofmap", product, **{
        "discovery_kind": "sale", "observed_at": iso(NOW),
        "observed_run_id": RUN, "price_yen": 10000, **changes})


def product_page(target, *, url=None, missing=(), member=False, body="", observed_at=None, warranty=None):
    """A product-scoped schema plus irrelevant body prices, never live HTML."""
    address = url or target.url
    terms = {"@type": "Offer", "priceCurrency": "JPY", "price": 13000,
             "availability": "https://schema.org/InStock",
             "itemCondition": "https://schema.org/NewCondition",
             "shippingDetails": {"shippingRate": {"currency": "JPY", "value": 0}}}
    for field in missing:
        terms.pop(field, None)
    item = {"@type": "Product", "name": "QAONLY Part", "url": address,
            "brand": "Fixture", "mpn": "PART-1", "offers": terms}
    member_html = ("<input id='priceIncTax' value='13000'>"
                   "<div class='productDetail--main__right'>"
                   "<div class='productDetail--main__right--price'>会員限定価格</div></div>"
                   if member else "")
    warranty_html = (f"<table><tr><th>保証期間</th><td>{warranty}</td></tr></table>"
                     if warranty is not None else "")
    html = ("<html><body><main><h1>QAONLY Part</h1>"
            f"<script type='application/ld+json'>{json.dumps(item)}</script>"
            f"{member_html}{warranty_html}</main><aside>{body}</aside></body></html>")
    return Page(address, html.encode("utf-8"), observed_at or iso(NOW),
                content_type="text/html; charset=utf-8")


class FakeClient:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.count = 0
        self.calls = []
        self.unexpected = []

    def get(self, url):
        self.calls.append(url)
        self.count += 1
        if url not in self.responses:
            self.unexpected.append(url)
            raise AssertionError("Unexpected QAONLY URL: " + url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response

    def rendered(self, url):
        self.unexpected.append(url)
        raise AssertionError("Browser access is forbidden in routing tests")


class ComparisonRouting(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="QAONLY-routing-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clients = []
        self.sequence = 0
        self.enterContext(patch("sale_monitor.runner.utcnow", return_value=NOW))
        self.enterContext(patch("sale_monitor.runner.iso", side_effect=lambda value=None: iso(value or NOW)))
        self.network = self.enterContext(patch("sale_monitor.http.Client.get", side_effect=AssertionError("No network")))
        self.browser = self.enterContext(patch("sale_monitor.http.Client.rendered", side_effect=AssertionError("No browser")))

    def tearDown(self):
        self.network.assert_not_called()
        self.browser.assert_not_called()
        for client in self.clients:
            self.assertEqual(client.unexpected, [])

    def request(self, candidates=None, store="ark", changes=None):
        planned, _ = plan_comparisons(candidates or [candidate()], changes or {}, [store], NOW)
        self.assertEqual(len(planned[store]), 1)
        return planned[store][0]

    def collector(self, known=(), *, requests=None, store="ark", client=None,
                  queue=None, done=None, state_run="QAONLY-prior", config=None):
        self.sequence += 1
        root = self.root / str(self.sequence) / "state"
        disk = Store(root)
        disk.save(f"stores/{store}.json", {
            "store": store, "run_id": state_run, "status": "partial",
            "cycle_complete": False, "queue": queue or {}, "done": done or [],
            "offers": {o.key: o.to_dict() for o in known}, "journal": []})
        disk.save(f"requests/{store}.json", [self.request(store=store)] if requests is None else requests)
        cfg = config or {"stores": {store: {"adapter": "html", "seed_urls": [], "browser_fallback": False}}}
        client = client or FakeClient()
        self.clients.append(client)
        return Collector(root, store, cfg, RUN, client)

    def searches(self, c):
        return {key: task for key, task in c.state["queue"].items() if task["type"] == "search"}

    def products(self, c):
        return {key: task for key, task in c.state["queue"].items() if task["type"] == "product"}

    def routed_task(self, c):
        tasks = [(key, task) for key, task in self.products(c).items() if task.get("comparison_routes")]
        self.assertEqual(len(tasks), 1)
        return tasks[0]

    def receipts(self, c):
        return list(c.state.get("comparison_routing", {}).get("receipts", {}).values())

    def one_iteration(self, c):
        # Stop after the first real process/catch/save cycle without sleeping or
        # processing the newly enqueued fallback search.
        with patch("sale_monitor.runner.time.monotonic", side_effect=[0, 0, 2]):
            c.collect(seconds=1)

    def assert_fallback(self, c, request):
        searches = list(self.searches(c).values())
        self.assertEqual(len(searches), 1)
        self.assertEqual(searches[0]["query"], request["query"])
        self.assertEqual(searches[0]["priority"], request.get("priority", 3))
        self.assertTrue(searches[0]["requested"])
        self.assertTrue(self.receipts(c))
        self.assertTrue(all(r["result"] != "verified_current_product" for r in self.receipts(c)))

    def test_planner_preserves_every_candidate_scope_without_prices(self):
        candidates = [candidate(), candidate("used", condition="used"),
                      candidate("bundle", variant="two modules", warranty="one year")]
        request = self.request(candidates, changes={candidates[1].key: "new"})
        self.assertEqual(request["priority"], 0)
        self.assertEqual(len(request["candidates"]), 3)
        for original, scoped in zip(candidates, request["candidates"]):
            self.assertTrue(same_product(original, Offer.from_dict(scoped)))
            self.assertEqual(scoped["seller_id"], original.seller_id)
            self.assertFalse(set(scoped) & {"price_yen", "shipping_yen", "stock", "observed_at", "evidence"})
        request["candidates"][0]["model"] = "mutated"
        self.assertEqual(candidates[0].model, "PART-1")

    def test_seed_reuses_existing_product_and_includes_all_known_prices(self):
        cheap = offer(price_yen=9000, issues=["latest_fetch_failed"])
        expensive = offer(product="expensive", price_yen=19000)
        c = self.collector([cheap, expensive])
        task_id = c.enqueue({"type": "product", "url": cheap.url, "kind": "comparison",
                             "created_at": OLD, "attempts": 7, "last_error": "http_503"})
        before = deepcopy(c.state["offers"])
        c.seed()
        self.assertEqual({t["url"] for t in self.products(c).values()}, {cheap.url, expensive.url})
        self.assertEqual(len(self.products(c)), 2)
        self.assertEqual(self.searches(c), {})
        self.assertEqual(c.state["queue"][task_id]["attempts"], 7)
        self.assertEqual(c.state["queue"][task_id]["created_at"], OLD)
        self.assertEqual(c.state["queue"][task_id]["last_error"], "http_503")
        self.assertTrue(all(t.get("comparison_routes") for t in self.products(c).values()))
        self.assertEqual(c.state["offers"], before)
        self.assertEqual(c.state["journal"], [])
        self.assertEqual(self.receipts(c), [])
        self.assertEqual(health(c.state, NOW)["current_offers"], 0)

    def test_legacy_missing_or_invalid_candidate_scope_uses_search(self):
        original = self.request()
        cases = [None, [], {}, [None], [{"store": "sofmap"}],
                 [{**original["candidates"][0], "model": "OTHER"}],
                 [{**original["candidates"][0], "condition": None}],
                 [{**original["candidates"][0], "channel": "flyer"}]]
        for scopes in cases:
            with self.subTest(scopes=scopes):
                request = deepcopy(original)
                if scopes is None:
                    request.pop("candidates")
                else:
                    request["candidates"] = scopes
                c = self.collector([offer()], requests=[request])
                c.seed()
                self.assertEqual(len(self.searches(c)), 1)
                self.assertFalse(any(t.get("comparison_routes") for t in self.products(c).values()))

    def test_untrusted_historical_rows_do_not_suppress_search(self):
        cases = [{"verified": False}, {"evidence": []}, {"shipping_yen": None},
                 {"observed_at": iso(NOW + timedelta(seconds=1))},
                 {"issues": ["canonical_product_mismatch", "latest_fetch_failed"]},
                 {"channel": "flyer"}, {"url": "https://rakuten.co.jp/item/known"}]
        for changes in cases:
            with self.subTest(changes=changes):
                c = self.collector([offer(**changes)])
                before = deepcopy(c.state["offers"])
                c.seed()
                self.assertEqual(len(self.searches(c)), 1)
                self.assertEqual(c.state["offers"], before)

    def test_identity_index_exactly_matches_full_scan_with_duplicates_and_unknowns(self):
        known = [offer(issues=["latest_fetch_failed"]), offer(product="cheap", price_yen=9000),
                 offer(product="used", condition="used"),
                 offer(product="bundle", variant="two modules", warranty="one year"),
                 offer(product="unrelated", model="OTHER"),
                 offer(product="unknown", model=None, brand=None),
                 offer(product="same-seller", seller_id="sofmap"),
                 offer(product="untrusted", evidence=[]),
                 offer(product="wrong-channel", channel="flyer"),
                 offer("joshin", "wrong-store")]
        rows = {o.key: o.to_dict() for o in known}
        rows["duplicate-row"] = deepcopy(known[0].to_dict())
        rows.update({"malformed": {"unknown": "row"}, "not-object": None})
        original = deepcopy(rows)
        index = comparison_routing.offers_by_identity(rows)
        requests = [self.request(), self.request([candidate(), candidate("used", condition="used")]),
                    self.request([candidate(variant="two modules", warranty="one year")]),
                    self.request([candidate(variant="two modules", warranty="three years")]),
                    self.request([candidate(variant="four modules")]),
                    self.request([candidate(model="UNSEEN")]),
                    {"identity": None, "query": "legacy"},
                    {**self.request(), "candidates": []}]
        for request in requests:
            with self.subTest(request=request):
                full = comparison_routing.known_targets(request, rows, "ark", NOW)
                indexed = comparison_routing.known_targets(
                    request, index.get(request.get("identity"), {}), "ark", NOW)
                self.assertEqual(indexed, full)
        targets, reason = comparison_routing.known_targets(requests[0], rows, "ark", NOW)
        self.assertEqual(reason, "known_urls_first")
        self.assertEqual([o.key for o in targets].count(known[0].key), 2)
        self.assertEqual({o.key for o in targets}, {known[0].key, known[1].key})
        self.assertEqual(rows, original)
        self.assertTrue(all("latest_fetch_failed" in o.issues for o in targets if o.key == known[0].key))

    def test_new_and_used_candidates_both_need_compatible_known_urls(self):
        request = self.request([candidate(), candidate("used", condition="used")])
        for complete in (False, True):
            with self.subTest(complete=complete):
                known = [offer()]
                if complete:
                    known.append(offer(product="used", condition="used"))
                c = self.collector(known, requests=[request]); c.seed()
                self.assertEqual(bool(self.searches(c)), not complete)
                if complete:
                    self.assertEqual(len(self.products(c)), 2)
                    self.assertTrue(all(t.get("comparison_routes") for t in self.products(c).values()))

    def test_each_variant_scope_requires_same_product_not_only_identity(self):
        request = self.request([candidate(), candidate("bundle", variant="two modules")])
        for variant in (None, "four modules", "two modules"):
            with self.subTest(variant=variant):
                c = self.collector([offer(), offer(product="bundle", variant=variant)], requests=[request])
                c.seed()
                self.assertEqual(bool(self.searches(c)), variant != "two modules")

    def test_warranty_coverage_uses_same_product_rules(self):
        request = self.request([candidate(warranty="one year"), candidate("long", warranty="three years")])
        for warranty in ("one year", "three years", None):
            with self.subTest(warranty=warranty):
                c = self.collector([offer(warranty="one year"), offer(product="second", warranty=warranty)], requests=[request])
                c.seed()
                # same_product deliberately permits an unspecified warranty;
                # two conflicting known warranties must still stay separate.
                self.assertEqual(bool(self.searches(c)), warranty == "one year")

    def test_unknown_historical_warranty_cannot_validate_fresh_conflicting_warranty(self):
        target = offer(warranty=None)
        original_candidate = candidate(warranty="one year")
        request = self.request([original_candidate])
        c = self.collector([target], requests=[request],
                           client=FakeClient({target.url: product_page(target, warranty="three years")}))
        c.seed()
        task_id, task = self.routed_task(c)
        expected = deepcopy(next(iter(task["comparison_routes"].values()))["expected"])
        self.assertIsNone(expected["warranty"])
        self.assertEqual(self.searches(c), {})
        self.one_iteration(c)
        current = Offer.from_dict(c.state["offers"][target.key])
        self.assertEqual(current.warranty, "three years")
        self.assertEqual(current.errors(NOW), [])
        self.assertTrue(same_product(target, original_candidate))
        self.assertTrue(same_product(target, current))
        self.assertFalse(same_product(original_candidate, current))
        # The four-argument API retains source-only compatibility. The real
        # product completion path must pass the original request as well.
        self.assertTrue(comparison_routing.current_target(current, expected, RUN, NOW))
        self.assertFalse(comparison_routing.current_target(current, expected, RUN, NOW, request))
        self.assert_fallback(c, request)
        self.assertIn(task_id, c.state["done"])
        self.assertEqual(len(self.receipts(c)), 1)
        self.assertEqual(self.receipts(c)[0]["expected"], expected)
        self.assertEqual(c.state["journal"][0]["offer"], current.to_dict())
        checkpoint = c.disk.load(c.name, {})
        self.assertEqual(checkpoint["comparison_routing"], c.state["comparison_routing"])
        self.assertEqual(checkpoint["queue"], c.state["queue"])
        self.assertEqual(c.client.calls, [target.url])

    def test_fresh_warranty_must_cover_both_scopes_previously_matched_by_unknown(self):
        target = offer(warranty=None)
        candidates = [candidate(warranty="one year"), candidate("long", warranty="three years")]
        request = self.request(candidates)
        c = self.collector([target], requests=[request],
                           client=FakeClient({target.url: product_page(target, warranty="one year")}))
        c.seed()
        task_id, task = self.routed_task(c)
        route = deepcopy(next(iter(task["comparison_routes"].values())))
        self.assertEqual(len(route["request"]["candidates"]), 2)
        self.assertTrue(all(same_product(target, item) for item in candidates))
        self.assertEqual(self.searches(c), {})
        self.one_iteration(c)
        current = Offer.from_dict(c.state["offers"][target.key])
        self.assertEqual(current.warranty, "one year")
        self.assertEqual(current.errors(NOW), [])
        self.assertTrue(same_product(current, candidates[0]))
        self.assertFalse(same_product(current, candidates[1]))
        self.assertTrue(comparison_routing.current_target(current, route["expected"], RUN, NOW))
        self.assertTrue(comparison_routing.current_target(
            current, route["expected"], RUN, NOW, self.request([candidates[0]])))
        self.assertFalse(comparison_routing.current_target(current, route["expected"], RUN, NOW, request))
        self.assert_fallback(c, request)
        self.assertIn(task_id, c.state["done"])
        self.assertEqual(len(self.receipts(c)), 1)
        self.assertEqual(self.receipts(c)[0]["request_id"], comparison_routing.request_key(request))
        self.assertEqual(len(c.state["journal"]), 1)
        self.assertEqual(c.state["journal"][0]["offer"], current.to_dict())
        checkpoint = c.disk.load(c.name, {})
        self.assertEqual(checkpoint["comparison_routing"], c.state["comparison_routing"])
        self.assertEqual(checkpoint["queue"], c.state["queue"])
        self.assertEqual(c.client.calls, [target.url])

    def test_independent_seller_is_required_for_each_candidate_scope(self):
        request = self.request([candidate(), candidate("affiliate", seller_id="ark")])
        for independent in (False, True):
            with self.subTest(independent=independent):
                known = [offer()]
                if independent:
                    known.append(offer(product="independent", seller_id="independent-fixture"))
                c = self.collector(known, requests=[request]); c.seed()
                self.assertEqual(bool(self.searches(c)), not independent)
                if independent:
                    self.assertEqual(len(self.products(c)), 2)

    def test_same_url_keeps_each_seller_warranty_expectation_and_falls_back_for_uncovered_scope(self):
        # Arrange the old implementation's first retained expectation to be the
        # one the response satisfies, so silently losing the second is detected.
        first, second = sorted([offer(seller_id="ark"), offer(seller_id="legacy-owner")], key=lambda o: o.key)
        first.warranty, second.warranty = "one year", "three years"
        request = self.request([candidate(warranty="one year"), candidate("long", warranty="three years")])
        request_id = comparison_routing.request_key(request)
        config = {"stores": {"ark": {"adapter": "html", "seed_urls": [], "browser_fallback": False}},
                  "seller_aliases": {"ark": first.seller_id}}
        c = self.collector([first, second], requests=[request], config=config)
        c.seed()
        task_id, task = self.routed_task(c)
        self.assertEqual(len(self.products(c)), 1)
        self.assertEqual(self.searches(c), {})
        routes = task["comparison_routes"]
        self.assertEqual(len(routes), 2)
        self.assertEqual({r["request_id"] for r in routes.values()}, {request_id})
        self.assertEqual({(r["expected"]["seller_id"], r["expected"]["warranty"]) for r in routes.values()},
                         {(first.seller_id, "one year"), (second.seller_id, "three years")})
        for route in routes.values():
            route["created_at"] = OLD
        task.update(created_at=OLD, attempts=3)
        c.save()
        original_routes = deepcopy(routes)

        client = FakeClient({first.url: product_page(first, warranty="one year")})
        resumed = Collector(c.disk.root, "ark", config, RUN, client)
        self.clients.append(client)
        resumed.seed()
        self.assertEqual(resumed.state["queue"][task_id]["comparison_routes"], original_routes)
        self.one_iteration(resumed)

        searches = list(self.searches(resumed).values())
        self.assertEqual(len(searches), 1)
        self.assertEqual((searches[0]["query"], searches[0]["priority"], searches[0]["created_at"]),
                         (request["query"], request["priority"], OLD))
        self.assertTrue(searches[0]["requested"])
        receipts = self.receipts(resumed)
        self.assertEqual(len(receipts), 2)
        self.assertEqual({r["request_id"] for r in receipts}, {request_id})
        self.assertEqual({(r["expected"]["seller_id"], r["expected"]["warranty"]): r["result"] for r in receipts},
                         {(first.seller_id, "one year"): "verified_current_product",
                          (second.seller_id, "three years"): "search_requested"})
        self.assertEqual(len(resumed.state["journal"]), 1)
        self.assertEqual(resumed.state["journal"][0]["offer"]["seller_id"], first.seller_id)
        self.assertEqual(resumed.state["journal"][0]["offer"]["warranty"], "one year")
        self.assertEqual(resumed.state["offers"][second.key]["observed_run_id"], "QAONLY-prior")
        self.assertIn(task_id, resumed.state["done"])
        checkpoint = resumed.disk.load(resumed.name, {})
        self.assertEqual(checkpoint["comparison_routing"], resumed.state["comparison_routing"])
        self.assertEqual(checkpoint["queue"], resumed.state["queue"])
        self.assertEqual(client.calls, [first.url])

    def test_existing_search_promotion_matches_legacy_baseline_preserving_age_attempts(self):
        request = self.request(changes={candidate().key: "new"})
        actual = self.collector([offer()], requests=[request])
        legacy = {key: value for key, value in request.items() if key != "candidates"}
        baseline = self.collector([offer()], requests=[legacy])
        original = {"type": "search", "query": request["query"], "kind": "comparison",
                    "priority": 3, "created_at": OLD, "attempts": 9, "last_error": "http_403"}
        for c in (actual, baseline):
            c.enqueue(deepcopy(original)); c.seed()
        self.assertEqual(self.searches(actual), self.searches(baseline),
                         "Known-URL routing must retain normal priority/requested promotion of existing search work")
        saved = next(iter(self.searches(actual).values()))
        self.assertEqual((saved["created_at"], saved["attempts"]), (OLD, 9))

    def test_promoted_durable_request_fallback_keeps_priority_and_original_age(self):
        target = offer()
        original_request = self.request()
        promoted_request = self.request(changes={candidate().key: "new"})
        request_id = comparison_routing.request_key(original_request)
        self.assertEqual(original_request["priority"], 3)
        self.assertEqual(promoted_request["priority"], 0)
        self.assertEqual(comparison_routing.request_key(promoted_request), request_id)
        c = self.collector([target], requests=[original_request])
        c.seed()
        task_id, task = self.routed_task(c)
        route = next(iter(task["comparison_routes"].values()))
        route["created_at"] = OLD
        task.update(created_at=OLD, attempts=7)
        expected = deepcopy(route["expected"])
        c.save()
        c.disk.save("requests/ark.json", [promoted_request])

        promoted_client = FakeClient()
        promoted = Collector(c.disk.root, "ark", c.config, "QAONLY-promoted", promoted_client)
        self.clients.append(promoted_client)
        promoted.seed()
        _, promoted_task = self.routed_task(promoted)
        self.assertEqual((promoted_task["priority"], promoted_task["created_at"], promoted_task["attempts"]), (0, OLD, 7))
        self.assertEqual(len(promoted_task["comparison_routes"]), 1)
        saved_route = next(iter(promoted_task["comparison_routes"].values()))
        self.assertEqual(saved_route["request_id"], request_id)
        self.assertEqual(saved_route["request"], promoted_request)
        self.assertEqual(saved_route["created_at"], OLD)
        self.assertEqual(saved_route["expected"], expected)
        self.assertEqual(self.searches(promoted), {})
        # A later lower-priority copy must not undo the durable promotion.
        promoted.route_comparison(original_request)
        self.assertEqual(saved_route["request"]["priority"], 0)
        promoted.save()
        self.assertEqual(promoted.disk.load(promoted.name, {})["queue"][task_id]["comparison_routes"],
                         promoted_task["comparison_routes"])

        # Failure after another interruption, with the request absent from the
        # plan, must use the promoted durable request and its original time.
        promoted.disk.save("requests/ark.json", [])
        client = FakeClient({target.url: FetchError("http_403")})
        resumed = Collector(c.disk.root, "ark", c.config, "QAONLY-after-promotion", client)
        self.clients.append(client)
        self.one_iteration(resumed)
        self.assert_fallback(resumed, promoted_request)
        search = next(iter(self.searches(resumed).values()))
        self.assertEqual(search["created_at"], OLD)
        pending = resumed.state["queue"][task_id]
        self.assertEqual((pending["created_at"], pending["attempts"]), (OLD, 8))
        self.assertEqual(pending["comparison_routes"], promoted_task["comparison_routes"])
        self.assertEqual(self.receipts(resumed)[0]["request_id"], request_id)
        self.assertEqual(self.receipts(resumed)[0]["reason"], "http_403")
        self.assertEqual(resumed.state["journal"], [])
        self.assertEqual(resumed.disk.load(resumed.name, {})["queue"], resumed.state["queue"])
        self.assertEqual(promoted_client.calls, [])
        self.assertEqual(client.calls, [target.url])

    def test_done_search_head_and_pending_pagination_are_retained(self):
        request = self.request()
        c = self.collector([offer()], state_run=RUN)
        c.state.update(list_pages=0, listed_candidates=0)
        head = c.enqueue({"type": "search", "query": request["query"], "kind": "comparison"})
        c.state["queue"].pop(head); c.state["done"].append(head)
        page = c.enqueue({"type": "list", "url": "https://ark.example/search/?page=2",
                          "kind": "comparison", "created_at": OLD, "attempts": 4, "priority": 2})
        unrelated = c.enqueue({"type": "search", "query": "older unrelated model", "kind": "comparison",
                               "created_at": OLD, "attempts": 8})
        before = deepcopy(c.state["queue"])
        c.seed()
        self.assertIn(head, c.state["done"])
        for key in (page, unrelated):
            self.assertEqual(c.state["queue"][key], before[key])
        self.assertEqual(set(self.searches(c)), {unrelated})
        self.assertEqual(len(self.products(c)), 1)

    def test_fresh_product_journal_and_receipt_precede_atomic_checkpoint(self):
        target = offer()
        client = FakeClient({target.url: product_page(target)})
        c = self.collector([target], client=client)
        saved = []
        atomic_save = c.disk.save

        def capture(name, value):
            atomic_save(name, value)
            saved.append(c.disk.load(name, {}))

        with patch.object(c.disk, "save", side_effect=capture):
            self.one_iteration(c)
        checkpoints = [s for s in saved if s["journal"]]
        self.assertTrue(checkpoints)
        for checkpoint in checkpoints:
            self.assertEqual(len(checkpoint["journal"]), 1)
            receipt = next(iter(checkpoint["comparison_routing"]["receipts"].values()))
            self.assertEqual(receipt["result"], "verified_current_product")
            row = checkpoint["journal"][0]["offer"]
            self.assertEqual((row["price_yen"], row["observed_run_id"]), (13000, RUN))
            self.assertEqual(receipt["evidence"], row["evidence"])
            self.assertEqual(checkpoint["offers"][target.key], row)
        self.assertEqual(self.searches(c), {})
        self.assertEqual(client.calls, [target.url])
        self.assertEqual(health(c.state, NOW)["comparison_routing"], c.state["comparison_routing"])
        # A repeated request in the same run has a real current receipt and
        # a done task: do not issue another HTTP request or manufacture history.
        c.route_comparison(self.request()); c.save()
        self.assertEqual(c.state["queue"], {})
        self.assertEqual(len(c.state["journal"]), 1)
        self.assertEqual(client.count, 1)
        self.assertEqual(len(self.receipts(c)), 1)

    def test_latest_failed_done_task_cannot_reuse_successful_same_run_journal(self):
        target = offer()
        c = self.collector([target], client=FakeClient({target.url: product_page(target)}))
        self.one_iteration(c)
        journal = deepcopy(c.state["journal"])
        c.state["offers"][target.key]["issues"].append("latest_fetch_failed")
        c.route_comparison(self.request()); c.save()
        self.assert_fallback(c, self.request())
        self.assertIn("latest_fetch_failed", c.state["offers"][target.key]["issues"])
        self.assertEqual(c.state["journal"], journal)
        self.assertEqual(c.client.count, 1)
        self.assertEqual(c.disk.load(c.name, {})["offers"], c.state["offers"])

    def test_http_failure_keeps_product_attempts_and_fallback_before_checkpoint(self):
        target = offer()
        c = self.collector([target], client=FakeClient({target.url: FetchError("http_503")}))
        task_id = c.enqueue({"type": "product", "url": target.url, "kind": "comparison",
                             "created_at": OLD, "attempts": 5})
        unrelated = c.enqueue({"type": "product", "url": "https://ark.example/item/backlog", "kind": "comparison",
                               "created_at": iso(NOW), "attempts": 8, "priority": 9})
        before = deepcopy(c.state["queue"][unrelated])
        snapshots = []
        save = c.disk.save

        def capture(name, value):
            save(name, value)
            snapshots.append(c.disk.load(name, {}))

        with patch.object(c.disk, "save", side_effect=capture):
            self.one_iteration(c)
        self.assert_fallback(c, self.request())
        self.assertEqual(c.state["queue"][task_id]["attempts"], 6)
        self.assertEqual(c.state["queue"][task_id]["created_at"], OLD)
        self.assertEqual(c.state["queue"][unrelated], before)
        self.assertNotIn(task_id, c.state["done"])
        self.assertEqual(c.state["journal"], [])
        self.assertEqual(c.state["offers"][target.key]["observed_run_id"], "QAONLY-prior")
        self.assertIn("latest_fetch_failed", c.state["offers"][target.key]["issues"])
        attempted = [s for s in snapshots if s["queue"][task_id]["attempts"] == 6]
        self.assertTrue(attempted)
        for snapshot in attempted:
            self.assertTrue(any(t["type"] == "search" for t in snapshot["queue"].values()))
            self.assertEqual(next(iter(snapshot["comparison_routing"]["receipts"].values()))["reason"], "http_503")

    def test_redirected_sku_records_actual_product_but_falls_back_original_query(self):
        target = offer()
        redirected = "https://ark.example/item/different-sku"
        c = self.collector([target], client=FakeClient({target.url: product_page(target, url=redirected)}))
        self.one_iteration(c)
        self.assert_fallback(c, self.request())
        self.assertEqual(c.state["journal"][0]["offer"]["url"], redirected)
        self.assertEqual(c.state["offers"][target.key]["observed_run_id"], "QAONLY-prior")
        self.assertEqual(self.receipts(c)[0]["expected"]["url"], target.url)

    def test_current_source_url_seller_and_identity_mismatches_fall_back(self):
        target = offer()
        mismatches = [{"store": "joshin"}, {"seller_id": "different-seller"},
                      {"channel": "flyer"}, {"model": "OTHER"},
                      {"url": "https://different.example/item/known"}]
        for changes in mismatches:
            with self.subTest(changes=changes):
                page = product_page(target)
                c = self.collector([target], client=FakeClient({target.url: page}))
                parsed = parse_product("ark", page, c.cfg, {"kind": "comparison"})
                for key, value in changes.items():
                    setattr(parsed, key, value)
                # Inject parser identity results; still exercise record(),
                # route completion, journal persistence and the collection loop.
                with patch("sale_monitor.runner.parse_product", return_value=parsed):
                    self.one_iteration(c)
                self.assert_fallback(c, self.request())
                self.assertEqual(len(c.state["journal"]), 1)

    def test_missing_current_fields_never_use_body_or_historical_price(self):
        target = offer(price_yen=7777)
        for field in ("price", "shippingDetails", "availability", "itemCondition"):
            with self.subTest(field=field):
                page = product_page(target, missing=[field], body="旧価格 7,777円 最安値 1円 在庫あり 送料無料 新品")
                c = self.collector([target], client=FakeClient({target.url: page}))
                self.one_iteration(c)
                self.assert_fallback(c, self.request())
                current = c.state["offers"][target.key]
                self.assertEqual(current["observed_run_id"], RUN)
                if field == "price":
                    self.assertIsNone(current["price_yen"])
                elif field == "shippingDetails":
                    self.assertIsNone(current["shipping_yen"])
                elif field == "availability":
                    self.assertEqual(current["stock"], "unknown")
                else:
                    self.assertIsNone(current["condition"])
                self.assertEqual(c.state["journal"][0]["offer"], current)

    def test_member_price_hold_selects_url_but_never_confirms_normal_price(self):
        target = offer("koubou", issues=["member_price_not_verified", "latest_fetch_failed"], verified=False)
        request = self.request(store="koubou")
        c = self.collector([target], store="koubou", requests=[request],
                           client=FakeClient({target.url: product_page(target, member=True)}))
        c.seed()
        _, task = self.routed_task(c)
        self.assertEqual(self.searches(c), {})
        self.assertIn("latest_fetch_failed", c.state["offers"][target.key]["issues"])
        c.process(task); c.save()
        self.assert_fallback(c, request)
        current = c.state["offers"][target.key]
        self.assertEqual(current["price_yen"], 13000)
        self.assertIn("member_price_not_verified", current["issues"])
        self.assertFalse(current["verified"])
        self.assertEqual(len(c.state["journal"]), 1)

    def test_interruption_retains_route_request_scope_and_original_time(self):
        target = offer()
        c = self.collector([target]); c.seed()
        task_id, task = self.routed_task(c)
        route = next(iter(task["comparison_routes"].values()))
        route["created_at"] = OLD
        task["created_at"] = OLD
        task["attempts"] = 4
        c.save()
        original = deepcopy(task)
        resumed = Collector(c.disk.root, c.store, c.config, RUN, FakeClient({target.url: FetchError("http_403")}))
        self.clients.append(resumed.client)
        resumed.seed()
        self.assertEqual(resumed.state["queue"][task_id], original)
        self.one_iteration(resumed)
        self.assert_fallback(resumed, route["request"])
        self.assertEqual(next(iter(self.searches(resumed).values()))["created_at"], OLD)
        self.assertEqual(resumed.state["queue"][task_id]["attempts"], 5)
        self.assertEqual(resumed.state["queue"][task_id]["comparison_routes"], original["comparison_routes"])

    def test_later_run_without_request_plan_still_falls_back_durable_original(self):
        target = offer()
        request = self.request(changes={candidate().key: "price_down"})
        c = self.collector([target], requests=[request]); c.seed()
        task_id, task = self.routed_task(c)
        next(iter(task["comparison_routes"].values()))["created_at"] = OLD
        task.update(created_at=OLD, attempts=11)
        c.save()
        original_routes = deepcopy(task["comparison_routes"])
        c.disk.save("requests/ark.json", [])
        c.disk.save("requests/candidate_identities.json", [])
        resumed = Collector(c.disk.root, "ark", c.config, "QAONLY-later", FakeClient({target.url: FetchError("http_403")}))
        self.clients.append(resumed.client)
        self.one_iteration(resumed)
        self.assert_fallback(resumed, request)
        self.assertEqual(next(iter(self.searches(resumed).values()))["created_at"], OLD)
        self.assertEqual(resumed.state["queue"][task_id]["comparison_routes"], original_routes)
        self.assertEqual(resumed.state["queue"][task_id]["attempts"], 12)
        self.assertEqual(self.receipts(resumed)[0]["run_id"], "QAONLY-later")
        self.assertEqual(resumed.disk.load(resumed.name, {})["queue"], resumed.state["queue"])

    def test_done_task_requires_fresh_same_run_usable_observation(self):
        for observed_at, observed_run in ((OLD, RUN), (iso(NOW), "QAONLY-other")):
            with self.subTest(observed_at=observed_at, run_id=observed_run):
                target = offer(observed_at=observed_at, observed_run_id=observed_run)
                c = self.collector([target], state_run=RUN)
                task_id = c.enqueue({"type": "product", "url": target.url, "kind": "comparison"})
                c.state["queue"].pop(task_id); c.state["done"].append(task_id)
                c.route_comparison(self.request())
                self.assert_fallback(c, self.request())
                self.assertEqual(c.client.count, 0)
                self.assertEqual(c.state["journal"], [])

    def test_unexpected_parse_failure_uses_catch_fallback_without_fabricated_offer(self):
        target = offer()
        c = self.collector([target], client=FakeClient({target.url: product_page(target)}))
        with patch("sale_monitor.runner.parse_product", side_effect=ValueError("QAONLY parser failure")):
            self.one_iteration(c)
        self.assert_fallback(c, self.request())
        self.assertEqual(self.receipts(c)[0]["reason"], "ValueError")
        self.assertEqual(c.state["journal"], [])
        _, task = self.routed_task(c)
        self.assertEqual(task["attempts"], 1)
        self.assertEqual(task["last_error"], "ValueError")
        self.assertIn("latest_fetch_failed", c.state["offers"][target.key]["issues"])


if __name__ == "__main__":
    unittest.main()

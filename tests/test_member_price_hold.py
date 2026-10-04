"""Offline member-price holds using invented markup and isolated state only.

No captured HTML, credentials, or real hidden form fields are used. Temporary
state follows TEMP/TMP, so the targeted run can keep every output on the QA drive.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sale_monitor.comparison_integrity import known_comparators, missing_comparators
from sale_monitor.engine import evaluate
from sale_monitor.http import Page
from sale_monitor.models import Offer, STORES, digest, iso
from sale_monitor.parsing import parse_product
from sale_monitor.reporting import aggregate
from sale_monitor.runner import Collector
from sale_monitor.storage import Store, read_json


NOW = datetime(2026, 10, 4, 10, tzinfo=timezone.utc)
URL = "https://www.pc-koubou.jp/products/detail.php?product_id=999001"
MODEL = "SYNTHETIC-PART-1"
BRAND = "Synthetic Brand"
NOTICE = "会員価格はログイン後に表示されます"
ISSUE = "member_price_not_verified"
HOLD = "comparator_member_price_not_verified"
CFG = {"stores": {"koubou": {"adapter": "html", "seed_urls": [],
    "browser_fallback": True, "default_condition": "new"}}, "flyer": {}}


def product_page(*, price_notice="", title_notice="", primary_extra="",
                 main_extra="", outside="", now=NOW, normal_price=13000):
    """A visible synthetic price control supplies the parser's normal price."""
    product = {"@context": "https://schema.org", "@type": "Product", "url": URL,
        "name": "Synthetic graphics card", "mpn": MODEL, "brand": BRAND,
        "offers": {"@type": "Offer", "priceCurrency": "JPY", "price": str(normal_price),
            "availability": "https://schema.org/InStock",
            "itemCondition": "https://schema.org/NewCondition",
            "shippingDetails": {"shippingRate": {"currency": "JPY", "value": "0"}}}}
    markup = f"""<!doctype html><html><head><meta charset="utf-8">
      <script type="application/ld+json">{json.dumps(product)}</script></head>
      <body><main><section class="productDetail--main__right">
        <div class="title-box"><h1>Synthetic graphics card</h1>{title_notice}</div>
        <div class="productDetail--main__right--price">
          <label>通常価格 <input type="number" id="priceIncTax" value="{normal_price}"></label>
          {price_notice}
        </div>
        <dl><dt>ポイント</dt><dd>0ポイント</dd></dl>{primary_extra}
      </section>{main_extra}</main>{outside}</body></html>"""
    return Page(URL, markup.encode("utf-8"), iso(now))


def parsed_member(*, now=NOW, run_id="first"):
    offer = parse_product("koubou", product_page(price_notice=NOTICE, now=now),
                          CFG["stores"]["koubou"], {"kind": "comparison"})
    offer.observed_run_id = run_id
    return offer


def offer(store="ark", price=10000, *, now=NOW, run_id="first", **changes):
    url = f"https://{store}.example/product/synthetic-part"
    data = dict(store=store, product_id="synthetic-part", url=url,
        brand=BRAND, model=MODEL, seller_id=store, condition="new", stock="in_stock",
        price_yen=price, shipping_yen=0, points_yen=0, verified=True,
        discovery_kind="sale" if store == "ark" else "comparison",
        observed_at=iso(now), observed_run_id=run_id,
        evidence=[{"url": url, "checked_at": iso(now), "content_hash": digest([store, price, iso(now)])}])
    data.update(changes)
    return Offer(**data)


def state(row, *, journal=False):
    return {"store": row.store, "run_id": row.observed_run_id, "status": "complete",
        "queue": {}, "offers": {row.key: row.to_dict()}, "done": [], "cycle_complete": True,
        "journal": [{"observation_id": digest(row.to_dict()), "run_id": row.observed_run_id,
                     "offer": row.to_dict()}] if journal else []}


class OfflineTest(unittest.TestCase):
    def setUp(self):
        # A missed stub must fail before any real HTTP or browser activity.
        for target in ("sale_monitor.http.Client.get", "sale_monitor.http.Client.rendered",
                       "socket.create_connection", "socket.socket.connect"):
            guard = patch(target, side_effect=AssertionError("Offline test attempted network/browser I/O"))
            guard.start()
            self.addCleanup(guard.stop)


class MemberPriceParsing(OfflineTest):
    def test_primary_price_and_title_notices_hold_and_preserve_normal_price_evidence(self):
        for zone in ("price_notice", "title_notice"):
            for notice in (NOTICE, "会員限定価格についてはログインしてください"):
                with self.subTest(zone=zone, notice=notice):
                    page = product_page(**{zone: notice})
                    row = parse_product("koubou", page, CFG["stores"]["koubou"])
                    self.assertEqual(row.price_yen, 13000)
                    self.assertEqual(row.payment, 13000)
                    self.assertEqual(row.issues, [ISSUE])
                    self.assertFalse(row.verified)
                    self.assertIn(ISSUE, row.errors(NOW))
                    proof = row.evidence[0]
                    self.assertEqual(proof["url"], URL)
                    self.assertEqual(proof["checked_at"], iso(NOW))
                    self.assertEqual(proof["content_hash"], digest(page.text))
                    conditional = proof["fields"]["conditional_prices"]
                    self.assertEqual(len(conditional), 1)
                    self.assertEqual(conditional[0]["kind"], "member")
                    self.assertEqual(conditional[0]["status"], "unverified")
                    self.assertIsNone(conditional[0]["price_yen"])
                    self.assertEqual(conditional[0]["normal_price_yen"], 13000)
                    self.assertTrue(any(notice in text for text in conditional[0]["notices"]))

    def assert_normal(self, page):
        row = parse_product("koubou", page, CFG["stores"]["koubou"])
        self.assertEqual(row.issues, [])
        self.assertTrue(row.verified)
        self.assertEqual(row.errors(NOW), [])
        self.assertEqual(row.price_yen, 13000)
        self.assertNotIn("conditional_prices", row.evidence[0]["fields"])

    def test_footer_navigation_and_recommendations_outside_primary_panels_do_not_hold(self):
        for fragment in (f"<footer>{NOTICE}</footer>", f"<nav>{NOTICE}</nav>",
                         f'<aside class="recommendations"><div class="title-box">{NOTICE}</div></aside>'):
            for placement in ("outside", "main_extra"):
                with self.subTest(fragment=fragment, placement=placement):
                    self.assert_normal(product_page(**{placement: fragment}))

    def test_nested_navigation_and_recommendations_are_not_primary_product_prices(self):
        for fragment in (f"<footer>{NOTICE}</footer>", f"<nav>{NOTICE}</nav>",
                         f'<aside class="recommendations"><div class="title-box">{NOTICE}</div></aside>'):
            with self.subTest(fragment=fragment):
                self.assert_normal(product_page(price_notice=fragment))

    def test_script_style_template_and_noscript_notices_do_not_hold(self):
        for tag in ("script", "style", "template", "noscript"):
            for zone in ("price_notice", "title_notice"):
                with self.subTest(tag=tag, zone=zone):
                    self.assert_normal(product_page(**{zone: f"<{tag}>{NOTICE}</{tag}>"}))
        for tag in ("template", "noscript"):
            with self.subTest(hidden_primary=tag):
                spoof = (f'<{tag}><section class="productDetail--main__right">'
                         f'<div class="title-box">{NOTICE}</div></section></{tag}>')
                self.assert_normal(product_page(main_extra=spoof))

    def test_nonmember_prices_and_registration_copy_do_not_hold(self):
        for text in ("非会員価格 13,000円", "非会員向け価格 13,000円", "会員登録はこちら",
                     "会員限定クーポンのご案内", "通常価格 13,000円"):
            with self.subTest(text=text):
                self.assert_normal(product_page(price_notice=text))

    def test_koubou_notice_rule_does_not_apply_to_other_store_parser(self):
        row = parse_product("ark", product_page(price_notice=NOTICE), {"default_condition": "new"})
        self.assertNotIn(ISSUE, row.issues)
        self.assertNotIn("conditional_prices", row.evidence[0]["fields"])


class MemberPriceDiskTests(OfflineTest):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="member-price-hold-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "state"
        self.public = Path(temporary.name) / "public"
        self.disk = Store(self.root)

    def write_market(self, *, member=None, rule="A", now=NOW, run_id="first"):
        rows = [offer(now=now, run_id=run_id)]
        rows += ([offer("sofmap", 12000, now=now, run_id=run_id),
                  offer("joshin", 12500, now=now, run_id=run_id)] if rule == "A" else
                 [offer("sofmap", 10500, now=now, run_id=run_id)])
        for row in rows:
            self.disk.save(f"stores/{row.store}.json", state(row))
        if member is not None:
            self.disk.save("stores/koubou.json", state(member, journal=True))
        return rows

    def write_B_history(self, rule):
        samples = ((40, 12000),) if rule == "B_observed_year_low" else ((40, 12500), (20, 12500), (1, 10000))
        for days, price in samples:
            row = offer(price=price, now=NOW-timedelta(days=days), run_id=f"past-{days}")
            self.disk.save(f"history/ark/{row.observed_at[:10]}.json",
                           [{"observation_id": digest(row.to_dict()), "run_id": row.observed_run_id,
                             "offer": row.to_dict()}])

    def aggregate_candidate(self, run_id="first", now=NOW):
        index = aggregate(self.root, self.public, run_id, now)
        decisions = read_json(self.public / "evidence.json", {})["decisions"]
        decision = next(d for d in decisions if d["offer"]["store"] == "ark")
        self.assertEqual(index["monitored_store_count"], 10)
        self.assertEqual(set(index["stores"]), set(STORES))
        return index, decision

    def assert_held(self, decision, rule="A"):
        for basis in ("payment", "points"):
            with self.subTest(basis=basis, rule=rule):
                result = decision[basis]
                self.assertEqual(result["status"], "insufficient")
                self.assertEqual(result["provisional_rule"], rule)
                self.assertIsNone(result["rule"])
                self.assertIn("known_comparator_not_verified_this_run", result["reasons"])
                self.assertIn(HOLD, result["reasons"])
                gaps = result["missing_comparators"]
                self.assertEqual(len(gaps), 1)
                self.assertEqual(gaps[0]["store"], "koubou")
                self.assertEqual(gaps[0]["basis"], basis)
                self.assertTrue(gaps[0]["unverified_member_price"])
                self.assertIsNone(gaps[0]["last_verified_at"])
                self.assertNotIn("koubou", [p["seller_id"] for p in result["comparisons"]])

    def test_collector_records_member_observation_without_browser_login_or_retry(self):
        page = product_page(price_notice=NOTICE)

        class FixtureClient:
            def __init__(self):
                self.count = 0
                self.calls = []
                self.browser_calls = []
                self.retry_after = {}
                self.transport_retry_after = {}

            def get(self, url):
                self.calls.append(url)
                self.count += 1
                if url != URL:
                    raise AssertionError("Unexpected fixture URL, including login")
                return page

            def rendered(self, url):
                self.browser_calls.append(url)
                raise AssertionError("A member-price hold must not launch a browser")

        client = FixtureClient()
        collector = Collector(self.root, "koubou", CFG, "first", client)
        collector.enqueue({"type": "product", "url": URL, "kind": "comparison",
                           "created_at": iso(NOW-timedelta(days=3)), "attempts": 0})
        task_id = next(iter(collector.state["queue"]))
        with patch("sale_monitor.runner.collect_flyer", return_value=([], {"status": "fixture"})):
            result = collector.collect(seconds=5)
        self.assertEqual(client.calls, [URL])
        self.assertEqual(client.browser_calls, [])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["retry_activity"]["retries_started"], 0)
        self.assertEqual(result["queue"], {})
        self.assertEqual(result["done"], [task_id])
        self.assertEqual(len(result["offers"]), 1)
        self.assertEqual(len(result["journal"]), 1)
        row = next(iter(result["offers"].values()))
        self.assertEqual(row["issues"], [ISSUE])
        self.assertEqual(row["observed_run_id"], "first")
        self.assertEqual(row["price_yen"], 13000)
        self.assertFalse(row["verified"])
        self.assertEqual(result["journal"][0]["offer"], row)
        saved = self.disk.load("stores/koubou.json", {})
        self.assertEqual(saved["offers"], result["offers"])
        self.assertEqual(saved["journal"], result["journal"])
        journal = deepcopy(result["journal"])
        row["issues"].append("latest_fetch_failed")
        row["evidence"][0]["fields"]["conditional_prices"][0]["notices"].append("later mutation")
        self.assertEqual(result["journal"], journal)

    def test_first_ever_member_observation_holds_both_A_bases_without_event(self):
        member = parsed_member()
        member.points_yen = None
        rows = self.write_market(member=member)
        self.assertEqual(self.disk.history(), [])
        for points in (False, True):
            self.assertEqual(evaluate(rows[0], rows[1:], [], NOW, points)["rule"], "A")
        index, decision = self.aggregate_candidate()
        self.assert_held(decision)
        self.assertEqual(index["notification_count"], 0)
        self.assertEqual(self.disk.load("events/registry.json", {})["events"], {})
        self.assertEqual(index["comparison_planning"]["known_comparator_hold_offers"], 1)
        self.assertEqual([c["price_yen"] for c in decision["payment"]["comparisons"]], [12000, 12500])
        self.assertEqual(self.disk.load("stores/koubou.json", {})["offers"][member.key], member.to_dict())

    def test_first_ever_member_observation_holds_both_B_variants_and_bases(self):
        for rule in ("B_observed_year_low", "B_recent_median"):
            with self.subTest(rule=rule):
                # Separate histories prevent one B rule from satisfying the other case.
                case = Store(self.root / rule)
                original = self.disk, self.root, self.public
                self.disk, self.root, self.public = case, case.root, self.public / rule
                try:
                    self.write_B_history(rule)
                    rows = self.write_market(member=parsed_member(), rule=rule)
                    for points in (False, True):
                        self.assertEqual(evaluate(rows[0], rows[1:], self.disk.history(), NOW, points)["rule"], rule)
                    index, decision = self.aggregate_candidate()
                    self.assert_held(decision, rule)
                    self.assertEqual(index["notification_count"], 0)
                    self.assertEqual(self.disk.load("events/registry.json", {})["events"], {})
                finally:
                    self.disk, self.root, self.public = original

    def test_points_only_acceptance_is_held_without_changing_payment_rejection(self):
        rows = [offer(price=12000, points_yen=3000), offer("sofmap", 10000), offer("joshin", 10500)]
        for row in rows:
            self.disk.save(f"stores/{row.store}.json", state(row))
        self.disk.save("stores/koubou.json", state(parsed_member(), journal=True))
        self.assertEqual(evaluate(rows[0], rows[1:], [], NOW, points=True)["rule"], "A")
        index, decision = self.aggregate_candidate()
        self.assertEqual(decision["payment"]["status"], "rejected")
        self.assertEqual(decision["payment"]["reasons"], ["cheaper_current_offer"])
        self.assertEqual(decision["points"]["status"], "insufficient")
        self.assertEqual(decision["points"]["provisional_rule"], "A")
        self.assertIn(HOLD, decision["points"]["reasons"])
        self.assertEqual(index["notification_count"], 0)
        self.assertEqual(self.disk.load("events/registry.json", {})["events"], {})

    def test_next_run_outage_retains_first_observation_obligation_from_state_or_history(self):
        member = parsed_member()
        self.write_market(member=member)
        self.aggregate_candidate()
        next_time = NOW + timedelta(days=2)
        self.write_market(now=next_time, run_id="outage")
        recorded = self.disk.load("stores/koubou.json", {})
        recorded.update(run_id="outage", status="partial", cycle_complete=False)
        recorded["offers"][member.key]["issues"].append("latest_fetch_failed")
        for history_only in (False, True):
            with self.subTest(history_only=history_only):
                failed = deepcopy(recorded)
                if history_only:
                    failed["offers"] = {}
                self.disk.save("stores/koubou.json", failed)
                index, decision = self.aggregate_candidate("outage", next_time)
                self.assert_held(decision)
                self.assertEqual(index["notification_count"], 0)
                for basis in ("payment", "points"):
                    gap = decision[basis]["missing_comparators"][0]
                    self.assertEqual(gap["last_conditional_price_observed_at"], iso(NOW))
                    self.assertIn("not_observed_this_run", gap["current_issues"])
                member_history = [r for r in self.disk.history() if r["offer"]["store"] == "koubou"]
                self.assertEqual(len(member_history), 1)
                self.assertFalse(member_history[0]["offer"]["verified"])
                self.assertIn(ISSUE, member_history[0]["offer"]["issues"])

    def test_fresh_normal_price_restores_normal_A_and_B_evaluation(self):
        for rule in ("A", "B_observed_year_low", "B_recent_median"):
            with self.subTest(rule=rule):
                case = Store(self.root / rule)
                original = self.disk, self.root, self.public
                self.disk, self.root, self.public = case, case.root, self.public / rule
                try:
                    if rule != "A":
                        self.write_B_history(rule)
                    self.write_market(member=parsed_member(), rule=rule)
                    self.assert_held(self.aggregate_candidate()[1], rule)
                    later = NOW + timedelta(hours=1)
                    recovered = parse_product("koubou", product_page(now=later), CFG["stores"]["koubou"], {"kind": "comparison"})
                    recovered.observed_run_id = "recovered"
                    self.write_market(member=recovered, rule=rule, now=later, run_id="recovered")
                    index, decision = self.aggregate_candidate("recovered", later)
                    for basis in ("payment", "points"):
                        self.assertEqual(decision[basis]["status"], "accepted")
                        self.assertEqual(decision[basis]["rule"], rule)
                        self.assertNotIn(HOLD, decision[basis]["reasons"])
                        self.assertIn("koubou", [p["seller_id"] for p in decision[basis]["comparisons"]])
                    self.assertEqual(index["notification_count"], 1)
                finally:
                    self.disk, self.root, self.public = original

    def test_fresh_explicit_end_releases_hold_without_using_ended_price(self):
        self.write_market(member=parsed_member())
        self.assert_held(self.aggregate_candidate()[1])
        later = NOW + timedelta(hours=1)
        ended = parse_product("koubou", product_page(now=later), CFG["stores"]["koubou"], {"kind": "comparison"})
        ended.observed_run_id = "ended"
        ended.stock = "out_of_stock"
        self.write_market(member=ended, now=later, run_id="ended")
        index, decision = self.aggregate_candidate("ended", later)
        for basis in ("payment", "points"):
            self.assertEqual(decision[basis]["status"], "accepted")
            self.assertEqual(decision[basis]["rule"], "A")
            self.assertNotIn("koubou", [p["seller_id"] for p in decision[basis]["comparisons"]])
        self.assertEqual(index["notification_count"], 1)

    def test_cheaper_normal_price_recovery_rejects_candidate_instead_of_releasing_alert(self):
        self.write_market(member=parsed_member())
        self.assert_held(self.aggregate_candidate()[1])
        later = NOW + timedelta(hours=1)
        recovered = parse_product("koubou", product_page(now=later, normal_price=9000),
                                  CFG["stores"]["koubou"], {"kind": "comparison"})
        recovered.observed_run_id = "recovered"
        self.write_market(member=recovered, now=later, run_id="recovered")
        index, decision = self.aggregate_candidate("recovered", later)
        for basis in ("payment", "points"):
            self.assertEqual(decision[basis]["status"], "rejected")
            self.assertEqual(decision[basis]["reasons"], ["cheaper_current_offer"])
            self.assertEqual(decision[basis]["comparisons"][0]["price_yen"], 9000)
            self.assertEqual(decision[basis]["comparisons"][0]["observed_at"], iso(later))
        self.assertEqual(index["notification_count"], 0)
        self.assertEqual(self.disk.load("events/registry.json", {})["events"], {})

    def test_hold_and_recovery_preserve_registry_old_queue_and_history_without_duplicate_event(self):
        self.write_market()
        self.aggregate_candidate()
        registry = self.disk.load("events/registry.json", {})
        self.assertEqual(len(registry["events"]), 1)
        event_id = next(iter(registry["events"]))
        registry["events"][event_id]["delivery_status"] = "delivered"
        self.disk.save("events/registry.json", registry)
        prior = deepcopy(registry)
        old_queue = {"original-search-id": {"type": "search", "kind": "comparison", "query": "old query",
            "created_at": iso(NOW-timedelta(days=20)), "attempts": 17, "last_error": "http_403"}}
        old = offer("koubou", now=NOW-timedelta(days=30), run_id="old", model="UNRELATED-PART")
        history_name = f"history/koubou/{old.observed_at[:10]}.json"
        self.disk.save(history_name, [{"observation_id": "original-observation", "run_id": "old", "offer": old.to_dict()}])
        history_bytes = (self.root / history_name).read_bytes()
        for phase, minutes in (("held", 30), ("recovered", 60)):
            with self.subTest(phase=phase):
                later = NOW + timedelta(minutes=minutes)
                member = parsed_member(now=later, run_id=phase) if phase == "held" else parse_product(
                    "koubou", product_page(now=later), CFG["stores"]["koubou"], {"kind": "comparison"})
                member.observed_run_id = phase
                self.write_market(member=member, now=later, run_id=phase)
                pending = self.disk.load("stores/koubou.json", {})
                pending.update(queue=deepcopy(old_queue), cycle_complete=False, status="partial")
                self.disk.save("stores/koubou.json", pending)
                index, decision = self.aggregate_candidate(phase, later)
                if phase == "held":
                    self.assert_held(decision)
                else:
                    self.assertEqual(decision["payment"]["status"], "accepted")
                    self.assertEqual(decision["points"]["status"], "accepted")
                self.assertEqual(index["notification_count"], 0)
                after = self.disk.load("events/registry.json", {})
                self.assertEqual(after["states"][offer().key], prior["states"][offer().key])
                self.assertEqual(after["events"], prior["events"])
                self.assertEqual(self.disk.load("stores/koubou.json", {})["queue"], old_queue)
                self.assertEqual((self.root / history_name).read_bytes(), history_bytes)


class MemberPriceComparatorScope(OfflineTest):
    def test_excluded_or_unconfirmed_member_sources_do_not_create_obligations(self):
        changes_by_case = {
            "excluded_store": {"store": "rakuten"},
            "excluded_host": {"url": "https://www.rakuten.co.jp/item/synthetic"},
            "non_web_url": {"url": "file:///synthetic-product"},
            "offline_channel": {"channel": "store"},
            "missing_identity": {"model": None, "brand": None},
            "missing_seller": {"seller_id": None},
            "missing_condition": {"condition": None},
            "missing_evidence": {"evidence": []},
            "conflicting_product": {"issues": [ISSUE, "canonical_product_mismatch"]},
            "future_observation": {"observed_at": iso(NOW+timedelta(hours=1))},
            "undated_observation": {"observed_at": None},
        }
        for case, changes in changes_by_case.items():
            with self.subTest(case=case):
                member = parsed_member()
                for key, value in changes.items():
                    setattr(member, key, value)
                self.assertEqual(known_comparators({member.store: state(member)}, [], NOW), {})
                self.assertEqual(known_comparators({}, [{"offer": member.to_dict()}], NOW), {})

    def test_incompatible_identity_condition_variant_and_same_seller_do_not_hold(self):
        for changes in ({"model": "OTHER-PART"}, {"condition": "used"}, {"variant": "two modules"},
                        {"seller_id": "ark"}):
            with self.subTest(changes=changes):
                member = parsed_member()
                for key, value in changes.items():
                    setattr(member, key, value)
                known = known_comparators({"koubou": state(member)}, [], NOW)
                for points in (False, True):
                    self.assertEqual(missing_comparators(offer(), known, {}, NOW, points=points), [])

    def test_only_exact_current_source_can_resolve_member_obligation(self):
        member = parsed_member()
        known = known_comparators({}, [{"offer": member.to_dict()}], NOW)
        for changes in ({"store": "tsukumo"}, {"model": "OTHER-PART"}, {"condition": "used"},
                        {"variant": "two modules"}, {"seller_id": "different-seller"}):
            with self.subTest(changes=changes):
                current = deepcopy(member)
                current.issues = []
                current.verified = True
                for key, value in changes.items():
                    setattr(current, key, value)
                for points in (False, True):
                    gaps = missing_comparators(offer(), known, {(current.store, current.key): current}, NOW, points=points)
                    self.assertEqual(len(gaps), 1)
                    self.assertTrue(gaps[0]["unverified_member_price"])

    def test_stale_unverified_failed_or_still_member_sold_out_does_not_release_hold(self):
        member = parsed_member()
        known = known_comparators({}, [{"offer": member.to_dict()}], NOW)
        for changes in ({"observed_at": iso(NOW-timedelta(days=1))}, {"verified": False},
                        {"issues": ["latest_fetch_failed"]}, {"issues": [ISSUE]}, {"evidence": []}):
            with self.subTest(changes=changes):
                ended = deepcopy(member)
                ended.stock, ended.verified, ended.issues = "out_of_stock", True, []
                for key, value in changes.items():
                    setattr(ended, key, value)
                for points in (False, True):
                    self.assertEqual(len(missing_comparators(offer(), known, {(ended.store, ended.key): ended}, NOW, points=points)), 1)

    def test_current_normal_price_resolves_payment_but_unknown_points_remain_held(self):
        member = parsed_member()
        known = known_comparators({}, [{"offer": member.to_dict()}], NOW)
        normal = deepcopy(member)
        normal.issues, normal.verified, normal.points_yen = [], True, None
        current = {(normal.store, normal.key): normal}
        self.assertEqual(missing_comparators(offer(), known, current, NOW), [])
        gaps = missing_comparators(offer(), known, current, NOW, points=True)
        self.assertEqual(len(gaps), 1)
        self.assertIn("points_unknown", gaps[0]["current_issues"])

    def test_fresh_verified_expiry_or_exhausted_coupon_resolves_both_bases(self):
        member = parsed_member()
        known = known_comparators({}, [{"offer": member.to_dict()}], NOW)
        for changes in ({"expires_at": iso(NOW-timedelta(minutes=1))},
                        {"coupon": {"verified": True, "remaining": 0, "limited": True, "checked_at": iso(NOW)}}):
            with self.subTest(changes=changes):
                ended = deepcopy(member)
                ended.issues, ended.verified = [], True
                for key, value in changes.items():
                    setattr(ended, key, value)
                self.assertTrue(ended.errors(NOW))
                for points in (False, True):
                    self.assertEqual(missing_comparators(offer(), known, {(ended.store, ended.key): ended}, NOW, points=points), [])


if __name__ == "__main__":
    unittest.main()

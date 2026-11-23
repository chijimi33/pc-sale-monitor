"""In-memory Sofmap fixtures modeled on the two verified retained captures.

The actual layout has #main and #detail_area as siblings under div#wrapper.item,
with 商品番号 BEFORE JANコード in #tab2.prod_detail's direct specification table.
Tests deliberately do not read captures, write fixtures, or use the network.
"""
from copy import deepcopy
import hashlib
from html import escape
import json
import unittest
from unittest.mock import patch

from sale_monitor.http import Page
from sale_monitor.models import valid_jan
from sale_monitor.parsing import parse_product
from monitor_lab.identity import extract_primary_jan


JAN = "0195553309745"
OTHER = "4711289500124"
SKU = "100788840"
URL = "https://www.sofmap.com/product_detail.aspx?sku=" + SKU
NOW = "2026-10-03T23:21:22.111382+00:00"
TITLE = "マザーボード(Socket AM4) PRIME B550M-A WIFI II ［MicroATX］"
CFG = {"adapter": "html", "default_condition": "new"}


def row(label, value):
    return "<tr><th>" + escape(label) + "</th><td>" + escape(value) + "</td></tr>"


def table(sku=SKU, claims=(("JANコード", JAN),)):
    return ("<table>" + row("商品名", TITLE) + row("型番", "PRIMEB550MAWIFI2") +
            (row("商品番号", sku) if sku is not None else "") +
            "".join(row(label, value) for label, value in claims) + "</table>")


def markup(sku=SKU, claims=(("JANコード", JAN),), *, extra_spec="", info="", before="", after=""):
    return (before + '<div id="wrapper" class="item"><main id="main" class="item with_aside">'
            '<section class="infobox"><h1>' + TITLE + "</h1>" + info + "</section></main>"
            '<section id="detail_area"><section id="tab1" class="tab_contents prod_detail current">'
            '商品について</section><section id="tab2" class="tab_contents prod_detail"><h2>仕様詳細</h2>' +
            table(sku, claims) + extra_spec + "</section></section></div>" + after)


def ld(product):
    return '<script type="application/ld+json">' + json.dumps(product, ensure_ascii=False) + "</script>"


def product(**changes):
    return {"@type": "Product", "url": URL, "sku": SKU, "gtin13": JAN, **changes}


def page(body, url=URL, *, encoding="utf-8", status=200):
    return Page(url, body.encode(encoding), NOW, "saved_diagnosis", "text/html; charset=" + encoding, status)


class PrimaryJanTest(unittest.TestCase):
    def extract(self, body=None, url=URL):
        return extract_primary_jan("sofmap", page(markup() if body is None else body, url), CFG)

    def assert_unknown(self, result):
        self.assertIsNone(result["selected_value"])
        self.assertEqual("unknown", result["status"])

    def assert_conflict(self, result, reason):
        self.assertIsNone(result["selected_value"])
        self.assertEqual("conflict", result["status"])
        self.assertIn(reason, result["conflicts"])

    def test_retained_layout_invalid_skus_do_not_hide_later_valid_jan(self):
        for sku, jan in ((SKU, JAN), ("23812490", OTHER)):
            with self.subTest(sku=sku):
                self.assertIsNone(valid_jan(sku))
                p = page(markup(sku, (("JANコード", jan),)), URL.replace(SKU, sku), encoding="shift_jis")
                self.assertIsNone(parse_product("sofmap", p, CFG).jan)
                result = extract_primary_jan("sofmap", p, CFG)
                self.assertEqual("observed", result["status"])
                self.assertEqual(jan, result["selected_value"])
                self.assertEqual([], result["conflicts"])

    def test_evidence_proves_label_identity_scope_time_and_exact_input_hash(self):
        p = page(markup(), encoding="shift_jis")
        result = extract_primary_jan("sofmap", p, CFG)
        labels = [e for e in result["evidence"] if e["kind"] == "primary_product_label"]
        self.assertEqual(1, len(labels))
        proof = labels[0]
        self.assertEqual(("JANコード", JAN, SKU), (proof["raw_label"], proof["raw_value"], proof["product_id"]))
        self.assertEqual((URL, NOW, "saved_diagnosis"), (proof["url"], proof["observed_at"], proof["method"]))
        self.assertEqual(hashlib.sha256(p.body).hexdigest(), proof["body_sha256"])
        self.assertIn("/section/section[2]/table/tr[4]", proof["scope"])
        self.assertTrue(any(e["status"] == "identity_match" and e["raw_value"] == SKU for e in result["evidence"]))

    def test_uses_shared_checksum_validator_without_prefix_guessing(self):
        for label, jan in (("JAN", "96385074"), ("UPC code", "036000291452"),
                           ("EANコード", "4006381333931"), ("JANコード", JAN)):
            with self.subTest(label=label):
                with patch("monitor_lab.identity.valid_jan", wraps=valid_jan) as validator:
                    result = self.extract(markup(claims=((label, jan),)))
                self.assertEqual(jan, result["selected_value"])
                validator.assert_called_with(jan)

    def test_checksum_valid_thirteen_digit_sku_alone_is_never_a_jan(self):
        result = self.extract(markup(sku=JAN, claims=()), URL.replace(SKU, JAN))
        self.assert_unknown(result)
        self.assertEqual([], [e for e in result["evidence"] if e["status"] == "valid"])

    def test_search_url_query_image_and_arbitrary_page_text_never_supply_jan(self):
        body = markup(claims=(), after=f'<p>JANコード {JAN}</p><img src="/{JAN}.jpg">')
        self.assert_unknown(self.extract(body, URL + "&jan=" + JAN + "&q=" + JAN))
        self.assert_unknown(self.extract(markup(), "https://www.sofmap.com/search_result.aspx?sku=" + SKU))

    def test_missing_scope_or_product_number_prevents_table_adoption(self):
        for body in (table(), markup(sku=None), markup().replace('id="main"', 'id="other"'),
                     markup().replace('class="tab_contents prod_detail"', 'class="other"'),
                     markup().replace("<h1>", "<h2>").replace("</h1>", "</h2>")):
            with self.subTest(body=body[:80]):
                self.assert_unknown(self.extract(body))

    def test_mismatched_product_number_blocks_even_explicit_json_ld(self):
        result = self.extract(markup(sku="23812490", after=ld(product())))
        self.assert_conflict(result, "jan_primary_product_number_mismatch")

    def test_ambiguous_main_or_spec_scope_never_adopts(self):
        for extra in ('<main id="main" class="item"></main>', '<section id="tab2"></section>',
                      '<div id="wrapper"></div>', '<section id="detail_area"></section>'):
            with self.subTest(extra=extra):
                result = self.extract(markup(after=extra + ld(product())))
                self.assert_conflict(result, "jan_primary_scope_ambiguous")

    def test_foreign_recommendation_footer_and_unrelated_tables_are_excluded(self):
        decoy = table(claims=(("JANコード", OTHER),))
        body = markup(before=decoy, after="<footer>" + decoy + "</footer>",
                      extra_spec='<aside>' + decoy + '</aside><div class="recommend">' + decoy + '</div>' +
                      table(sku=None, claims=(("JANコード", OTHER),)))
        result = self.extract(body)
        self.assertEqual(JAN, result["selected_value"])
        self.assertFalse(any(e["raw_value"] == OTHER for e in result["evidence"]))

    def test_entire_layout_inside_recommendation_is_not_primary(self):
        self.assert_unknown(self.extract('<aside class="recommended">' + markup() + "</aside>"))

    def test_nested_table_is_not_flattened_into_primary_value(self):
        body = markup().replace("<td>" + JAN + "</td>", "<td>" + table(claims=(("JAN", OTHER),)) + "</td>")
        result = self.extract(body)
        self.assert_unknown(result)
        self.assertIn("jan_invalid_explicit_claim", result["conflicts"])

    def test_recommendation_inside_value_cannot_be_used_as_primary_digits(self):
        body = markup().replace("<td>" + JAN + "</td>", '<td><span class="recommend">' + JAN + "</span></td>")
        result = self.extract(body)
        self.assert_unknown(result)
        self.assertIn("jan_invalid_explicit_claim", result["conflicts"])

    def test_same_product_info_table_can_corroborate_but_conflicting_value_blocks(self):
        for jan in (JAN, OTHER):
            info = '<table class="infotable">' + row("EANコード", jan) + "</table>"
            result = self.extract(markup(info=info))
            if jan == JAN:
                self.assertEqual(JAN, result["selected_value"])
            else:
                self.assert_conflict(result, "jan_conflict_review_needed")

    def test_unrelated_info_tables_are_excluded(self):
        result = self.extract(markup(info=table(claims=(("JAN", OTHER),))))
        self.assertEqual(JAN, result["selected_value"])

    def test_duplicate_label_different_values_conflict_without_dictionary_overwrite(self):
        for values in ((JAN, OTHER), (OTHER, JAN)):
            result = self.extract(markup(claims=tuple(("JANコード", v) for v in values)))
            self.assert_conflict(result, "jan_conflict_review_needed")
            self.assertEqual(list(values), [e["raw_value"] for e in result["evidence"] if e["kind"] == "primary_product_label"])

    def test_duplicate_identical_labels_are_retained_and_agree(self):
        result = self.extract(markup(claims=(("JAN", JAN), ("JAN", JAN))))
        self.assertEqual(JAN, result["selected_value"])
        self.assertEqual(2, sum(e["kind"] == "primary_product_label" for e in result["evidence"]))

    def test_duplicate_product_numbers_cannot_hide_mismatch(self):
        result = self.extract(markup(extra_spec=table("23812490", (("JAN", OTHER),))))
        self.assert_conflict(result, "jan_primary_product_number_mismatch")

    def test_invalid_checksum_alone_unknown_and_not_silently_skipped_beside_valid(self):
        for bad in ("0195553309746", "4711289500125"):
            result = self.extract(markup(claims=(("JAN", bad),)))
            self.assert_unknown(result)
            self.assertIn("jan_invalid_explicit_claim", result["conflicts"])
            self.assertTrue(any(e.get("validation") == "invalid_checksum" for e in result["evidence"]))
            for claims in ((("JAN", bad), ("EAN", JAN)), (("JAN", JAN), ("JAN", bad))):
                self.assert_conflict(self.extract(markup(claims=claims)), "jan_invalid_explicit_claim")

    def test_malformed_claims_are_not_cleaned_into_valid_identifiers(self):
        for bad in ("", "JAN " + JAN, JAN + " / " + OTHER, "x" + JAN, "0195553 309745", "019555330974", JAN + "注記"):
            with self.subTest(bad=bad):
                result = self.extract(markup(claims=(("JANコード", bad),)))
                self.assert_unknown(result)
                self.assertIn("jan_invalid_explicit_claim", result["conflicts"])

    def test_empty_and_multicell_rows_cannot_hide_invalid_claim(self):
        for bad in ("<tr><th>JAN</th></tr>", "<tr><th>JAN</th><td>" + JAN + "</td><td>other</td></tr>"):
            result = self.extract(markup(claims=(("EAN", JAN),)).replace("</table>", bad + "</table>", 1))
            self.assert_conflict(result, "jan_invalid_explicit_claim")

    def test_explicit_json_ld_product_url_can_establish_primary_without_html_layout(self):
        result = self.extract(ld(product()))
        self.assertEqual(JAN, result["selected_value"])
        proof = next(e for e in result["evidence"] if e["status"] == "valid")
        self.assertEqual("explicit_url", proof["primary_basis"])

    def test_json_ld_offer_url_and_relative_primary_url(self):
        for p in ({"@type": "Product", "offers": {"url": URL}, "gtin13": JAN},
                  product(url="/product_detail.aspx?sku=" + SKU + "#product")):
            self.assertEqual(JAN, self.extract(ld(p))["selected_value"])

    def test_url_less_unique_product_needs_verified_sku_or_exact_name_context(self):
        for context in ({"sku": SKU}, {"name": TITLE}):
            p = {"@type": "Product", "gtin13": JAN, **context}
            self.assert_unknown(self.extract(ld(p)))
            result = self.extract(markup(claims=(), after=ld(p)))
            self.assertEqual(JAN, result["selected_value"])
            self.assertEqual("unique_verified_context", next(e for e in result["evidence"] if e["status"] == "valid")["primary_basis"])
        self.assert_unknown(self.extract(markup(claims=(), after=ld({"@type": "Product", "gtin13": JAN}))))

    def test_url_less_multiple_products_are_ambiguous_even_if_sku_matches(self):
        products = [{"@type": "Product", "sku": SKU, "gtin13": JAN},
                    {"@type": "Product", "sku": "23812490", "gtin13": OTHER}]
        result = self.extract(markup(claims=(), after=ld({"@graph": products})))
        self.assert_unknown(result)
        self.assertIn("json_ld_ambiguous_primary", result["reasons"])

    def test_explicit_current_url_disambiguates_other_product_nodes(self):
        result = self.extract(markup(claims=(), after=ld([product(), product(url=URL.replace(SKU, "23812490"), sku="23812490", gtin13=OTHER)])))
        self.assertEqual(JAN, result["selected_value"])
        self.assertTrue(any(e.get("reason") == "json_ld_wrong_product_url" for e in result["evidence"]))

    def test_wrong_url_json_ld_is_not_promoted_by_matching_sku_or_single_product(self):
        for url in (URL.replace(SKU, "23812490"), "https://other.example/?sku=" + SKU, "#product", ""):
            result = self.extract(markup(claims=(), after=ld(product(url=url))))
            self.assert_unknown(result)
            result = self.extract(markup(after=ld(product(url=url, gtin13=OTHER))))
            self.assertEqual(JAN, result["selected_value"])

    def test_recommendation_json_ld_not_adopted_even_with_current_url(self):
        for extra in ("<footer>" + ld(product(gtin13=OTHER)) + "</footer>",
                      '<div class="recommend">' + ld(product(gtin13=OTHER)) + "</div>",
                      ld({"@type": "ItemList", "itemListElement": [{"item": product(gtin13=OTHER)}]}),
                      ld({"@type": "Product", "url": URL, "isRelatedTo": product(gtin13=OTHER)})):
            result = self.extract(markup(after=extra))
            self.assertEqual(JAN, result["selected_value"])
            self.assertTrue(any(e.get("reason") == "json_ld_non_primary_context" for e in result["evidence"]))

    def test_duplicate_primary_products_with_different_explicit_gtins_conflict(self):
        self.assert_conflict(self.extract(ld([product(), product(gtin13=OTHER)])), "jan_conflict_review_needed")

    def test_duplicate_json_properties_preserve_all_claims_including_invalid(self):
        for value, reason in ((OTHER, "jan_conflict_review_needed"), ("bad", "jan_invalid_explicit_claim")):
            for first, second in ((JAN, value), (value, JAN)):
                raw = json.dumps(product(gtin13=first))[:-1] + ', "gtin13": "' + second + '"}'
                result = self.extract('<script type="application/ld+json">' + raw + '</script>')
                self.assert_conflict(result, reason)
                self.assertEqual([first, second], [e["raw_value"] for e in result["evidence"] if e["raw_label"] == "gtin13"])

    def test_duplicate_json_url_and_sku_properties_cannot_hide_wrong_product(self):
        for key, value, reason in (("url", URL.replace(SKU, "23812490"), "jan_json_ld_primary_url_conflict"),
                                   ("sku", "23812490", "jan_json_ld_product_number_mismatch")):
            raw = json.dumps(product())[:-1] + ', "' + key + '": "' + value + '"}'
            result = self.extract(markup(after='<script type="application/ld+json">' + raw + '</script>'))
            self.assert_conflict(result, reason)

    def test_conflicting_primary_label_and_json_ld_are_both_preserved(self):
        result = self.extract(markup(after=ld(product(gtin13=OTHER))))
        self.assert_conflict(result, "jan_conflict_review_needed")
        self.assertEqual({JAN, OTHER}, {e["value"] for e in result["evidence"] if e["status"] == "valid"})

    def test_primary_json_ld_invalid_claim_does_not_fall_back_to_valid_label(self):
        for value in ("0195553309746", "bad", "", None, int(JAN), [JAN], {"value": JAN}):
            with self.subTest(value=value):
                self.assert_conflict(self.extract(markup(after=ld(product(gtin13=value)))), "jan_invalid_explicit_claim")

    def test_json_ld_gtin_keys_validate_lengths_and_never_convert_gtin14(self):
        for key, value in (("gtin8", "96385074"), ("gtin12", "036000291452"), ("gtin13", JAN), ("gtin", JAN)):
            self.assertEqual(value, self.extract(ld({"@type": "Product", "url": URL, key: value}))["selected_value"])
        for p in (product(gtin13="036000291452"), {"@type": "Product", "url": URL, "gtin14": "0" + JAN}):
            result = self.extract(ld(p))
            self.assert_unknown(result)
            self.assertIn("jan_invalid_explicit_claim", result["conflicts"])

    def test_json_ld_sku_without_gtin_is_never_adopted(self):
        self.assert_unknown(self.extract(ld({"@type": "Product", "url": URL, "sku": JAN})))

    def test_conflicting_product_and_offer_urls_block_otherwise_valid_label(self):
        result = self.extract(markup(after=ld(product(offers={"url": URL.replace(SKU, "23812490")}))))
        self.assert_conflict(result, "jan_json_ld_primary_url_conflict")

    def test_current_url_json_ld_with_wrong_sku_is_not_primary(self):
        self.assert_conflict(self.extract(markup(after=ld(product(sku="23812490")))), "jan_json_ld_product_number_mismatch")

    def test_graph_and_webpage_mainentity_contexts_are_supported(self):
        for p in ({"@graph": [product()]}, {"@type": "WebPage", "mainEntity": product()}, product(**{"@type": ["Thing", "Product"]})):
            self.assertEqual(JAN, self.extract(ld(p))["selected_value"])

    def test_malformed_unrelated_json_ld_does_not_obscure_primary_label(self):
        result = self.extract(markup(after='<script type="application/ld+json">{"@type":"BreadcrumbList",}</script>'))
        self.assertEqual(JAN, result["selected_value"])
        self.assertTrue(any(e.get("reason") == "unreadable_json_ld" for e in result["evidence"]))

    def test_unreadable_product_json_ld_does_not_silently_fall_back(self):
        broken = ld(product()).replace('"gtin13": "' + JAN + '"', '"gtin13": invalid')
        self.assert_conflict(self.extract(markup(after=broken)), "jan_json_ld_product_unreadable")
        self.assertEqual(JAN, self.extract(markup(after='<aside>' + broken + '</aside>'))["selected_value"])

    def test_missing_empty_and_error_pages_are_unknown(self):
        for body in ("", "<html></html>", "<p>server unavailable</p>", markup(claims=())):
            self.assert_unknown(self.extract(body))
        self.assert_unknown(extract_primary_jan("sofmap", page(markup(), status=503), CFG))

    def test_invalid_product_urls_and_duplicate_query_ids_are_unknown(self):
        for url in (URL + "&sku=23812490", URL + "&product_id=23812490", URL.replace("sofmap.com", "sofmap.com.evil.example"),
                    URL.replace("product_detail.aspx", "error/error.aspx"), URL.replace(SKU, "")):
            self.assert_unknown(self.extract(markup(), url))

    def test_unsupported_stores_are_no_override(self):
        for store in ("ark", "koubou", "unknown"):
            self.assertIsNone(extract_primary_jan(store, page(markup()), CFG))

    def test_limits_fail_closed_without_adopting_already_seen_evidence(self):
        for name, bound, body in (("MAX_BODY_BYTES", 10, markup()), ("MAX_NODES", 5, markup()),
                                  ("MAX_ROWS", 1, markup()), ("MAX_EVIDENCE", 1, markup()),
                                  ("MAX_JSON_BYTES", 5, markup(after=ld(product()))),
                                  ("MAX_JSON_NODES", 1, markup(after=ld([product()]))),
                                  ("MAX_JSON_DEPTH", 0, markup(after=ld({"@graph": [product()]})))):
            with self.subTest(limit=name), patch("monitor_lab.identity." + name, bound):
                self.assert_unknown(self.extract(body))

    def test_helper_is_pure_and_does_not_modify_page_config_or_production_result(self):
        p, cfg = page(markup()), deepcopy(CFG)
        before_page, before_cfg = deepcopy(p), deepcopy(cfg)
        baseline = parse_product("sofmap", p, cfg).to_dict()
        with patch("builtins.open", side_effect=AssertionError("unexpected file I/O")), \
                patch("socket.socket", side_effect=AssertionError("unexpected network I/O")):
            result = extract_primary_jan("sofmap", p, cfg)
        self.assertEqual(JAN, result["selected_value"])
        self.assertEqual(before_page, p)
        self.assertEqual(before_cfg, cfg)
        self.assertEqual(baseline, parse_product("sofmap", p, cfg).to_dict())


if __name__ == "__main__":
    unittest.main()

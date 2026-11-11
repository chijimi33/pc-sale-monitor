"""Offline distinction between a restricted product page and a refused host."""
from copy import deepcopy
from http.client import IncompleteRead
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError

from sale_monitor.http import Client, FetchError, Page
from sale_monitor.models import Offer, iso
from sale_monitor.parsing import page_level_denial
from sale_monitor.retrying import transient
from sale_monitor.runner import Collector
from sale_monitor.storage import Store


HOST = "www.sofmap.com"
BASE = "https://" + HOST + "/product_detail.aspx?sku="
BODY = ("指定されたページはアクセスが制限されています\nError : 403 Forbidden.\n"
        "The requested URL was not found on this server.").encode()
CFG = {"stores": {"sofmap": {"adapter": "html", "seed_urls": [], "browser_fallback": True}}}


def response(url, body=BODY):
    result = MagicMock()
    result.__enter__.return_value = result
    result.url = url
    result.status = 403
    result.headers = {"Content-Type": "text/html; charset=utf-8"}
    result.read.return_value = body
    return result


class PageDenialTests(unittest.TestCase):
    def test_three_page_denials_leave_host_available_and_record_each_403(self):
        callback = Mock(return_value=True)
        client = Client(delay=0, page_denial=callback)
        for n in range(3):
            url = BASE + str(n)
            with patch.object(client.opener, "open", side_effect=HTTPError(
                    url, 403, "", {"Retry-After": "900"}, BytesIO(BODY))):
                with self.assertRaisesRegex(FetchError, "^page_access_restricted$") as caught:
                    client.get(url)
            self.assertEqual(caught.exception.page.status, 403)
            self.assertEqual(caught.exception.page.body, BODY)
            callback.assert_called_with(url, caught.exception.page)
        self.assertEqual(client.access_retry_after, {})
        self.assertEqual(client.access_failed_urls, {})
        self.assertEqual(client.refused_hosts, set())
        self.assertEqual(client.count, 3)
        evidence = client.evidence.snapshot()
        self.assertEqual(evidence["response_status_counts"]["http"], {"403": 3})
        self.assertTrue(all("host_retry_at_epoch_seconds" not in s for s in evidence["failure_samples"]))
        ok = response(BASE + "ok", b"ok")
        ok.status = 200
        with patch.object(client.opener, "open", return_value=ok):
            self.assertEqual(client.get(BASE + "ok").status, 200)

    def test_false_or_missing_callback_preserves_host_refusal_and_cooldown(self):
        for callback in (None, lambda url, page: False):
            with self.subTest(callback=callback), patch("sale_monitor.http.time.time", return_value=1000):
                client = Client(delay=0, page_denial=callback)
                for n in range(3):
                    url = BASE + str(n)
                    with patch.object(client.opener, "open", side_effect=HTTPError(url, 403, "", {}, BytesIO(BODY))):
                        with self.assertRaisesRegex(FetchError, "^http_403$"):
                            client.get(url)
                self.assertEqual(client.refused_hosts, {HOST})
                self.assertEqual(len(client.access_failed_urls[HOST]), 3)
                self.assertEqual(client.access_retry_after[HOST], {"until": 1300, "reason": "http_403"})
                with patch.object(client.opener, "open") as network:
                    with self.assertRaisesRegex(FetchError, "^access_retry_later:http_403$"):
                        client.get(BASE + "later")
                    network.assert_not_called()

    def test_page_denial_does_not_clear_prior_host_refusal_evidence(self):
        client = Client(delay=0, page_denial=lambda url, page: True)
        client.access_failed_urls[HOST] = {BASE + "host-denied"}
        client.refused_hosts.add(HOST)
        with patch.object(client.opener, "open", side_effect=HTTPError(BASE, 403, "", {}, BytesIO(BODY))):
            with self.assertRaisesRegex(FetchError, "^page_access_restricted$"):
                client.get(BASE)
        self.assertEqual(client.access_failed_urls[HOST], {BASE + "host-denied"})
        self.assertEqual(client.refused_hosts, {HOST})

    def test_returned_403_uses_classifier_before_host_accounting(self):
        for denied in (True, False):
            with self.subTest(denied=denied):
                client = Client(delay=0, page_denial=lambda url, page: denied)
                result = response(BASE)
                with patch.object(client.opener, "open", return_value=result):
                    reason = "page_access_restricted" if denied else "http_403"
                    with self.assertRaisesRegex(FetchError, "^" + reason + "$") as caught:
                        client.get(BASE)
                result.read.assert_called_once_with(200_000)
                self.assertEqual(caught.exception.page.status, 403)
                self.assertEqual(client.refused_hosts, set() if denied else {HOST})
                self.assertEqual(client.evidence.snapshot()["response_status_counts"]["http"], {"403": 1})

    def test_http_error_body_is_bounded_for_single_and_repeated_attempts(self):
        for attempts in (1, 3):
            with self.subTest(attempts=attempts):
                client = Client(delay=0, page_denial=lambda url, page: True)
                error = HTTPError(BASE, 403, "", {}, BytesIO(b"x" * 200_001))
                with patch.object(client.opener, "open", side_effect=error):
                    with self.assertRaises(FetchError) as caught:
                        client.get(BASE, max_attempts=attempts)
                self.assertEqual(len(caught.exception.page.body), 200_000)
                self.assertEqual(client.count, 1)

    def test_unreadable_403_body_is_empty_and_still_refuses_host(self):
        for returned in (False, True):
            for error in (OSError(), IncompleteRead(b"partial"), ValueError("closed stream")):
                with self.subTest(returned=returned, error=type(error).__name__):
                    client = Client(delay=0, page_denial=lambda url, page: page_level_denial("sofmap", url, page))
                    result = response(BASE)
                    result.read.side_effect = error
                    value = HTTPError(BASE, 403, "", {}, Mock(read=Mock(side_effect=error)))
                    kwargs = {"return_value": result} if returned else {"side_effect": value}
                    with patch.object(client.opener, "open", **kwargs):
                        with self.assertRaisesRegex(FetchError, "^http_403$") as caught:
                            client.get(BASE)
                    self.assertEqual(caught.exception.page.body, b"")
                    self.assertEqual(caught.exception.page.status, 403)
                    self.assertEqual(client.refused_hosts, {HOST})

    def test_missing_403_body_still_refuses_host(self):
        client = Client(delay=0, page_denial=lambda url, page: page_level_denial("sofmap", url, page))
        with patch.object(client.opener, "open", side_effect=HTTPError(BASE, 403, "", {}, None)):
            with self.assertRaisesRegex(FetchError, "^http_403$") as caught:
                client.get(BASE)
        self.assertEqual(caught.exception.page.body, b"")
        self.assertEqual(client.refused_hosts, {HOST})

    def test_classifier_requires_store_host_product_path_and_both_markers(self):
        cases = [
            ("sofmap", BASE, BODY, True),
            ("sofmap", BASE.replace("www.sofmap.com", "sofmap.com"), BODY, True),
            ("sofmap", BASE, "アクセスが制限されています".encode(), False),
            ("sofmap", BASE, b"The requested URL was not found on this server", False),
            ("sofmap", BASE, b"Access Denied", False),
            ("sofmap", BASE.replace("/product_detail.aspx", "/search.aspx"), BODY, False),
            ("sofmap", BASE.replace(HOST, "sofmap.com.example.test"), BODY, False),
            ("sofmap", BASE.replace(HOST, "example.test"), BODY, False),
            ("ark", BASE, BODY, False),
            ("koubou", BASE, BODY, False),
        ]
        for store, url, body, expected in cases:
            with self.subTest(store=store, url=url, body=body):
                self.assertEqual(page_level_denial(store, url, Page(url, body, iso(), status=403)), expected)
        page = Page(BASE, BODY.decode().encode("shift_jis"), iso(), status=403, content_type="text/html; charset=shift_jis")
        self.assertTrue(page_level_denial("sofmap", BASE, page))

    def test_collector_installs_callback_only_for_phases_and_preserves_custom_callback(self):
        for phase in (None, "discovery", "verification"):
            for custom in (None, lambda url, page: False):
                with self.subTest(phase=phase, custom=custom), tempfile.TemporaryDirectory() as directory:
                    client = Client(delay=0, page_denial=custom)
                    Collector(Path(directory), "sofmap", CFG, "run", client, phase=phase)
                    if phase is None or custom is not None:
                        self.assertIs(client.page_denial, custom)
                    else:
                        self.assertTrue(client.page_denial(BASE, Page(BASE, BODY, iso(), status=403)))
        with tempfile.TemporaryDirectory() as directory:
            client = Mock(spec=["count"])
            Collector(Path(directory), "sofmap", CFG, "run", client, phase="discovery")
            self.assertFalse(hasattr(client, "page_denial"))

    def test_collector_caches_page_failure_without_browser_or_same_run_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            client = Client(delay=0, browser=True, page_denial=lambda url, page: True)
            collector = Collector(Path(directory), "sofmap", CFG, "run", client)
            with patch.object(client.opener, "open", side_effect=HTTPError(BASE, 403, "", {}, BytesIO(BODY))) as network, patch.object(client, "rendered") as browser:
                for _ in range(2):
                    with self.assertRaisesRegex(FetchError, "^page_access_restricted$"):
                        collector.page(BASE)
            self.assertEqual(network.call_count, 1)
            browser.assert_not_called()
            self.assertNotIn(BASE, collector.page_failure_retry_at)
            self.assertFalse(transient("page_access_restricted"))

    def test_failed_product_retains_task_price_stock_and_original_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = Client(delay=0, browser=True)
            collector = Collector(root, "sofmap", CFG, "run", client, phase="discovery")
            old = Offer("sofmap", "100551046", BASE + "100551046", price_yen=5000,
                        stock="in_stock", observed_at="2026-10-01T00:00:00+00:00", observed_run_id="old")
            before = deepcopy(old.to_dict())
            collector.state["offers"][old.key] = before
            task_id = collector.enqueue({"type": "product", "url": old.url, "kind": "sale", "created_at": old.observed_at})
            with patch.object(client.opener, "open", side_effect=HTTPError(old.url, 403, "", {}, BytesIO(BODY))), patch.object(client, "rendered") as browser:
                result = collector.collect(1)
            expected = {**old.to_dict(), "issues": ["latest_fetch_failed"]}
            self.assertEqual(result["offers"][old.key], expected)
            self.assertEqual(result["queue"][task_id]["last_error"], "page_access_restricted")
            self.assertEqual(result["queue"][task_id]["attempts"], 1)
            self.assertEqual(result["queue"][task_id]["created_at"], old.observed_at)
            self.assertNotIn("retry_at_epoch_seconds", result["queue"][task_id])
            self.assertNotIn(task_id, result["done"])
            self.assertEqual(result["journal"], [])
            self.assertEqual(result["http_evidence"]["response_status_counts"]["http"], {"403": 1})
            self.assertEqual(client.refused_hosts, set())
            browser.assert_not_called()
            self.assertEqual(Store(root).load("stores/sofmap.json", {})["queue"], result["queue"])


if __name__ == "__main__":
    unittest.main()

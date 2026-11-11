from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError

from sale_monitor.budget import HostBudget, Robots
from sale_monitor.http import Client, FetchError, Page, SafeRedirect
from urllib.request import Request
from sale_monitor.runner import Collector


URL = "https://example.test/product"
HOST = "example.test"


class Clock:
    def __init__(self):
        self.now = 1000
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def response():
    result = MagicMock()
    result.__enter__.return_value = result
    result.url = URL
    result.status = 200
    result.headers = {}
    result.read.return_value = b"ok"
    return result


class HttpBudgetTests(TestCase):
    def setUp(self):
        self.clock = Clock()

    def budget(self, seconds=10):
        return HostBudget({}, seconds, sleep=self.clock.sleep, clock=self.clock)

    def test_exhaustion_has_zero_further_communication(self):
        budget = self.budget(2)
        client = Client(budget=budget)
        with patch.object(client.opener, "open", return_value=response()) as op:
            client.get(URL)
            with self.assertRaisesRegex(FetchError, "^host_budget_exhausted$"):
                client.get(URL)
        self.assertEqual(op.call_count, 1)
        self.assertEqual(client.count, 1)

    def test_retries_count_each_attempt_and_stop_at_limit(self):
        budget = self.budget(4)
        client = Client(budget=budget)
        with patch.object(client.opener, "open", side_effect=TimeoutError()) as op, patch("sale_monitor.http.time.sleep"):
            with self.assertRaisesRegex(FetchError, "host_budget_exhausted"):
                client.get(URL)
        self.assertEqual(op.call_count, 2)
        self.assertEqual(budget.snapshot(), {HOST: 2})

    def test_retries_can_recover_with_three_charges(self):
        budget = self.budget()
        client = Client(budget=budget)
        with patch.object(client.opener, "open", side_effect=[TimeoutError(), TimeoutError(), response()]), patch("sale_monitor.http.time.sleep"):
            self.assertEqual(client.get(URL).text, "ok")
        self.assertEqual(budget.snapshot(), {HOST: 3})

    def test_budget_pacing_does_not_double_wait(self):
        client = Client(delay=100, budget=self.budget())
        with patch.object(client.opener, "open", return_value=response()), patch("sale_monitor.http.time.sleep") as sleep:
            client.get(URL)
            client.get(URL)
        sleep.assert_not_called()
        self.assertEqual(self.clock.waits, [2])

    def test_learned_rate_spacing_is_preserved_without_changing_limit(self):
        budget = self.budget(20)
        client = Client(budget=budget)
        with patch.object(client.opener, "open", return_value=response()):
            client.get(URL)
            client.rate_limit_spacing[HOST] = 7
            client.get(URL)
        self.assertEqual(sum(self.clock.waits), 7)
        self.assertEqual(budget.limit(HOST), 10)

    def test_disallow_does_not_fetch_target_http_or_browser(self):
        robots = Robots(Mock(return_value=(200, "User-agent: PCSaleMonitor\nDisallow: /product")))
        budget = self.budget()
        client = Client(browser=True, robots=robots, budget=budget)
        with patch.object(client.opener, "open") as op, patch("playwright.sync_api.sync_playwright") as browser:
            for action in (client.get, client.rendered):
                with self.assertRaisesRegex(FetchError, "^robots_disallowed$"):
                    action(URL)
        op.assert_not_called()
        browser.assert_not_called()
        robots.fetch.assert_called_once()
        self.assertEqual(budget.snapshot(), {HOST: 1})

    def test_policy_charge_and_crawl_delay_applied_before_target(self):
        budget = self.budget(31)
        robots = Robots(Mock(return_value=(200, "User-agent: PCSaleMonitor\nCrawl-delay: 15")))
        client = Client(budget=budget, robots=robots)
        with patch.object(client.opener, "open", return_value=response()) as op:
            client.get(URL)
            with self.assertRaisesRegex(FetchError, "host_budget_exhausted"):
                client.get(URL)
        self.assertEqual(budget.snapshot(), {HOST: 2})
        self.assertEqual(budget.limit(HOST), 2)
        self.assertEqual(self.clock.waits, [15])
        op.assert_called_once()

    def test_forbidden_after_http_refusal_does_not_launch_browser(self):
        client = Client(browser=True, budget=self.budget())
        with patch.object(client.opener, "open", side_effect=HTTPError(URL, 403, "", {}, None)) as op:
            with self.assertRaisesRegex(FetchError, "^http_403$"):
                client.get(URL)
        with patch("playwright.sync_api.sync_playwright") as browser:
            with self.assertRaisesRegex(FetchError, "^browser_blocked_after_refusal$"):
                client.rendered(URL + "other")
        browser.assert_not_called()
        op.assert_called_once()
        self.assertEqual(client.budget.snapshot(), {HOST: 1})

    def test_browser_navigation_charged_and_refusal_persisted(self):
        client = Client(browser=True, budget=self.budget())
        with patch("playwright.sync_api.sync_playwright") as factory:
            browser = factory.return_value.__enter__.return_value.chromium.launch.return_value
            page = browser.new_page.return_value
            page.goto.return_value.status = 403
            page.goto.return_value.url = URL
            page.goto.return_value.header_value.return_value = None
            with self.assertRaisesRegex(FetchError, "^http_403$"):
                client.rendered(URL)
            with self.assertRaisesRegex(FetchError, "^browser_blocked_after_refusal$"):
                client.rendered(URL)
            page.goto.assert_called_once()
        self.assertEqual(client.budget.snapshot(), {HOST: 1})
        self.assertIn(HOST, client.refused_hosts)

    def test_collector_does_not_fallback_after_403_or_local_gate(self):
        cfg = {"stores": {"ark": {"seed_urls": [], "browser_fallback": True}}}
        for reason in ("http_403", "robots_disallowed", "host_budget_exhausted"):
            with tempfile.TemporaryDirectory() as directory:
                client = Mock()
                client.get.side_effect = FetchError(reason)
                collector = Collector(Path(directory), "ark", cfg, "test", client)
                with self.assertRaisesRegex(FetchError, reason):
                    collector.page(URL)
                client.rendered.assert_not_called()

    def test_collector_records_amazon_challenge_host(self):
        cfg = {"stores": {"amazon": {"seed_urls": [], "browser_fallback": True}}}
        with tempfile.TemporaryDirectory() as directory:
            client = Client(browser=True)
            collector = Collector(Path(directory), "amazon", cfg, "test", client)
            with patch.object(client, "get", side_effect=FetchError("amazon_access_challenge", Page(URL, b"challenge", "now"))):
                with self.assertRaisesRegex(FetchError, "amazon_access_challenge"):
                    collector.page(URL)
            self.assertIn(HOST, client.refused_hosts)

    def test_redirects_share_budget_and_check_destination_robots(self):
        budget = self.budget(4)
        robots = Robots(Mock(return_value=(404, "")), budget)
        client = Client(budget=budget, robots=robots)
        request = Request(URL)
        handler = SafeRedirect(client._before_redirect)
        handler.redirect_request(request, None, 302, "", {}, "https://other.test/next")
        self.assertEqual(budget.snapshot(), {"other.test": 2})
        with self.assertRaisesRegex(FetchError, "host_budget_exhausted"):
            handler.redirect_request(request, None, 302, "", {}, "https://other.test/next")

from unittest import TestCase
from unittest.mock import Mock

from sale_monitor.budget import HostBudget, Robots
from sale_monitor.http import FetchError


class Clock:
    def __init__(self):
        self.now = 0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class BudgetTests(TestCase):
    def test_limit_spacing_and_exhaustion(self):
        clock = Clock()
        budget = HostBudget({"default": {"min_interval_seconds": 2}}, 5, sleep=clock.sleep, clock=clock)
        budget.spend("a")
        budget.spend("a")
        with self.assertRaisesRegex(FetchError, "^host_budget_exhausted$"):
            budget.spend("a")
        self.assertEqual(clock.waits, [2])
        self.assertEqual(budget.snapshot(), {"a": 2})
        self.assertEqual(budget.remaining("a"), 0)
        budget.spend("b")
        self.assertEqual(clock.waits, [2])

    def test_resume_never_restores_usage(self):
        budget = HostBudget({}, 4, used={"a": 2})
        snapshot = budget.snapshot()
        resumed = HostBudget({}, 4, used=snapshot)
        snapshot["a"] = 0
        with self.assertRaisesRegex(FetchError, "host_budget_exhausted"):
            resumed.spend("a")
        self.assertEqual(budget.snapshot(), {"a": 2})

    def test_crawl_delay_controls_limit(self):
        budget = HostBudget({"default": {"min_interval_seconds": 2}, "b": {"min_interval_seconds": 20}}, 61)
        budget.set_crawl_delay("a", 15)
        budget.set_crawl_delay("b", 15)
        budget.set_crawl_delay("a", 1)
        self.assertEqual(budget.interval("a"), 15)
        self.assertEqual(budget.limit("a"), 4)
        self.assertEqual(budget.limit("b"), 3)

    def test_invalid_budget_values(self):
        for seconds in (-1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                HostBudget({}, seconds)
        with self.assertRaises(ValueError):
            HostBudget({}, 5, used={"a": -1})


class RobotsTests(TestCase):
    def test_disallow_token_cache_and_crawl_delay(self):
        fetch = Mock(return_value=(200, "User-agent: PCSaleMonitor\nDisallow: /private\nCrawl-delay: 15\n"))
        budget = HostBudget({}, 61)
        robots = Robots(fetch, budget)
        self.assertFalse(robots.allowed("https://a/private"))
        self.assertTrue(robots.allowed("https://a/public"))
        self.assertEqual(robots.crawl_delay("a"), 15)
        self.assertEqual(robots.status("a"), "available")
        self.assertEqual(budget.limit("a"), 4)
        self.assertEqual(budget.snapshot(), {"a": 1})
        fetch.assert_called_once_with("https://a/robots.txt")

    def test_unavailable_policy_denies_entire_run(self):
        for code in (401, 403, 500, 503):
            with self.subTest(code=code):
                fetch = Mock(return_value=(code, ""))
                robots = Robots(fetch)
                self.assertFalse(robots.allowed("https://a/x"))
                self.assertFalse(robots.allowed("https://a/y"))
                self.assertEqual(robots.status("a"), "unavailable")
                self.assertIsNone(robots.crawl_delay("a"))
                fetch.assert_called_once()

    def test_timeout_is_cached(self):
        for exc in (TimeoutError(), OSError(), ValueError()):
            fetch = Mock(side_effect=exc)
            robots = Robots(fetch)
            self.assertFalse(robots.allowed("https://a/x"))
            self.assertEqual(robots.status("a"), "unavailable")
            fetch.assert_called_once()

    def test_other_4xx_allows_all_and_cache_is_per_host(self):
        fetch = Mock(return_value=(404, "User-agent: *\nDisallow: /"))
        robots = Robots(fetch)
        self.assertTrue(robots.allowed("https://a/x"))
        self.assertTrue(robots.allowed("https://a/y"))
        self.assertTrue(robots.allowed("https://b/x"))
        self.assertEqual(fetch.call_count, 2)

    def test_budget_exhaustion_prevents_policy_fetch(self):
        fetch = Mock()
        robots = Robots(fetch, HostBudget({}, 0))
        self.assertFalse(robots.allowed("https://a/x"))
        fetch.assert_not_called()

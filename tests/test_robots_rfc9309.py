from unittest import TestCase
from unittest.mock import Mock

from sale_monitor.budget import HostBudget, Robots


TSUKUMO = """User-agent: Slurp
Crawl-delay: 10

User-Agent: bingbot
Crawl-delay: 10

User-agent: AhrefsBot
Disallow: /

User-agent: *
Disallow: /akinlist
Disallow: /mypage/

Disallow: /search/go
Disallow: /nav/
Disallow: /computer/

Disallow: /features/test_*
"""


class RobotsRFC9309Tests(TestCase):
    def policy(self, body, budget=None):
        return Robots(Mock(return_value=(200, body)), budget)

    def check_paths(self, body, allowed=(), denied=()):
        robots = self.policy(body)
        for expected, paths in ((True, allowed), (False, denied)):
            for path in paths:
                with self.subTest(path=path, allowed=expected):
                    self.assertEqual(robots.allowed("https://example.test" + path), expected)

    def test_wildcard_in_middle_and_at_end(self):
        self.check_paths("User-agent: *\nDisallow: /a/*/private\nDisallow: /test_*",
                         allowed=("/a/x/public", "/other/test_x", "/a/x/Private"),
                         denied=("/a//private", "/a/x/y/private/more", "/test_", "/test_x"))

    def test_end_anchor(self):
        self.check_paths("User-agent: *\nDisallow: /exact$\nDisallow: /*.pdf$",
                         allowed=("/exact/more", "/exact?q=1", "/x.pdf?download=1", "/x.pdf/more"),
                         denied=("/exact", "/exact#fragment", "/x.pdf", "/a/b.pdf"))

    def test_anchor_after_multiple_wildcards(self):
        self.check_paths("User-agent: *\nDisallow: /a*b*c$\nDisallow: /tail*$",
                         allowed=("/acb", "/abc/more", "/ab"),
                         denied=("/abc", "/abcbc", "/axybzc", "/tail", "/tail/more"))

    def test_non_trailing_dollar_and_regex_characters_are_literal(self):
        self.check_paths("User-agent: *\nDisallow: /a$b\nDisallow: /[x].(y)+",
                         allowed=("/axb", "/xay"), denied=("/a$b/more", "/[x].(y)+"))

    def test_longest_pattern_wins_in_either_order(self):
        for rules in (("Disallow: /private", "Allow: /private/public"),
                      ("Allow: /private/public", "Disallow: /private")):
            self.check_paths("User-agent: *\n" + "\n".join(rules),
                             allowed=("/private/public/x",), denied=("/private/x",))

    def test_longest_is_pattern_length_not_matched_url_length(self):
        self.check_paths("User-agent: *\nDisallow: /a*\nAllow: /abc",
                         allowed=("/abc/" + "x" * 100,), denied=("/axxx",))

    def test_allow_wins_equal_length_in_either_order(self):
        for rules in (("Disallow: /same", "Allow: /same"),
                      ("Allow: /same", "Disallow: /same"),
                      ("Disallow: /ab*", "Allow: /a*c")):
            self.check_paths("User-agent: *\n" + "\n".join(rules),
                             allowed=("/same", "/abc"))

    def test_percent_encoded_unreserved_on_both_sides(self):
        self.check_paths("User-agent: *\nDisallow: /%70rivate/~user/%41%2d%2E%5f",
                         allowed=("/private/~user/a-._",),
                         denied=("/private/~user/A-._", "/pr%69vate/%7euser/A%2D._"))

    def test_reserved_escapes_keep_encoding_and_normalise_hex(self):
        self.check_paths("User-agent: *\nDisallow: /a%2fb/%3f%23%2a%24$",
                         allowed=("/a/b/%3F%23%2A%24", "/a%252Fb/%3F%23%2A%24"),
                         denied=("/a%2Fb/%3F%23%2A%24", "/a%2fb/%3f%23%2a%24"))

    def test_percent_encoded_wildcard_and_anchor_are_literal(self):
        self.check_paths("User-agent: *\nDisallow: /literal%2a%24$",
                         allowed=("/literal*%24", "/literalabc", "/literal%2A%24/more"),
                         denied=("/literal%2A%24",))

    def test_normalised_equivalent_rules_tie(self):
        self.check_paths("User-agent: *\nDisallow: /%61\nAllow: /a",
                         allowed=("/a", "/%61"))

    def test_utf8_raw_and_escaped_paths_are_equivalent(self):
        for pattern in ("/caf\u00e9", "/caf%C3%A9"):
            self.check_paths("User-agent: *\nDisallow: " + pattern,
                             allowed=("/cafe",), denied=("/caf\u00e9", "/caf%c3%a9"))

    def test_query_is_matched_and_fragment_is_ignored(self):
        self.check_paths("User-agent: *\nDisallow: /search?keyword=%61$",
                         allowed=("/search", "/search?keyword=b", "/search?keyword=a&x=1"),
                         denied=("/search?keyword=a", "/search?keyword=%61#ignored"))

    def test_empty_query_and_empty_url_path(self):
        self.check_paths("User-agent: *\nDisallow: /?", allowed=("",), denied=("?", "?x=1"))

    def test_specific_group_overrides_wildcard_case_insensitively(self):
        self.check_paths("User-agent: *\nDisallow: /\nCrawl-delay: 99\n\n"
                         "uSeR-aGeNt: pCsAlEmOnItOr\nDisallow: /private",
                         allowed=("/public",), denied=("/private",))

    def test_multiple_specific_groups_are_merged(self):
        self.check_paths("User-agent: PCSaleMonitor\nDisallow: /one\nDisallow: /shared\n\n"
                         "User-agent: OtherBot\nDisallow: /other\n\n"
                         "User-agent: pcsalemonitor\nDisallow: /two\nAllow: /shared",
                         allowed=("/other", "/shared"), denied=("/one", "/two"))

    def test_multiple_wildcard_groups_are_merged(self):
        self.check_paths("User-agent: *\nDisallow: /one\n\n"
                         "User-agent: *\nDisallow: /two",
                         allowed=("/public",), denied=("/one", "/two"))

    def test_multiple_agent_lines_share_rules(self):
        self.check_paths("User-agent: OtherBot\n\nUser-agent: PCSaleMonitor\nDisallow: /private",
                         allowed=("/public",), denied=("/private",))

    def test_agent_product_token_must_match_exactly(self):
        self.check_paths("User-agent: SaleMonitor\nDisallow: /\n\n"
                         "User-agent: PCSaleMonitorExtra\nDisallow: /\n\n"
                         "User-agent: *\nDisallow: /private",
                         allowed=("/public",), denied=("/private",))

    def test_no_applicable_group_allows_everything(self):
        for body in ("", "User-agent: OtherBot\nDisallow: /", "Disallow: /"):
            self.check_paths(body, allowed=("/", "/private", "/robots.txt"))

    def test_empty_disallow_adds_no_restriction(self):
        self.check_paths("User-agent: *\nDisallow:", allowed=("/", "/anything"))
        self.check_paths("User-agent: *\nDisallow:\nDisallow: /private",
                         allowed=("/public",), denied=("/private",))

    def test_empty_specific_group_still_overrides_wildcard(self):
        self.check_paths("User-agent: *\nDisallow: /\n\nUser-agent: PCSaleMonitor\nDisallow:",
                         allowed=("/", "/anything"))

    def test_robots_txt_is_implicitly_allowed(self):
        self.check_paths("User-agent: *\nDisallow: /",
                         allowed=("/robots.txt", "/robots.txt?q=1", "/%72obots.txt"),
                         denied=("/robots.txt/extra", "/ROBOTS.TXT"))

    def test_comments_bom_unknown_lines_and_blank_lines(self):
        self.check_paths("\ufeffUser-agent: * # comment\nDisallow: /one # comment\n\n"
                         "invalid\nSitemap: https://example.test/map.xml\nDisallow: /two\n"
                         "Disallow: invalid-pattern\nDisallow: /hash%23value",
                         allowed=("/public",), denied=("/one", "/two", "/hash%23value"))

    def test_crawl_delay_uses_selected_specific_groups(self):
        budget = HostBudget({}, 100)
        robots = self.policy("User-agent: *\nCrawl-delay: 99\n\n"
                             "User-agent: PCSaleMonitor\nCrawl-delay: 2.5\n\n"
                             "User-agent: pcsalemonitor\nCrawl-delay: 7", budget)
        self.assertEqual(robots.crawl_delay("example.test"), 7)
        self.assertEqual(budget.interval("example.test"), 7)
        self.assertEqual(budget.snapshot(), {"example.test": 1})

    def test_crawl_delay_falls_back_to_wildcard(self):
        budget = HostBudget({}, 100)
        robots = self.policy("User-agent: OtherBot\nCrawl-delay: 99\n\n"
                             "User-agent: *\nCrawl-delay: 3.5", budget)
        self.assertEqual(robots.crawl_delay("example.test"), 3.5)
        self.assertEqual(budget.interval("example.test"), 3.5)

    def test_invalid_crawl_delays_are_ignored(self):
        for delay in ("later", "", "NaN", "inf", "-1"):
            with self.subTest(delay=delay):
                robots = self.policy("User-agent: *\nCrawl-delay: 4\nCrawl-delay: " + delay)
                self.assertEqual(robots.crawl_delay("example.test"), 4)
        robots = self.policy("User-agent: *\nCrawl-delay: later")
        self.assertTrue(robots.allowed("https://example.test/"))
        self.assertIsNone(robots.crawl_delay("example.test"))

    def test_specific_group_does_not_inherit_wildcard_delay(self):
        robots = self.policy("User-agent: *\nCrawl-delay: 99\n\n"
                             "User-agent: PCSaleMonitor\nDisallow: /private")
        self.assertIsNone(robots.crawl_delay("example.test"))

    def test_zero_delay_and_non_selected_delay(self):
        robots = self.policy("User-agent: PCSaleMonitor\nCrawl-delay: 0")
        self.assertEqual(robots.crawl_delay("example.test"), 0)
        robots = self.policy("User-agent: OtherBot\nCrawl-delay: 10")
        self.assertIsNone(robots.crawl_delay("example.test"))

    def test_tsukumo_saved_policy(self):
        self.check_paths(TSUKUMO, allowed=("/features/sale", "/goods/123", "/search?keyword=a"),
                         denied=("/features/test_x", "/search/go", "/nav/x", "/computer/x",
                                 "/mypage/", "/akinlist"))
        self.assertIsNone(self.policy(TSUKUMO).crawl_delay("example.test"))

    def test_pipeline_cache_transfer_needs_no_fetch_or_budget_spend(self):
        responses = {"example.test": [200, "User-agent: *\nDisallow: /test_*\nCrawl-delay: 7"]}
        cached = Robots(lambda url: responses["example.test"])
        self.assertTrue(cached.allowed("https://example.test/"))
        budget = HostBudget({}, 100)
        budget.set_crawl_delay("example.test", cached.crawl_delay("example.test"))
        fetch = Mock(side_effect=AssertionError("unexpected fetch"))
        robots = Robots(fetch, budget)
        robots.cache.update(cached.cache)
        robots.responses = responses
        self.assertFalse(robots.allowed("https://example.test/test_x"))
        self.assertEqual(robots.crawl_delay("example.test"), 7)
        self.assertEqual(robots.status("example.test"), "available")
        self.assertEqual(budget.snapshot(), {})
        fetch.assert_not_called()

    def test_loading_errors_and_http_statuses_keep_host_semantics(self):
        for status in (401, 403, 500, 503, 302):
            with self.subTest(status=status):
                robots = Robots(Mock(return_value=(status, "User-agent: *\nAllow: /")))
                self.assertFalse(robots.allowed("https://example.test/robots.txt"))
                self.assertEqual(robots.status("example.test"), "unavailable")
        for status in (404, 410, 429):
            with self.subTest(status=status):
                robots = Robots(Mock(return_value=(status, "User-agent: *\nDisallow: /")))
                self.assertTrue(robots.allowed("https://example.test/private"))
                self.assertIsNone(robots.crawl_delay("example.test"))
        robots = Robots(Mock(return_value=(200, None)))
        self.assertFalse(robots.allowed("https://example.test/robots.txt"))
        self.assertEqual(robots.status("example.test"), "unavailable")

    def test_budget_delay_failure_denies_whole_host(self):
        budget = Mock(spec=HostBudget)
        budget.set_crawl_delay.side_effect = ValueError("failed")
        robots = self.policy("User-agent: *\nCrawl-delay: 3", budget)
        self.assertFalse(robots.allowed("https://example.test/public"))
        self.assertEqual(robots.status("example.test"), "unavailable")

    def test_pattern_starting_with_wildcard(self):
        self.check_paths("User-agent: *\nDisallow: *?sort=\nDisallow: *.pdf$",
                         allowed=("/list", "/x.pdf/more"),
                         denied=("/list?sort=price", "/a/b.pdf"))

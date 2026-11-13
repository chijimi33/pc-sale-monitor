import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

from tools.robots_diff import compare, find_urls, main


class RobotsDiffTests(TestCase):
    def write_store(self, root, name, state):
        path = Path(root) / "stores" / (name + ".json")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
        return path

    def test_find_urls_in_nested_values_and_dictionary_keys(self):
        state = {"queue": [{"url": "https://a/queued"}], "offers": {"https://a/key": {}},
                 "verification": {"results": ["https://a/verified"]},
                 "done": ["https://a/done", "opaque-id"], "invalid": ["http://[", None, 42]}
        self.assertEqual(set(find_urls(state)), {"https://a/queued", "https://a/key",
                                                 "https://a/verified", "https://a/done"})

    def test_comparison_is_offline_and_read_only(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.write_store(root, "test", {
                "robots": {"responses": {"a": [200, "User-agent: *\nDisallow: /test_*"]}},
                "queue": [{"url": "https://a/test_x"}],
                "offers": {"one": {"url": "https://a/public"}},
                "verification_queue": [{"url": "https://a/test_x"}],
                "verification_results": {"https://a/test_y": {}},
                "done": ["https://a/test_z"]})
            before = hashlib.sha256(path.read_bytes()).digest(), path.stat().st_mtime_ns
            with patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
                    patch("urllib.robotparser.RobotFileParser.read", side_effect=AssertionError("network")):
                summary = compare(root)
            self.assertEqual(before, (hashlib.sha256(path.read_bytes()).digest(), path.stat().st_mtime_ns))
            self.assertEqual(summary["stores_read"], 1)
            self.assertEqual(summary["missing_robots_responses"], {})
            host = summary["hosts"]["a"]
            self.assertEqual(host["urls_checked"], 4)
            self.assertEqual(host["differences_count"], 3)
            self.assertEqual([row["url"] for row in host["differences"]],
                             ["https://a/test_x", "https://a/test_y", "https://a/test_z"])
            for row in host["differences"]:
                self.assertTrue(row["old_allowed"])
                self.assertFalse(row["new_allowed"])

    def test_differences_are_capped_but_every_url_is_checked(self):
        with tempfile.TemporaryDirectory() as root:
            for store in ("one", "two"):
                self.write_store(root, store, {
                    "robots": {"responses": {"a": [200, "User-agent: *\nDisallow: /test_*"]}},
                    "done": [f"https://a/test_{i}" for i in range(60)]})
            host = compare(root)["hosts"]["a"]
            self.assertEqual(host["urls_checked"], 120)
            self.assertEqual(host["differences_count"], 120)
            self.assertEqual(len(host["differences"]), 50)

    def test_missing_policies_are_reported_without_assuming_allow_or_deny(self):
        with tempfile.TemporaryDirectory() as root:
            self.write_store(root, "one", {"queue": ["https://a/x"]})
            self.write_store(root, "two", {
                "robots": {"responses": {"b": [404, ""], "unused": [200, ""]}},
                "queue": ["https://a/x", "https://b/x"]})
            result = compare(root)
            self.assertEqual(result["stores_without_robots_responses"], ["one"])
            self.assertEqual(result["missing_robots_responses"],
                             {"a": {"stores": ["one", "two"], "urls_skipped": 2}})
            self.assertEqual(result["hosts"]["a"]["urls_checked"], 0)
            self.assertEqual(result["hosts"]["b"]["urls_checked"], 1)
            self.assertEqual(result["hosts"]["unused"]["urls_checked"], 0)

    def test_http_statuses_match_existing_loading_semantics(self):
        with tempfile.TemporaryDirectory() as root:
            responses = {f"host{code}": [code, "User-agent: *\nDisallow: /test_*"]
                         for code in (200, 302, 401, 403, 404, 429, 500, 503)}
            responses["broken"] = [200, None]
            self.write_store(root, "test", {"robots": {"responses": responses},
                                             "done": [f"https://{host}/test_x" for host in responses]})
            result = compare(root)
            for host, summary in result["hosts"].items():
                self.assertEqual(summary["urls_checked"], 1)
                self.assertEqual(summary["differences_count"], int(host == "host200"))

    def test_sitemap_urls_in_policy_bodies_are_not_tasks(self):
        with tempfile.TemporaryDirectory() as root:
            self.write_store(root, "test", {
                "robots": {"responses": {"a": [200, "https://sitemap.test/map.xml"]}},
                "done": ["https://a/x"]})
            self.assertEqual(set(compare(root)["hosts"]), {"a"})

    def test_command_line_prints_json_and_rejects_empty_state(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                compare(root)
            self.write_store(root, "test", {"robots": {"responses": {"a": [404, ""]}},
                                             "done": ["https://a/x"]})
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                main(["--state", root])
            self.assertEqual(json.loads(output.getvalue())["hosts"]["a"]["urls_checked"], 1)

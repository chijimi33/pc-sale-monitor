"""The CI helper that names failing tests in annotations."""
import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci_test_failures.py"
spec = importlib.util.spec_from_file_location("ci_test_failures", SCRIPT)
ci_test_failures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_test_failures)

RULE, LINE = "=" * 70, "-" * 70
REPORT = "\n".join([
    "test_ok (m.T.test_ok) ... ok", "test_bad (m.T.test_bad) ... FAIL", "test_err (m.T.test_err) ... ERROR", "",
    RULE, "ERROR: test_err (m.T.test_err)", LINE, "Traceback (most recent call last):", "RuntimeError: boom", "",
    RULE, "FAIL: test_bad (m.T.test_bad)", LINE, "Traceback (most recent call last):",
    "AssertionError: 'a,b::c 100%' != 'x'", "", LINE, "Ran 3 tests in 0.001s", "", "FAILED (failures=1, errors=1)", ""])


class CiTestFailures(unittest.TestCase):
    def run_main(self, text):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tests.log"
            path.write_text(text, encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                ci_test_failures.main(str(path))
        return output.getvalue().splitlines()

    def test_each_failure_becomes_one_escaped_error_line(self):
        self.assertEqual(list(ci_test_failures.failures(REPORT)), [
            ("ERROR: test_err (m.T.test_err)", "Traceback (most recent call last):\nRuntimeError: boom"),
            ("FAIL: test_bad (m.T.test_bad)",
             "Traceback (most recent call last):\nAssertionError: 'a,b::c 100%' != 'x'")])
        lines = self.run_main(REPORT)
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "::error title=ERROR: test_err (m.T.test_err)::"
                                   "Traceback (most recent call last):%0ARuntimeError: boom")
        self.assertTrue(lines[1].endswith("AssertionError: 'a,b::c 100%25' != 'x'"))
        self.assertTrue(lines[2].startswith("::notice title=test environment::2 failing test(s); "))

    def test_run_without_report_shows_the_end_of_the_log(self):
        lines = self.run_main("collecting\nKilled\n")
        self.assertEqual(lines[0], "::error title=test run ended without a failure report::collecting%0AKilled%0A")
        self.assertTrue(lines[1].startswith("::notice title=test environment::0 failing test(s); "))

    def test_at_most_eight_errors(self):
        many = "".join("%s\nFAIL: t%d (m.T.t%d)\n%s\nx\n\n" % (RULE, i, i, LINE) for i in range(12))
        lines = self.run_main(many + LINE + "\nRan 12 tests in 0.001s\n")
        self.assertEqual(sum(line.startswith("::error ") for line in lines), 8)
        self.assertIn("12 failing test(s)", lines[-1])


if __name__ == "__main__":
    unittest.main()

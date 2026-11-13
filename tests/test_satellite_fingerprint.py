from importlib import metadata
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sale_monitor.satellite.fingerprint import code_fingerprint, dependency_status


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.package = self.root / "sale_monitor"
        self.package.mkdir()
        self.project = self.root / "pyproject.toml"
        self.project.write_text('[project]\ndependencies = ["Thing==1.2"]\n', encoding="utf-8")

    def fingerprint(self):
        return code_fingerprint(self.package, self.project)

    def test_portable_lines_paths_and_recursive_order(self):
        (self.package / "z.py").write_bytes(b"a\r\nb\rc\n")
        (self.package / "nested").mkdir()
        (self.package / "nested/a.py").write_bytes(b"x = 1\n")
        first = self.fingerprint()
        (self.package / "z.py").write_bytes(b"a\nb\nc\n")
        (self.package / "__pycache__").mkdir()
        (self.package / "__pycache__/ignore.py").write_bytes(b"ignored")
        (self.package / "README.txt").write_bytes(b"ignored")
        self.assertEqual(first, self.fingerprint())
        self.assertEqual(first["files"], 2)
        for file in ("z.py", "nested/a.py"):
            original = (self.package / file).read_bytes()
            (self.package / file).write_bytes(original + b"# change\n")
            self.assertNotEqual(first, self.fingerprint())
            (self.package / file).write_bytes(original)
        self.project.write_text('[project]\ndependencies = ["Thing==1.3"]', encoding="utf-8")
        self.assertNotEqual(first, self.fingerprint())

    def test_dependencies_match_missing_wrong_and_unsupported(self):
        with patch("sale_monitor.satellite.fingerprint.metadata.version", return_value="1.2"):
            self.assertTrue(dependency_status(self.project)["ok"])
        with patch("sale_monitor.satellite.fingerprint.metadata.version", return_value="1.3"):
            self.assertEqual(dependency_status(self.project)["mismatches"], ["thing"])
        with patch("sale_monitor.satellite.fingerprint.metadata.version", side_effect=metadata.PackageNotFoundError):
            self.assertIsNone(dependency_status(self.project)["installed"]["thing"])
        self.project.write_text('[project]\ndependencies = ["thing>=1", "thing[x]==1", "thing==1; os_name==1"]', encoding="utf-8")
        result = dependency_status(self.project)
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["mismatches"]), 3)

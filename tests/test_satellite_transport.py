"""Git plumbing contracts, using only a temporary local bare repository."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sale_monitor.satellite.testing import MemoryMailbox, mailbox_pair
from sale_monitor.satellite.transport import GitMailbox, TransportError


class GitTransportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", "--quiet", str(self.repo)], check=True, capture_output=True)
        self.coord = self.mailbox("coord", "relay", "satellite", "")
        self.pc = self.mailbox("pc", "satellite", "relay", "")

    def mailbox(self, name, write, read, start):
        return GitMailbox(str(self.repo), write, read, self.root / name, start_after=start)

    def test_roundtrip_raw_bytes_and_exact_tree_both_directions(self):
        for sender, reader in ((self.coord, self.pc), (self.pc, self.coord)):
            old = sender.post({"old": b"discard"}, "old")
            files = {"input/a.json": b'{\r\n"a":1}\r\n', "binary": bytes(range(256))}
            new = sender.post(files, "new")
            self.assertEqual(reader.poll(), [old, new])
            self.assertEqual(reader.position(), new)
            self.assertEqual(reader.poll(), [])
            for path, body in files.items():
                self.assertEqual(reader.read(new, path), body)
            with self.assertRaises(KeyError):
                reader.read(new, "old")
            paths = subprocess.run(["git", "--git-dir", str(self.repo), "ls-tree", "-r", "--name-only", new],
                                   check=True, capture_output=True).stdout.decode().splitlines()
            self.assertEqual(set(paths), set(files))

    def test_ordering_start_after_baseline_and_missing_branch(self):
        self.assertEqual(self.pc.poll(), [])
        commits = [self.coord.post({"x": str(i).encode()}, str(i)) for i in range(4)]
        self.assertEqual(self.pc.poll(), commits)
        self.assertEqual(self.mailbox("resume", "satellite", "relay", commits[1]).poll(), commits[2:])
        all_reader = self.mailbox("all", "satellite", "relay", "")
        self.assertEqual(all_reader.poll(), commits)
        new_reader = self.mailbox("baseline", "satellite", "relay", None)
        self.assertEqual(new_reader.poll(), [])
        self.assertIsNone(new_reader.position())
        next_commit = self.coord.post({}, "empty")
        self.assertEqual(new_reader.poll(), [next_commit])
        self.assertEqual(self.mailbox("missing", "satellite", "missing", None).poll(), [])

    def test_read_is_restricted_to_read_branch_and_safe_paths(self):
        commit = self.coord.post({"x": b"x"}, "one")
        with self.assertRaises(TransportError):
            self.coord.read(commit, "x")
        for path in ("../x", "/x", "a/../x", "x\\y", "a//b"):
            with self.subTest(path=path), self.assertRaises(TransportError):
                self.coord.post({path: b"x"}, "invalid")

    def test_every_command_has_timeout_and_errors_hide_credentials(self):
        with patch("sale_monitor.satellite.transport.subprocess.run", side_effect=subprocess.TimeoutExpired("SECRET", 1)) as run:
            with self.assertRaisesRegex(TransportError, "timed out") as caught:
                self.coord.poll()
            self.assertEqual(run.call_args.kwargs["timeout"], 60)
            self.assertNotIn("SECRET", str(caught.exception))

    def test_retry_non_fast_forward_once_without_force(self):
        other = self.mailbox("other", "relay", "satellite", "")
        original = self.coord._git
        pushes = []

        def race(*args, **kwargs):
            if args[0] == "push":
                pushes.append(args)
                if len(pushes) == 1:
                    other.post({"race": b"winner"}, "racing writer")
            return original(*args, **kwargs)

        with patch.object(self.coord, "_git", side_effect=race):
            commit = self.coord.post({"ours": b"payload"}, "retry")
        self.assertEqual(len(pushes), 2)
        self.assertTrue(all("--force" not in args for args in pushes))
        self.assertEqual(self.pc.poll()[-1], commit)
        self.assertEqual(self.pc.read(commit, "ours"), b"payload")

    def test_auth_header_and_fixed_remote(self):
        mailbox = GitMailbox(str(self.repo), "r", "s", self.root / "auth",
                             env={"PATH": __import__("os").environ["PATH"], "GITHUB_TOKEN": "secret"})
        self.assertEqual(mailbox.env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        self.assertNotIn("secret", (mailbox.workdir / ".git/config").read_text())
        with self.assertRaises(TransportError):
            GitMailbox("different", "r", "s", mailbox.workdir)


class MemoryTransportTests(unittest.TestCase):
    def test_pair_and_resume_contract(self):
        a, b = mailbox_pair()
        first = a.post({"x": b"\r\n"}, "first")
        second = a.post({}, "second")
        self.assertEqual(b.poll(), [first, second])
        self.assertEqual(b.read(first, "x"), b"\r\n")
        self.assertEqual(b.poll(), [])
        self.assertEqual(MemoryMailbox(b._write, b._read, start_after=first).poll(), [second])
        baseline = MemoryMailbox(b._write, b._read, start_after=None)
        self.assertEqual(baseline.poll(), [])
        third = a.post({}, "third")
        self.assertEqual(baseline.poll(), [third])

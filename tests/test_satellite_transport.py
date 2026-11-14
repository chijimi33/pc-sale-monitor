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
        self.assertEqual(new_reader.poll(), [commits[-1]])
        self.assertEqual(new_reader.position(), commits[-1])
        self.assertEqual(new_reader.poll(), [])
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

    def assert_push(self, args, kwargs):
        self.assertNotIn("--force", args)
        self.assertEqual(kwargs["allowed"], (0, 1))

    def rejected(self, args):
        return subprocess.CompletedProcess(args, 1,
            b"!\t<src>:refs/heads/x\t[remote rejected] (incorrect old value provided)\n", b"SECRET")

    def race_post(self, *, missing=False, server=False):
        initial = [] if missing else [self.coord.post({"initial": b"x"}, "initial")]
        other = self.mailbox("other", "relay", "satellite", "")
        original = self.coord._git
        pushes, winners = [], []

        def race(*args, **kwargs):
            if args[0] == "push":
                self.assert_push(args, kwargs)
                pushes.append(args)
                if len(pushes) == 1:
                    winners.append(other.post({"race": b"winner"}, "racing writer"))
                    if server:
                        return self.rejected(args)
            return original(*args, **kwargs)

        with patch.object(self.coord, "_git", side_effect=race):
            commit = self.coord.post({"ours": b"payload"}, "retry")
        self.assertEqual(len(pushes), 2)
        self.assertEqual(self.pc.poll(), initial + winners + [commit])
        self.assertEqual(self.coord._fetch("relay"), commit)
        self.assertEqual(self.pc.read(commit, "ours"), b"payload")

    def test_retry_client_rejection_without_force(self):
        self.race_post()

    def test_retry_server_rejection_without_force(self):
        self.race_post(server=True)

    def test_rejection_with_unchanged_head_is_not_retried(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    self.coord.post({"initial": b"x"}, "initial")
                original = self.coord._git
                pushes = []

                def reject(*args, **kwargs):
                    if args[0] == "push":
                        self.assert_push(args, kwargs)
                        pushes.append(args)
                        return self.rejected(args)
                    return original(*args, **kwargs)

                with patch.object(self.coord, "_git", side_effect=reject):
                    with self.assertRaisesRegex(TransportError, "^git push failed$"):
                        self.coord.post({"ours": b"payload"}, "rejected")
                self.assertEqual(len(pushes), 1)

    def test_continuous_races_stop_after_five_pushes(self):
        other = self.mailbox("other", "relay", "satellite", "")
        original = self.coord._git
        pushes, winners = [], []

        def race(*args, **kwargs):
            if args[0] == "push":
                self.assert_push(args, kwargs)
                pushes.append(args)
                winners.append(other.post({"race": str(len(pushes)).encode()}, "racing writer"))
            return original(*args, **kwargs)

        with patch.object(self.coord, "_git", side_effect=race):
            with self.assertRaisesRegex(TransportError, "^git push failed$"):
                self.coord.post({"ours": b"payload"}, "retry")
        self.assertEqual(len(pushes), 5)
        self.assertEqual(self.pc.poll(), winners)

    def test_retry_race_on_missing_branch_without_force(self):
        self.race_post(missing=True)

    def accepted_post(self, *, descendant=False, attempt=1):
        other = self.mailbox("other", "relay", "satellite", "")
        original = self.coord._git
        pushes, winners = [], []

        def report_failure(*args, **kwargs):
            if args[0] == "push":
                self.assert_push(args, kwargs)
                pushes.append(args)
                if len(pushes) < attempt:
                    winners.append(other.post({"race": str(len(pushes)).encode()}, "racing writer"))
                    return self.rejected(args)
                result = original(*args, **kwargs)
                self.assertEqual(result.returncode, 0)
                if descendant:
                    winners.append(other.post({"later": b"D"}, "later writer"))
                return subprocess.CompletedProcess(args, 1, b"", b"SECRET")
            return original(*args, **kwargs)

        with patch.object(self.coord, "_git", side_effect=report_failure):
            commit = self.coord.post({"ours": b"payload"}, "accepted")
        self.assertEqual(len(pushes), attempt)
        self.assertEqual(commit, pushes[-1][-1].split(":")[0])
        history = self.pc.poll()
        expected = [commit, *winners] if descendant else [*winners, commit]
        self.assertEqual(history, expected)
        payloads = []
        for sha in history:
            try:
                payloads.append(self.pc.read(sha, "ours"))
            except KeyError:
                pass
        self.assertEqual(payloads, [b"payload"])
        self.assertEqual(self.coord._fetch("relay"), expected[-1])

    def test_accepted_push_reporting_failure_returns_commit_once(self):
        self.accepted_post()

    def test_accepted_push_reporting_failure_returns_ancestor_not_head(self):
        self.accepted_post(descendant=True)

    def test_fifth_push_accepted_reporting_failure_returns_commit(self):
        self.accepted_post(attempt=5)

    def test_post_git_exceptions_are_not_retried(self):
        for failure in ("push", "ls-remote", "merge-base"):
            with self.subTest(failure=failure):
                self.coord.post({"initial": b"x"}, "initial")
                original = self.coord._git
                pushes = []

                def fail(*args, **kwargs):
                    if args[0] == "push":
                        self.assert_push(args, kwargs)
                        pushes.append(args)
                        if failure == "push":
                            raise TransportError("git command unavailable or timed out")
                        return self.rejected(args)
                    if pushes and args[0] == failure:
                        raise TransportError("git command unavailable or timed out")
                    return original(*args, **kwargs)

                with patch.object(self.coord, "_git", side_effect=fail):
                    with self.assertRaisesRegex(TransportError, "^git command unavailable or timed out$"):
                        self.coord.post({"ours": b"payload"}, "failure")
                self.assertEqual(len(pushes), 1)

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
        self.assertEqual(baseline.poll(), [second])
        self.assertEqual(baseline.poll(), [])
        third = a.post({}, "third")
        self.assertEqual(baseline.poll(), [third])

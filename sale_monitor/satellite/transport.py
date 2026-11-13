"""Append-only git mailboxes; payloads never pass through checkout filters."""
from __future__ import annotations

import base64
import os
from pathlib import Path, PurePosixPath
import re
import subprocess


class TransportError(RuntimeError):
    pass


def _path(path):
    if (not isinstance(path, str) or not path or "\\" in path or "\0" in path
            or path.startswith("/") or any(p in ("", ".", "..") for p in path.split("/"))):
        raise TransportError("invalid mailbox path")
    return PurePosixPath(path).as_posix()


class GitMailbox:
    def __init__(self, remote_url, write_branch, read_branch, workdir, *, env=None,
                 start_after=None, timeout=60,
                 identity=("pc-sale-monitor", "noreply@example.invalid")):
        self.workdir = Path(workdir)
        self.env = dict(os.environ if env is None else env)
        # An ambient repository/index must never redirect plumbing elsewhere.
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
            self.env.pop(key, None)
        token = self.env.get("GITHUB_TOKEN")
        if token:
            index = int(self.env.get("GIT_CONFIG_COUNT", "0"))
            credential = base64.b64encode(("x-access-token:" + token).encode()).decode()
            self.env.update({"GIT_CONFIG_COUNT": str(index + 1),
                             f"GIT_CONFIG_KEY_{index}": "http.https://github.com/.extraheader",
                             f"GIT_CONFIG_VALUE_{index}": "AUTHORIZATION: basic " + credential})
        self.timeout = timeout
        self.write_branch, self.read_branch = write_branch, read_branch
        self._seen, self._position = start_after, start_after or None
        self._initialized = start_after is not None
        try:
            self.workdir.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise TransportError("mailbox directory unavailable") from None
        self._git("init", "--quiet")
        for branch in (write_branch, read_branch):
            self._git("check-ref-format", "refs/heads/" + branch)
        existing = self._git("remote", "get-url", "origin", allowed=(0, 2, 128))
        if existing.returncode:
            self._git("remote", "add", "origin", remote_url)
        elif existing.stdout.decode().strip() != remote_url:
            raise TransportError("mailbox remote mismatch")
        self.env.update(GIT_AUTHOR_NAME=identity[0], GIT_AUTHOR_EMAIL=identity[1],
                        GIT_COMMITTER_NAME=identity[0], GIT_COMMITTER_EMAIL=identity[1],
                        GIT_INDEX_FILE=str((self.workdir / ".git" / "mailbox-index").resolve()))

    def _git(self, *args, data=None, allowed=(0,)):
        try:
            result = subprocess.run(["git", *args], cwd=self.workdir, env=self.env,
                                    input=data, capture_output=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise TransportError("git command unavailable or timed out") from None
        if result.returncode not in allowed:
            # Git stderr can contain credentials supplied by external helpers.
            raise TransportError("git " + args[0] + " failed")
        return result

    def _fetch(self, branch):
        ref = "refs/heads/" + branch
        probe = self._git("ls-remote", "--exit-code", "--heads", "origin", ref, allowed=(0, 2))
        if probe.returncode == 2:
            return None
        local = "refs/mailbox/" + branch
        self._git("fetch", "--quiet", "--no-tags", "origin", ref + ":" + local)
        return self._git("rev-parse", local).stdout.decode().strip()

    def post(self, files, message):
        rows = []
        for path, data in files.items():
            path = _path(path)
            if type(data) is not bytes:
                raise TransportError("mailbox payload must be bytes")
            blob = self._git("hash-object", "-w", "--stdin", data=data).stdout.strip()
            rows.append(b"100644 " + blob + b"\t" + path.encode() + b"\0")
        for attempt in range(2):
            parent = self._fetch(self.write_branch)
            self._git("read-tree", "--empty")
            self._git("update-index", "-z", "--index-info", data=b"".join(rows))
            tree = self._git("write-tree").stdout.decode().strip()
            parents = ("-p", parent) if parent else ()
            commit = self._git("commit-tree", tree, *parents, data=message.encode()).stdout.decode().strip()
            pushed = self._git("push", "--porcelain", "origin", commit + ":refs/heads/" + self.write_branch,
                               allowed=(0, 1))
            if pushed.returncode == 0:
                return commit
            if attempt or not (b"[rejected]" in pushed.stdout and
                               (b"fetch first" in pushed.stdout or b"non-fast-forward" in pushed.stdout)):
                raise TransportError("git push failed")
        raise TransportError("git push rejected")

    def poll(self):
        head = self._fetch(self.read_branch)
        if head is None:
            self._initialized = True
            return []
        if not self._initialized:
            # Inspect the current head once on a fresh installation. A ticket
            # posted during a short outage can still be live; the agent checks
            # its collect_until rather than treating startup time as a cutoff.
            self._initialized, self._seen, self._position = True, head, head
            return [head]
        history = self._git("rev-list", "--first-parent", head).stdout.decode().splitlines()
        if self._seen:
            if self._seen not in history:
                raise TransportError("mailbox history diverged")
            history = history[:history.index(self._seen)]
        result = list(reversed(history))
        if result:
            self._seen = self._position = result[-1]
        return result

    def read(self, commit, path):
        path = _path(path)
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            raise TransportError("invalid mailbox commit")
        head = self._fetch(self.read_branch)
        if head is None or self._git("merge-base", "--is-ancestor", commit, head,
                                      allowed=(0, 1, 128)).returncode:
            raise TransportError("commit outside read branch")
        exists = self._git("cat-file", "-e", commit + ":" + path, allowed=(0, 1, 128))
        if exists.returncode:
            raise KeyError(path)
        return self._git("cat-file", "blob", commit + ":" + path).stdout

    def position(self):
        return self._position

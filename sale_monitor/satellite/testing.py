"""Deterministic two-branch mailboxes for offline tests."""
import hashlib

from .transport import TransportError, _path


class MemoryMailbox:
    def __init__(self, write, read, *, start_after=""):
        self._write, self._read = write, read
        self._seen, self._position = start_after, start_after or None
        self._initialized = start_after is not None

    def post(self, files, message):
        for path, data in files.items():
            _path(path)
            if type(data) is not bytes:
                raise TransportError("mailbox payload must be bytes")
        sha = hashlib.sha1(repr((self._write, files, message)).encode()).hexdigest()
        self._write.append((sha, dict(files)))
        return sha

    def poll(self):
        history = [sha for sha, _ in self._read]
        if not self._initialized:
            self._initialized = True
            self._seen = history[-1] if history else ""
            self._position = self._seen or None
            return [self._seen] if self._seen else []
        if self._seen:
            if self._seen not in history:
                raise TransportError("mailbox history diverged")
            history = history[history.index(self._seen) + 1:]
        if history:
            self._seen = self._position = history[-1]
        return history

    def read(self, commit, path):
        _path(path)
        for sha, files in self._read:
            if sha == commit:
                return files[path]
        raise TransportError("commit outside read branch")

    def position(self):
        return self._position


def mailbox_pair():
    relay, satellite = [], []
    return MemoryMailbox(relay, satellite), MemoryMailbox(satellite, relay)

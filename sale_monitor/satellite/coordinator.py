"""Coordinate remote children without merging their repository or state tree."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import time

from . import fingerprint, protocol, validation
from .transport import TransportError


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


@dataclass
class RemoteStage:
    store: str
    phase: str
    input_bytes: bytes
    ticket: dict = field(default_factory=dict)
    audit: dict = field(default_factory=dict)
    nonces: set = field(default_factory=set)
    acked: bool = False
    status: str | None = None
    accepted: bytes | None = None


class RemoteStages:
    def __init__(self, mailbox, disk, route_state, run_id, *, sources_config,
                 package_dir=None, pyproject_path=None, state_sha=None,
                 clock=time.time, monotonic=time.monotonic):
        protocol.string(run_id, r"[A-Za-z0-9_-]{1,100}")
        self.mailbox, self.disk, self.route_state = mailbox, disk, route_state
        self.run_id, self.sources_config, self.state_sha = run_id, sources_config, state_sha
        self.package_dir = Path(package_dir) if package_dir else Path(__file__).resolve().parents[1]
        self.pyproject_path = Path(pyproject_path) if pyproject_path else self.package_dir.parent / "pyproject.toml"
        self.clock, self.monotonic = clock, monotonic
        self.handles = []
        self._last_poll = None
        self._poll_seconds = 15
        self._failed = False
        self._pending = []
        self.audit = {"run_id": run_id, "phases": {}}
        self._position = disk.load("satellite/position.json", {}).get("commit")
        try:
            commits = self.mailbox.poll()
            if self._position in commits:
                commits = commits[commits.index(self._position) + 1:]
            self._pending.extend(commits)
            self._drain()
        except TransportError:
            self._failed = True

    def _drain(self):
        while self._pending:
            commit = self._pending[0]
            self._process(commit)
            self._position = commit
            self.disk.save("satellite/position.json", {"commit": commit})
            self._pending.pop(0)

    def _refusals(self, manifest):
        try:
            rows = validation.extract_refusals(manifest)
        except protocol.ValidationError:
            return
        for store in protocol.ALLOWED_HOSTS:
            route = store + ":direct"
            if route in self.route_state.routes:
                self.route_state.apply_refusals(route, rows, self.clock())
        self.route_state.save()

    def _late(self, commit, manifest):
        # Identifiers have already passed the manifest schema.
        name = "satellite/late/" + manifest["run_id"] + "-" + manifest["phase"] + ".json"
        records = self.disk.load(name, {"records": []})
        if not any(row["satellite_commit"] == commit for row in records["records"]):
            records["records"].append({"satellite_commit": commit, "manifest": manifest})
            self.disk.save(name, records)

    def begin(self, store, phase, input_state_bytes, deadlines, plan_bytes=None, accepted_discovery=None):
        handle = RemoteStage(store, phase, input_state_bytes)
        self.handles.append(handle)
        route_id = store + ":direct"
        function = "sale_discovery" if phase == "discovery" else "product_verify"
        if not self.route_state.is_active(store, function):
            handle.status = "skipped:route_inactive"
            return handle
        if self._failed:
            handle.status = "relay_failed"
            return handle
        try:
            effective = validation.build_effective_config(self.sources_config,
                {**self.route_state.config, "routes": self.route_state.routes}, store)
            files = {"input/stores/" + store + ".json": input_state_bytes,
                     "input/routes.json": encode({"schema_version": 1,
                         "config_revision": self.route_state.config["config_revision"],
                         "routes": {route_id: self.route_state.routes[route_id]}}),
                     "input/config.json": encode(effective)}
            if plan_bytes is not None:
                files["input/plan.json"] = plan_bytes
            now = self.clock()
            code = fingerprint.code_fingerprint(self.package_dir, self.pyproject_path)
            fields = dict(run_id=self.run_id, phase=phase, store=store,
                issued_at=protocol.time_string(now),
                collect_until=protocol.time_string(deadlines["collect_until"]),
                accept_until=protocol.time_string(deadlines["accept_until"]),
                seconds=max(1, int(deadlines["S"] - now)), code_fingerprint=code["fingerprint"],
                config_revision=self.route_state.config["config_revision"], state_sha=self.state_sha,
                cleared_refusals=self.route_state.config["routes"][route_id].get("cleared_refusals", []),
                input_sha256={path: protocol.sha256_bytes(body) for path, body in files.items()})
            if accepted_discovery is not None:
                fields["accepted_discovery"] = accepted_discovery
            handle.ticket = protocol.build_ticket(**fields)
            files["ticket.json"] = encode(handle.ticket)
            handle.audit["ticket_commit"] = self.mailbox.post(files, self.run_id + " " + phase)
        except (TransportError, protocol.ValidationError, OSError, ValueError, KeyError):
            handle.status = "relay_failed"
        return handle

    def _ack_end(self, handle):
        return protocol.ack_window_end(protocol.epoch(handle.ticket["issued_at"]),
                                       protocol.epoch(handle.ticket["accept_until"]))

    def _process(self, commit):
        try:
            manifest = json.loads(self.mailbox.read(commit, "manifest.json"))
        except (KeyError, ValueError, UnicodeError):
            return
        self._refusals(manifest)
        try:
            protocol.validate_manifest(manifest)
        except protocol.ValidationError as exc:
            for handle in self.handles:
                if handle.status is None and isinstance(manifest, dict) and manifest.get("ticket_commit") == handle.audit.get("ticket_commit"):
                    handle.status = "satellite_rejected:" + exc.reason
                    handle.audit.update(result_commit=commit, received_at=protocol.time_string(self.clock()))
            return
        handle = next((h for h in self.handles if h.audit.get("ticket_commit") == manifest["ticket_commit"]
                       and (self.run_id, h.phase, h.store) == (manifest["run_id"], manifest["phase"], manifest["store"])), None)
        if handle is None or handle.status is not None:
            self._late(commit, manifest)
            return
        now = self.clock()
        handle.audit.update(counters=manifest["counters"], code_sha=manifest["code_sha"],
                            code_fingerprint=manifest["code_fingerprint"])
        if manifest["kind"] == "result":
            handle.audit["result_commit"] = commit
        if not handle.acked and now > self._ack_end(handle):
            handle.status = "satellite_absent"
            self._late(commit, manifest)
            return
        if now > protocol.epoch(handle.ticket["accept_until"]):
            handle.status = "satellite_deadline"
            self._late(commit, manifest)
            return
        if manifest["status"].startswith("declined:"):
            handle.acked = True
            handle.status = "satellite_" + manifest["status"]
            handle.audit["received_at"] = protocol.time_string(now)
            if manifest["kind"] == "ack":
                handle.audit["ack_commit"] = commit
            return
        if manifest["kind"] == "ack":
            if now <= self._ack_end(handle):
                handle.acked = True
                handle.audit.setdefault("ack_commit", commit)
                nonce = manifest["nonce"]
                if nonce not in handle.nonces:
                    echo = protocol.build_echo(ticket_commit=handle.audit["ticket_commit"], nonce=nonce,
                                               actions_time=protocol.time_string(now))
                    handle.audit["echo_commit"] = self.mailbox.post({"echo.json": encode(echo)}, "Echo " + self.run_id)
                    handle.nonces.add(nonce)
            return
        handle.acked = True
        try:
            body = self.mailbox.read(commit, "stores/" + handle.store + ".json")
            received = self.clock()
            handle.audit["received_at"] = protocol.time_string(received)
            if received > protocol.epoch(handle.ticket["accept_until"]):
                handle.status = "satellite_deadline"
                self._late(commit, manifest)
                return
            validation.validate_result(handle.input_bytes, body, handle.ticket, manifest, received,
                                       ticket_commit=handle.audit["ticket_commit"])
        except (protocol.ValidationError, KeyError) as exc:
            handle.status = "satellite_rejected:" + (exc.reason if isinstance(exc, protocol.ValidationError) else "missing_state")
            return
        handle.status, handle.accepted = manifest["status"], body

    def service(self):
        now = self.monotonic()
        if self._last_poll is not None and now - self._last_poll < self._poll_seconds:
            self._expire()
            return
        self._last_poll = now
        try:
            self._pending.extend(self.mailbox.poll())
            self._drain()
            self._poll_seconds = 15
        except TransportError:
            self._failed = True
            self._poll_seconds = min(300, self._poll_seconds * 2)
            for handle in self.handles:
                if handle.status is None:
                    handle.status = "relay_failed"
        self._expire()

    def _expire(self):
        for handle in self.handles:
            if handle.status is None:
                now = self.clock()
                if not handle.acked and now >= self._ack_end(handle):
                    handle.status = "satellite_absent"
                elif now > protocol.epoch(handle.ticket["accept_until"]):
                    handle.status = "satellite_deadline"

    def finish(self, handle, wait):
        while handle.status is None:
            self.service()
            if handle.status is None:
                end = protocol.epoch(handle.ticket["accept_until"]) if handle.acked else self._ack_end(handle)
                # At the inclusive result boundary, allow the next poll to decide.
                wait(max(.01, min(1, end - self.clock())))
        if handle.accepted is not None:
            path = self.disk.root / "stores" / (handle.store + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".json.tmp")
            with temporary.open("wb") as output:
                output.write(handle.accepted)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        outcome = handle.status
        handle.audit.update(outcome=outcome, reason=outcome.split(":", 1)[1] if ":" in outcome else None)
        self.audit = self.disk.load("satellite/" + self.run_id + ".json", self.audit)
        self.audit["phases"][handle.phase] = dict(handle.audit)
        self.disk.save("satellite/" + self.run_id + ".json", self.audit)
        self.route_state.save()
        return handle.status, handle.accepted

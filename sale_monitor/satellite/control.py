"""Persistent safety state, independent of transferred store state."""
from copy import deepcopy
from pathlib import Path

from ..storage import atomic_json, read_json
from .protocol import (UNREACHABLE_RUN_LIMIT, bounded, hash_value, integer, keys,
                       require, string, validate_refusals)


class ControlState:
    """One local writer; inject read/write functions for in-memory tests."""
    def __init__(self, directory, *, read=read_json, write=atomic_json):
        self.path = Path(directory) / "control.json"
        self.write = write
        self.data = read(self.path, {
            "version": 1, "active_refusals": [], "cleared_refusals": [],
            "retry_after_until": {}, "last_request_at": {}, "zero_success_runs": 0,
            "unreachable": False, "intake_stopped": False, "intake_reason": None,
            "processed": {}, "unsent_results": {}, "runs": {},
        })
        self._validate()

    def _validate(self):
        bounded(self.data, limit=16 * 1024 * 1024)
        keys(self.data, {"version", "active_refusals", "cleared_refusals", "retry_after_until", "last_request_at", "zero_success_runs", "unreachable", "intake_stopped", "intake_reason", "processed", "unsent_results", "runs"})
        require(type(self.data["version"]) is int and self.data["version"] == 1, "control_version")
        validate_refusals(self.data["active_refusals"])
        require(type(self.data["cleared_refusals"]) is list and all(type(x) is str for x in self.data["cleared_refusals"]), "control_cleared")
        integer(self.data["zero_success_runs"])
        for key in ("unreachable", "intake_stopped"):
            require(type(self.data[key]) is bool, "control_flag")
        require(self.data["intake_reason"] is None or type(self.data["intake_reason"]) is str, "control_reason")
        for key in ("retry_after_until", "last_request_at", "processed", "unsent_results", "runs"):
            require(type(self.data[key]) is dict, "control_mapping")
        for key in ("retry_after_until", "last_request_at"):
            require(all(type(x) in (int, float) for x in self.data[key].values()), "control_time")
        for commit, outcome in self.data["processed"].items():
            hash_value(commit, commit=True)
            string(outcome)
        for commit, metadata in self.data["unsent_results"].items():
            require(commit in self.data["processed"] and type(metadata) is dict, "control_unsent_result")
        for run_id, run in self.data["runs"].items():
            string(run_id, r"[A-Za-z0-9_-]{1,100}")
            keys(run, {"attempted", "succeeded", "excluded"})
            integer(run["attempted"])
            integer(run["succeeded"])
            require(run["succeeded"] <= run["attempted"] and type(run["excluded"]) is bool, "control_run")

    def save(self):
        self._validate()
        self.write(self.path, self.data)

    @property
    def active_refusals(self):
        cleared = set(self.data["cleared_refusals"])
        return deepcopy([row for row in self.data["active_refusals"] if row["refusal_id"] not in cleared])

    def add_refusal(self, row):
        validate_refusals([row])
        if row["refusal_id"] not in set(self.data["cleared_refusals"]) | {r["refusal_id"] for r in self.active_refusals}:
            self.data["active_refusals"].append(deepcopy(row))
            self.save()

    def apply_ticket(self, ticket):
        cleared = ticket.get("cleared_refusals", [])
        require(type(cleared) is list and all(type(x) is str for x in cleared), "invalid_cleared_refusals")
        self.data["cleared_refusals"] = sorted(set(self.data["cleared_refusals"]) | set(cleared))
        self.data["active_refusals"] = self.active_refusals
        self.save()

    def decline_reason(self, ticket):
        self.apply_ticket(ticket)
        if self.active_refusals:
            return "refused"
        if self.data["unreachable"]:
            return "unreachable"
        if self.data["intake_stopped"]:
            return "intake_stopped"
        return None

    def set_retry_after(self, host, until):
        self.data["retry_after_until"][host] = max(self.data["retry_after_until"].get(host, 0), until)
        self.save()

    def retry_until(self, host, input_until=0):
        return max(self.data["retry_after_until"].get(host, 0), input_until)

    def request_started(self, host, at):
        self.data["last_request_at"][host] = at
        self.save()

    def record_run(self, run_id, ordinary_attempted, ordinary_succeeded, *, excluded=False):
        """Call once at run end, aggregating discovery and verification."""
        if run_id in self.data["runs"]:
            return
        integer(ordinary_attempted)
        integer(ordinary_succeeded)
        require(ordinary_succeeded <= ordinary_attempted, "invalid_success_count")
        self.data["runs"][run_id] = {"attempted": ordinary_attempted, "succeeded": ordinary_succeeded, "excluded": excluded}
        if ordinary_succeeded:
            self.data["zero_success_runs"] = 0
        elif not excluded and ordinary_attempted:
            self.data["zero_success_runs"] += 1
            if self.data["zero_success_runs"] >= UNREACHABLE_RUN_LIMIT:
                self.data["unreachable"] = True
        self.save()

    def stop_intake(self, reason):
        self.data.update(intake_stopped=True, intake_reason=reason)
        self.save()

    def clear_local(self):
        self.data.update(unreachable=False, zero_success_runs=0, intake_stopped=False, intake_reason=None)
        self.save()

    def claim_ticket(self, commit):
        """Persist before collection; interrupted tickets remain processed."""
        hash_value(commit, commit=True)
        if commit in self.data["processed"]:
            return False
        self.data["processed"][commit] = "started"
        self.save()
        return True

    def finish_ticket(self, commit, outcome):
        require(commit in self.data["processed"], "unclaimed_ticket")
        self.data["processed"][commit] = outcome
        self.save()

    def queue_result(self, commit, metadata):
        require(commit in self.data["processed"], "unclaimed_ticket")
        self.data["unsent_results"][commit] = deepcopy(metadata)
        self.save()

    def result_sent(self, commit):
        self.data["unsent_results"].pop(commit, None)
        self.save()

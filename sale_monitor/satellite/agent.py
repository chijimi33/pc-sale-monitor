"""Durable PC-side ticket intake and isolated stage supervision."""
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid

from ..pipeline import _attach_job, _close_job, _stop_process_tree, _kill_remaining_process_group, _job_api
from ..storage import atomic_json, read_json
from . import fingerprint, protocol, transport
from .control import ControlState
from .deadline import Handshake, deadline_clock as system_deadline_clock
from .stage import COUNTERS, STOP_REASONS, atomic_bytes, check_inputs


def default_home():
    if os.name == "nt":
        return Path.home() / ".pc-sale-monitor-satellite"
    return Path.home() / ".local/state/pc-sale-monitor-satellite"


class LockHeld(RuntimeError):
    pass


class OSLock:
    def __init__(self, home):
        self.path = Path(home) / "agent.lock"
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise LockHeld("Satellite is already running or updating (agent.lock).") from exc
        self.handle = handle
        return self

    def __exit__(self, *args):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def read_agent_id(home):
    try:
        value = (Path(home) / "agent_id").read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return None
    protocol.string(value, r"[0-9a-f]{32}")
    return value


def ensure_agent_id(home):
    """Caller holds OSLock(home); never replace an existing identity."""
    value = read_agent_id(home)
    if value is None:
        value = uuid.uuid4().hex
        atomic_bytes(Path(home) / "agent_id", (value + "\n").encode("ascii"))
    return value


def make_log(home):
    directory = Path(home) / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("satellite." + str(Path(home).resolve()))
    if not log.handlers:
        handler = RotatingFileHandler(directory / "agent.log", maxBytes=1_048_576, backupCount=3,
                                      encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


def close_log(log):
    for handler in list(log.handlers):
        handler.close()
        log.removeHandler(handler)


def make_mailbox(home):
    from .transport import GitMailbox
    state = read_json(Path(home) / "agent.json", {})
    return GitMailbox(protocol.REMOTE_URL, protocol.SATELLITE_BRANCH, protocol.RELAY_BRANCH,
                      Path(home) / "mailbox", start_after=state.get("relay_position"), timeout=60,
                      identity=("PC sale monitor satellite", "satellite@localhost"))


def code_sha(package_dir):
    try:
        result = subprocess.run(["git", "-C", str(package_dir), "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=15, check=True)
        value = result.stdout.strip()
        protocol.hash_value(value, commit=True)
        return value
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _kill_on_close(process):
    """Contain the suspended child before the pipeline helper resumes it."""
    class Basic(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                    ("flags", ctypes.c_uint32), ("minimum", ctypes.c_size_t),
                    ("maximum", ctypes.c_size_t), ("active", ctypes.c_uint32),
                    ("affinity", ctypes.c_size_t), ("priority", ctypes.c_uint32),
                    ("scheduling", ctypes.c_uint32)]

    class Limits(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", ctypes.c_ulonglong * 6),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

    limits = Limits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    kernel = _job_api()
    function = kernel.SetInformationJobObject
    function.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    function.restype = ctypes.c_int
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not function(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.AssignProcessToJobObject(job, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel.CloseHandle(job)
        raise
    process._satellite_job = job


def _close_parent_job(process):
    job = getattr(process, "_satellite_job", None)
    if job is not None:
        _job_api().CloseHandle(job)
        process._satellite_job = None


def start_stage(work):
    options = {"start_new_session": True} if os.name == "posix" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004}
    process = subprocess.Popen([sys.executable, "-m", "sale_monitor.satellite", "stage", "--work", str(work)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, **options)
    try:
        if os.name == "posix":
            process._pipeline_owned_group = True
        if os.name == "nt":
            _kill_on_close(process)
        _attach_job(process)
    except BaseException:
        _stop_process_tree(process)
        _close_parent_job(process)
        raise
    return process


def manifest_for(ticket, commit, status, *, started_at, finished_at, identity, dependencies,
                 config_sha256, agent_id, output=None, active_refusals, counters=None, nonce=None, code=None):
    """Keep the PC wire vocabulary here while protocol validation evolves."""
    result = dict(protocol_version=protocol.PROTOCOL_VERSION, kind="ack" if nonce else "result",
        ticket_commit=commit, agent_id=agent_id, run_id=ticket["run_id"], phase=ticket["phase"], store=ticket["store"],
        status=status, started_at=started_at, finished_at=finished_at, code_sha=code,
        code_fingerprint=identity["fingerprint"], dependencies=dependencies,
        input_sha256=ticket["input_sha256"], config_sha256=config_sha256,
        output_sha256=protocol.sha256_bytes(output) if output is not None else None,
        active_refusals=active_refusals, counters=counters or dict.fromkeys(COUNTERS, 0))
    if nonce:
        result["nonce"] = nonce
    return result


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def stage_outcome(work):
    try:
        result = read_json(work / "stage_result.json", {})
        counters = result.get("counters", dict.fromkeys(COUNTERS, 0))
        protocol.keys(counters, COUNTERS)
        for count in counters.values():
            protocol.integer(count)
        protocol.require(counters["ordinary_succeeded"] <= counters["ordinary_attempted"] <= counters["requests"]
                         and counters["no_response"] <= counters["requests"]
                         and counters["refusal"] <= counters["requests"], "invalid_counters")
        return result.get("reason", "error"), counters
    except (OSError, ValueError, TypeError, AttributeError):
        return "error", dict.fromkeys(COUNTERS, 0)


class Agent:
    def __init__(self, home, mailbox, *, deadline_clock=None, wall_clock=time.time, sleep=time.sleep,
                 stage_runner=start_stage, package_dir=None, pyproject_path=None, log=None):
        self.home = Path(home).resolve()
        self.mailbox = mailbox
        self.clock = deadline_clock
        self.wall_clock, self.sleep, self.stage_runner = wall_clock, sleep, stage_runner
        self.package_dir = Path(package_dir) if package_dir else Path(__file__).resolve().parents[1]
        self.pyproject_path = Path(pyproject_path) if pyproject_path else self.package_dir.parent / "pyproject.toml"
        self.log = log
        self.owns_log = log is None
        self.lock = None
        self.control = None
        self.waiting = None
        self.next_poll = self.next_io = 0
        self.backoff = 30
        self.poll_thread = None
        self.poll_results = queue.Queue()

    def _save(self):
        atomic_json(self.home / "agent.json", self.state)

    def _open(self):
        if self.lock is not None:
            return
        lock = OSLock(self.home)
        lock.__enter__()
        try:
            self.agent_id = ensure_agent_id(self.home)
            self.clock = self.clock or system_deadline_clock()
            self.log = self.log or make_log(self.home)
            self.control = ControlState(self.home)
            self.state = read_json(self.home / "agent.json", {
                "relay_position": None, "pending": [], "tickets": {}, "accounting": {}})
            self.lock = lock
        except BaseException:
            lock.__exit__()
            raise

    def close(self):
        if self.poll_thread is not None:
            self.poll_thread.join()
            self._collect_poll()
        if self.lock is not None:
            self.lock.__exit__()
            self.lock = None
        if self.owns_log and self.log is not None:
            close_log(self.log)
            self.log = None

    def _failure(self, operation, exc):
        detail = type(exc).__name__
        if isinstance(exc, transport.TransportError):
            detail += ": " + str(exc)
        self.log.warning("%s failed (%s); retry in %ss", operation, detail, self.backoff)
        self.next_io = self.clock() + self.backoff
        self.backoff = min(300, self.backoff * 2)

    def _post(self, files, message):
        if not self._collect_poll() or self.clock() < self.next_io:
            return False
        try:
            self.mailbox.post(files, message)
        except Exception as exc:
            self._failure("post", exc)
            return False
        self.backoff = 30
        return True

    def _poll(self, *, echo=False):
        if not self._collect_poll():
            return
        if self.clock() < max(self.next_io, self.next_poll):
            return
        try:
            commits = self.mailbox.poll()
            self._accept_poll(commits, self.mailbox.position())
        except Exception as exc:
            self._failure("poll", exc)
            return
        self.next_poll = self.clock() + (5 if echo else 30)
        self.backoff = 30
        for commit in list(self.state["pending"]):
            try:
                data = self.mailbox.read(commit, "echo.json")
            except KeyError:
                continue
            except Exception as exc:
                self._failure("read", exc)
                return
            try:
                value = json.loads(data)
                if (self.waiting is not None and value.get("nonce") == self.waiting.nonce
                        and value.get("ticket_commit") == self.waiting.ticket_commit):
                    self.waiting.accept_echo(value)
            except (ValueError, TypeError, AttributeError):
                self.log.info("Ignoring invalid echo")
            self.state["pending"].remove(commit)
            self._save()

    def _accept_poll(self, commits, position):
        self.state["pending"] = list(dict.fromkeys(self.state["pending"] + commits))
        self.state["relay_position"] = position
        self._save()

    def _collect_poll(self):
        if self.poll_thread is None:
            return True
        if self.poll_thread.is_alive():
            return False
        self.poll_thread.join()
        self.poll_thread = None
        commits, position, error = self.poll_results.get_nowait()
        if error is not None:
            self._failure("poll", error)
        else:
            self._accept_poll(commits, position)
            self.backoff = 30
        return True

    def _background_poll(self):
        # Only this worker touches the mailbox until it finishes.
        if not self._collect_poll() or self.clock() < max(self.next_io, self.next_poll):
            return
        self.next_poll = self.clock() + 30

        def poll():
            try:
                commits = self.mailbox.poll()
                self.poll_results.put((commits, self.mailbox.position(), None))
            except Exception as exc:
                self.poll_results.put((None, None, exc))

        self.poll_thread = threading.Thread(target=poll, name="satellite-poll", daemon=True)
        self.poll_thread.start()
        self._collect_poll()

    def _identity(self):
        return fingerprint.code_fingerprint(self.package_dir, self.pyproject_path)

    def _work(self, commit):
        return self.home / "work" / commit

    def _manifest(self, commit, status, *, output=None, counters=None, nonce=None):
        metadata = self.state["tickets"][commit]
        ticket = metadata["ticket"]
        return manifest_for(ticket, commit, status, started_at=metadata["started_at"],
            finished_at=None if nonce else protocol.time_string(max(self.wall_clock(), protocol.epoch(metadata["started_at"]))),
            identity=metadata["identity"], dependencies=metadata["dependencies"],
            config_sha256=metadata["config_sha256"], output=output,
            active_refusals=self.control.active_refusals, counters=counters, nonce=nonce,
            code=metadata["code_sha"], agent_id=self.agent_id)

    def _finish(self, commit, status, output=None, counters=None):
        self.control = ControlState(self.home)
        result_dir = self.home / "results" / commit
        result_dir.mkdir(parents=True, exist_ok=True)
        manifest = self._manifest(commit, status, output=output, counters=counters)
        atomic_bytes(result_dir / "manifest.json", encode(manifest))
        paths = {"manifest.json": str((result_dir / "manifest.json").relative_to(self.home))}
        if output is not None:
            path = result_dir / "stores" / (manifest["store"] + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_bytes(path, output)
            paths["stores/" + manifest["store"] + ".json"] = str(path.relative_to(self.home))
        # Queue before marking finished, so a crash never loses the result.
        self.control.queue_result(commit, {"files": paths, "accept_until": self.state["tickets"][commit]["ticket"]["accept_until"]})
        self.control.finish_ticket(commit, status)
        self._account(commit, manifest)
        self._resend()

    def _resend(self):
        for commit, queued in list(self.control.data["unsent_results"].items()):
            if self.clock() < self.next_io:
                break
            if self.wall_clock() > protocol.epoch(queued["accept_until"]) + 86400:
                if not queued.get("expired"):
                    queued["expired"] = True
                    self.control.save()
                    self.log.warning("Result %s retained after retry expiry", commit)
                continue
            if queued.get("expired"):
                continue
            try:
                files = {name: (self.home / path).read_bytes() for name, path in queued["files"].items()}
                manifest = json.loads(files["manifest.json"])
            except (OSError, ValueError) as exc:
                self.log.error("Queued result %s unreadable (%s)", commit, type(exc).__name__)
                continue
            self._account(commit, manifest)
            if self._post(files, "Satellite result"):
                self.control.result_sent(commit)
                metadata = self.state["tickets"].get(commit)
                if metadata:
                    self.control.finish_ticket(commit, json.loads(files["manifest.json"])["status"])
                self._cleanup_work(commit)
                self._cleanup_payload(self.home / "results" / commit)

    def _cleanup_work(self, commit):
        self._cleanup_payload(self._work(commit))

    def _cleanup_payload(self, directory):
        target = directory.resolve()
        protocol.require(target != self.home and target.is_relative_to(self.home), "cleanup_outside_home")
        if target.exists():
            shutil.rmtree(target)

    def _account(self, commit, manifest):
        status = manifest["status"]
        excluded = status.split(":")[-1] in {"code_mismatch", "dependency_mismatch", "agent_mismatch", "expired", "retry_after",
                                             "clock_unverified", "clock_skew", "deadline", "killed"}
        excluded = excluded or self.state["tickets"][commit].get("account_excluded", False)
        run_id = manifest["run_id"]
        if run_id in self.control.data["runs"]:
            return
        self.state["accounting"].setdefault(run_id, {})[commit] = {
            "attempted": manifest["counters"]["ordinary_attempted"],
            "succeeded": manifest["counters"]["ordinary_succeeded"], "excluded": excluded,
            "accept_until": self.state["tickets"][commit]["ticket"]["accept_until"]}
        self._save()

    def _finalize_runs(self, next_run=None):
        for run_id, stages in list(self.state["accounting"].items()):
            last = max(protocol.epoch(row["accept_until"]) for row in stages.values())
            if (next_run is not None and next_run != run_id) or self.wall_clock() > last + 3600:
                counted = [row for row in stages.values() if not row["excluded"] or row["succeeded"]]
                self.control.record_run(run_id, sum(row["attempted"] for row in counted),
                    sum(row["succeeded"] for row in counted), excluded=not counted)
                del self.state["accounting"][run_id]
                self._save()

    def _recover(self):
        if self.clock() < self.next_io:
            return
        for commit, outcome in list(self.control.data["processed"].items()):
            if outcome != "started" or commit in self.control.data["unsent_results"]:
                continue
            if commit not in self.state["tickets"]:
                try:
                    ticket = protocol.validate_ticket(json.loads(self.mailbox.read(commit, "ticket.json")))
                except Exception as exc:
                    self._failure("recover", exc)
                    continue
                self._remember(commit, ticket)
            self._inspect_code(commit)
            ticket = self.state["tickets"][commit]["ticket"]
            work = self._work(commit)
            path = work / "state/stores" / (ticket["store"] + ".json")
            try:
                output = path.read_bytes() if path.exists() else self.mailbox.read(commit, "input/stores/" + ticket["store"] + ".json")
            except Exception as exc:
                self._failure("recover", exc)
                continue
            _, counters = stage_outcome(work)
            self._stage_accounting(commit, work)
            self._finish(commit, "stopped:error", output, counters)

    def _remember(self, commit, ticket):
        self.state["tickets"][commit] = {"ticket": ticket,
            "started_at": protocol.time_string(self.wall_clock()), "identity": None,
            "dependencies": None,
            "config_sha256": ticket["input_sha256"]["input/config.json"], "code_sha": code_sha(self.package_dir)}
        self._save()

    def _inspect_code(self, commit):
        metadata = self.state["tickets"][commit]
        if metadata["identity"] is None:
            metadata["identity"] = self._identity()
            metadata["dependencies"] = fingerprint.dependency_status(self.pyproject_path)
            self._save()

    def _stage_accounting(self, commit, work):
        try:
            result = read_json(work / "stage_result.json", {})
        except (ValueError, OSError):
            return
        if type(result) is dict and result.get("guard_reason") in {"clock_unverified", "clock_skew"}:
            self.state["tickets"][commit]["account_excluded"] = True
            self._save()

    def _handle(self, commit, ticket, ticket_bytes):
        protocol.hash_value(commit, commit=True)
        if ticket.get("store") not in protocol.ALLOWED_HOSTS:
            return
        ticket = protocol.validate_ticket(ticket)
        if commit in self.control.data["processed"]:
            return
        self._finalize_runs(ticket["run_id"])
        if self.wall_clock() > protocol.epoch(ticket["collect_until"]):
            self.log.info("Ignoring stale ticket %s", commit)
            return
        self._remember(commit, ticket)
        if not self.control.claim_ticket(commit):
            return
        self.control.apply_ticket(ticket)
        reason = self.control.decline_reason(ticket)
        self._inspect_code(commit)
        metadata = self.state["tickets"][commit]
        inputs = {}
        if not reason and (metadata["identity"]["fingerprint"] != ticket["code_fingerprint"]
                           or metadata["identity"]["fingerprint_version"] != ticket["fingerprint_version"]):
            reason = "code_mismatch"
        if not reason and not metadata["dependencies"]["ok"]:
            reason = "dependency_mismatch"
        if not reason:
            try:
                for path, hashed in ticket["input_sha256"].items():
                    inputs[path] = self.mailbox.read(commit, path)
                    protocol.require(protocol.sha256_bytes(inputs[path]) == hashed, "input_hash")
                accepted = ticket.get("accepted_discovery", {}).get("state_sha256")
                if accepted:
                    protocol.require(protocol.sha256_bytes(inputs[f"input/stores/{ticket['store']}.json"]) == accepted, "discovery_hash")
            except Exception as exc:
                if not isinstance(exc, (KeyError, protocol.ValidationError)):
                    self._failure("read", exc)
                reason = "input_mismatch"
        if not reason:
            metadata["config_sha256"] = protocol.sha256_bytes(inputs["input/config.json"])
            self._save()
            try:
                config, saved, plan, interval = check_inputs(ticket, inputs)
                host = protocol.ALLOWED_HOSTS[ticket["store"]]
                if protocol.registered_agent(config["route"]) != self.agent_id:
                    reason = "agent_mismatch"
                elif self.control.retry_until(host, saved.get("retry_after", {}).get(host, 0)) > self.wall_clock():
                    reason = "retry_after"
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
                # The wire status stays coarse; the local log says which check failed.
                self.log.info("Declining %s: inputs invalid (%s: %s)", commit, type(exc).__name__,
                              getattr(exc, "reason", ""))
                reason = "config_invalid"
        if reason:
            self._finish(commit, "declined:" + reason)
            return
        nonce = uuid.uuid4().hex
        handshake = Handshake(commit, nonce, deadline_clock=self.clock, wall_clock=self.wall_clock)
        ack = encode(self._manifest(commit, "acknowledged", nonce=nonce))
        self.waiting = handshake
        handshake.begin_ack()
        posted = self._post({"manifest.json": ack}, "Satellite acknowledgement")
        self.next_poll = self.clock()
        try:
            while posted and handshake.actions_time is None and not handshake.failed:
                if self.clock() - handshake.m1 > protocol.ECHO_WAIT_SECONDS:
                    break
                self._poll(echo=True)
                if handshake.actions_time is None and not handshake.failed:
                    self.sleep(1)
        finally:
            self.waiting = None
        if not posted or handshake.reason:
            self._finish(commit, "declined:" + (handshake.reason or "clock_unverified"))
            return
        if not handshake.may_request(ticket["collect_until"]):
            self._finish(commit, "declined:" + (handshake.reason or "expired"))
            return
        self.next_poll = self.clock() + 30
        work = self._work(commit)
        work.mkdir(parents=True, exist_ok=True)
        (work / "ticket.json").write_bytes(ticket_bytes)
        for path, data in inputs.items():
            destination = work / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        atomic_json(work / "handshake.json", handshake.to_dict())
        atomic_json(work / "context.json", {"home": str(self.home)})
        output_path = work / "state/stores" / (ticket["store"] + ".json")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_bytes(output_path, inputs[f"input/stores/{ticket['store']}.json"])
        process = None
        killed = False
        try:
            process = self.stage_runner(work)
            while process.poll() is None:
                if handshake.must_kill(ticket["collect_until"]):
                    # Reap a child that exited while the deadline was checked.
                    if process.poll() is not None:
                        break
                    _stop_process_tree(process)
                    killed = True
                    break
                self._background_poll()
                self.sleep(1)
            _kill_remaining_process_group(process)
            _close_job(process)
            _close_parent_job(process)
            reason, counters = stage_outcome(work)
            reason = "killed" if killed else reason
            if reason != "completed" and (reason not in STOP_REASONS or (reason == "killed" and not killed)):
                reason = "error"
            if not killed and process.returncode and reason == "completed":
                reason = "error"
        except Exception as exc:
            if process is not None and process.poll() is None:
                _stop_process_tree(process)
            self.log.warning("Stage failed (%s)", type(exc).__name__)
            if process is not None:
                _close_parent_job(process)
            reason, counters = "error", stage_outcome(work)[1]
        except BaseException:
            if process is not None and process.poll() is None:
                _stop_process_tree(process)
            if process is not None:
                _close_parent_job(process)
            raise
        self._stage_accounting(commit, work)
        self._finish(commit, "completed" if reason == "completed" else "stopped:" + reason,
                     output_path.read_bytes(), counters)

    def step(self):
        self._open()
        if not self._collect_poll():
            return
        if self.mailbox is None:
            if self.clock() < self.next_io:
                return
            try:
                self.mailbox = make_mailbox(self.home)
            except Exception as exc:
                self._failure("mailbox", exc)
                return
        self.control = ControlState(self.home)
        self._recover()
        self._resend()
        self._finalize_runs()
        self._poll()
        while self.state["pending"] and self._collect_poll() and self.clock() >= self.next_io:
            commit = self.state["pending"][0]
            try:
                ticket_bytes = self.mailbox.read(commit, "ticket.json")
                ticket = json.loads(ticket_bytes)
            except KeyError:
                self.state["pending"].remove(commit)
                self._save()
                continue
            except (ValueError, UnicodeError):
                self.log.info("Ignoring invalid ticket %s", commit)
            except Exception as exc:
                self._failure("read", exc)
                break
            else:
                try:
                    self._handle(commit, ticket, ticket_bytes)
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    self.log.info("Ignoring invalid ticket %s (%s)", commit, type(exc).__name__)
            if commit in self.state["pending"]:
                self.state["pending"].remove(commit)
                self._save()

    def run_forever(self):
        try:
            while True:
                self.step()
                self.sleep(1)
        finally:
            self.close()

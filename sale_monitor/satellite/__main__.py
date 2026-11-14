"""Explicit PC satellite commands; updates never run during intake."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from . import fingerprint, protocol
from .agent import (Agent, LockHeld, OSLock, close_log, default_home, ensure_agent_id,
                    make_log, read_agent_id)
from .control import ControlState


def update(home, *, repo=None, command=subprocess.run, output=print):
    repo = Path(repo) if repo else Path(__file__).resolve().parents[2]
    with OSLock(home):
        control = ControlState(home)
        log = make_log(home)
        try:
            origin = command(["git", "-C", str(repo), "remote", "get-url", "origin"],
                             check=True, capture_output=True, text=True, timeout=60)
            if origin.stdout.strip() != protocol.REMOTE_URL:
                raise ValueError("origin_mismatch")
            for arguments in (["git", "-C", str(repo), "fetch", "origin", "main"],
                              ["git", "-C", str(repo), "merge", "--ff-only", "origin/main"],
                              [sys.executable, "-m", "pip", "install", "-e", str(repo)]):
                command(arguments, check=True, capture_output=True, timeout=300)
            identity = fingerprint.code_fingerprint(repo / "sale_monitor", repo / "pyproject.toml")
            dependencies = fingerprint.dependency_status(repo / "pyproject.toml")
            if not dependencies["ok"]:
                raise ValueError("dependency_mismatch")
        except Exception as exc:
            reason = "update_failed:" + type(exc).__name__
            control.stop_intake(reason)
            log.error("%s", reason)
            output("Update failed; ticket intake stopped (" + type(exc).__name__ + ").")
            close_log(log)
            return 1
        # Updates must not clear an unreachable latch or any refusal.
        control.data.update(intake_stopped=False, intake_reason=None)
        control.save()
        output("Updated fingerprint: " + identity["fingerprint"])
        close_log(log)
        return 0


def status(home):
    agent_id = read_agent_id(home)
    if agent_id is None:
        try:
            with OSLock(home):
                agent_id = ensure_agent_id(home)
        except LockHeld:
            agent_id = read_agent_id(home)
    control = ControlState(home)
    package = Path(__file__).resolve().parents[1]
    data = control.data
    return {"home": str(Path(home).resolve()), "agent_id": agent_id, "active_refusals": control.active_refusals,
            "unreachable": data["unreachable"], "zero_success_runs": data["zero_success_runs"],
            "intake_stopped": data["intake_stopped"], "intake_reason": data["intake_reason"],
            "last_tickets": list(data["processed"].items())[-20:],
            "queued_results": data["unsent_results"],
            "code_fingerprint": fingerprint.code_fingerprint(package, package.parent / "pyproject.toml")}


def main(argv=None):
    parser = argparse.ArgumentParser(description="PC sale monitor satellite")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--once", action="store_true")
    for name in ("status", "clear-local", "update"):
        commands.add_parser(name)
    for name in ("run", "status", "clear-local", "update"):
        commands.choices[name].add_argument("--home", type=Path, default=None)
    stage = commands.add_parser("stage")
    stage.add_argument("--work", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "stage":
            from .stage import run_stage
            return run_stage(args.work)
        home = args.home or default_home()
        if args.command == "run":
            agent = Agent(home, None)
            try:
                if args.once:
                    agent.step()
                else:
                    agent.run_forever()
            finally:
                agent.close()
            return 0
        if args.command == "status":
            print(json.dumps(status(home), ensure_ascii=False, indent=2))
            return 0
        if args.command == "clear-local":
            with OSLock(home):
                ControlState(home).clear_local()
            print("Cleared unreachable latch and intake stop. Refusals require their IDs in "
                  "cleared_refusals in config/routes.json on main.")
            return 0
        return update(home)
    except LockHeld as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

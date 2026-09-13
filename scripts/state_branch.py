"""Fetch/publish durable state without force-push, resets, caches or personal tokens."""
from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path
import subprocess


def run(args, cwd, env, **kwargs):
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "publish"])
    parser.add_argument("--path", type=Path, default=Path("state-repo"))
    parser.add_argument("--branch", default="monitor-data")
    args = parser.parse_args()
    repo = os.environ["GITHUB_REPOSITORY"]
    env = dict(os.environ)
    token = env.get("GITHUB_TOKEN")
    if token:
        credential = base64.b64encode(("x-access-token:" + token).encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader", GIT_CONFIG_VALUE_0="AUTHORIZATION: basic " + credential)
    if args.command == "prepare":
        args.path.mkdir(parents=True, exist_ok=True)
        run(["init", "--quiet"], args.path, env)
        run(["remote", "add", "origin", "https://github.com/" + repo + ".git"], args.path, env)
        probe = subprocess.run(["git", "ls-remote", "--exit-code", "--heads", "origin", args.branch], cwd=args.path, env=env, capture_output=True)
        if probe.returncode == 0:
            run(["fetch", "--depth=1", "origin", args.branch], args.path, env)
            run(["checkout", "-b", args.branch, "FETCH_HEAD"], args.path, env)
        elif probe.returncode == 2:
            run(["checkout", "--orphan", args.branch], args.path, env)
        else:
            raise RuntimeError("data_branch_unavailable; refusing_to_initialize_empty_state")
    else:
        run(["config", "user.name", "github-actions[bot]"], args.path, env)
        run(["config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], args.path, env)
        run(["add", "state", "public"], args.path, env)
        changed = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=args.path, env=env).returncode
        if changed == 1:
            run(["commit", "-m", "Update sale observations, queue, events and validation metrics"], args.path, env)
            run(["push", "origin", "HEAD:" + args.branch], args.path, env)
        elif changed != 0:
            raise RuntimeError("unable_to_inspect_state_changes")


if __name__ == "__main__":
    main()

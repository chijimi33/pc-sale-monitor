from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import re
import subprocess

from .models import STORES, iso, utcnow
from .reporting import aggregate, checkpoint, merge_incoming, validation
from .runner import Collector
from .storage import Store, read_json


def _remote(args):
    from . import routes
    from .satellite import protocol
    from .satellite.coordinator import RemoteStages
    from .satellite.transport import GitMailbox
    try:
        disk = Store(args.state)
        config = routes.load_config(args.routes)
        state = routes.RouteState(disk, config)
        configured = any(route.get("executor") == "satellite" for route in config["routes"].values())
        # Late safety metadata still matters after pausing or changing executors.
        if not configured and not (args.state / "satellite").exists():
            return None
        position = disk.load("satellite/position.json", {}).get("commit", "")
        mailbox = GitMailbox(protocol.REMOTE_URL or "https://github.com/" + os.environ["GITHUB_REPOSITORY"] + ".git",
            write_branch=protocol.RELAY_BRANCH, read_branch=protocol.SATELLITE_BRANCH,
            workdir=args.state.resolve().parent.parent / "relay-repo", env=dict(os.environ), start_after=position)
        state_sha = None
        # The monitor-data head is provenance, never the application checkout.
        try:
            branch = subprocess.run(["git", "-C", str(args.state.parent), "symbolic-ref", "--short", "HEAD"],
                capture_output=True, check=True, timeout=10).stdout.decode().strip()
            if branch == "monitor-data":
                state_sha = subprocess.run(["git", "-C", str(args.state.parent), "rev-parse", "HEAD"],
                    capture_output=True, check=True, timeout=10).stdout.decode().strip()
        except (OSError, subprocess.SubprocessError):
            state_sha = None
        return RemoteStages(mailbox, disk, state, args.run_id,
                            sources_config=read_json(args.config, None), state_sha=state_sha)
    except Exception as exc:
        logging.getLogger(__name__).warning("Satellite relay unavailable (%s)", type(exc).__name__)
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="10店の特価監視。楽天価格取得は対象外。")
    parser.add_argument("command", choices=["collect", "checkpoint", "aggregate", "validate", "ack", "run", "stage", "migrate", "probe"])
    parser.add_argument("--state", type=Path, default=Path("data"))
    parser.add_argument("--public", type=Path, default=Path("public"))
    parser.add_argument("--config", type=Path, default=Path("config/sources.json"))
    parser.add_argument("--routes", type=Path, default=Path("config/routes.json"))
    parser.add_argument("--phase", choices=["discovery", "verification"])
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--input-sha")
    parser.add_argument("--revert", action="store_true")
    parser.add_argument("--route")
    parser.add_argument("--store", choices=STORES)
    parser.add_argument("--incoming", type=Path)
    parser.add_argument("--seconds", type=int, default=2100, help="Runner時間制限。中断位置は永続化し次回再開。Web呼出数の上限ではない。")
    parser.add_argument("--run-id", default=utcnow().strftime("%Y%m%dT%H%M%SZ"))
    parser.add_argument("--event-id", action="append", default=[])
    parser.add_argument("--transport-diagnostic", action="store_true", help="One independent Tsukumo transport comparison after collection; never a price observation")
    args = parser.parse_args(argv)
    if args.transport_diagnostic and (args.command != "collect" or args.store != "tsukumo"):
        parser.error("transport diagnostic requires collect --store tsukumo")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", args.run_id):
        parser.error("invalid run id")
    if args.command == "collect":
        if not args.store:
            parser.error("collect requires --store")
        config = read_json(args.config, None)
        if set(config["stores"]) != set(STORES):
            parser.error("configuration must contain exactly the ten supported stores")
        collector = Collector(args.state, args.store, config, args.run_id)
        result = collector.collect(args.seconds)
        if args.transport_diagnostic:
            from .transport_diagnostic import save_after_collection
            save_after_collection(collector)
        print(json.dumps({"store": args.store, "status": result["status"], "offers": len(result["offers"]), "pending": len(result["queue"]), "errors": result["errors"]}, ensure_ascii=False))
    elif args.command == "run":
        from . import pipeline
        result = pipeline.run(args.state, args.public, args.run_id, config_path=args.config,
                              routes_path=args.routes, remote=_remote(args))
        print(json.dumps({"run_id": args.run_id, "run_outcome": result["run_outcome"],
                          "notification_count": result["notification_count"],
                          "stage_results": result["run"]["stage_results"]}, ensure_ascii=False))
    elif args.command == "stage":
        from . import pipeline
        if not args.store or not args.phase:
            parser.error("stage requires --store and --phase")
        if args.seconds < 0:
            parser.error("stage requires nonnegative --seconds")
        result = pipeline.stage(args.state, args.store, args.phase, args.run_id, args.seconds,
                                args.config, args.routes, args.plan)
        print(json.dumps({"store": args.store, "phase": args.phase, "status": result["status"],
                          "verification": result.get("verification")}, ensure_ascii=False))
    elif args.command == "migrate":
        from . import migrate, routes
        result = migrate.revert(args.state) if args.revert else migrate.apply(
            args.state, routes.RouteState(Store(args.state), routes.load_config(args.routes)), args.input_sha)
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == "probe":
        from . import routes
        from .http import Client
        route_state = routes.RouteState(Store(args.state), routes.load_config(args.routes))
        if args.route not in route_state.routes:
            parser.error("probe requires a known --route")
        route = route_state.routes[args.route]
        if not route.get("probe"):
            config = read_json(args.config, {})
            source = config.get("stores", {}).get(route["store"], {})
            url = next(iter(source.get("seed_urls", [])), source.get("source_url"))
            if not url:
                parser.error("route has no probe URL configured")
            route = {**route, "probe": {"url": url}}
        result = routes.probe(Client(browser=False), args.route, route)
        route_state.record_probe(args.route, result, utcnow())
        route_state.save()
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == "checkpoint":
        if args.incoming is None:
            parser.error("checkpoint requires --incoming")
        print(json.dumps(checkpoint(args.state, args.incoming, args.run_id), ensure_ascii=False))
    elif args.command == "aggregate":
        if args.incoming:
            merge_incoming(args.state, args.incoming)
        result = aggregate(args.state, args.public, args.run_id)
        print(json.dumps({"run_id": args.run_id, "complete_stores": result["complete_stores"], "notifications": result["notification_count"], "review": result["review_count"]}))
    elif args.command == "validate":
        print(json.dumps(validation(args.state), ensure_ascii=False, indent=2))
    else:
        disk = Store(args.state)
        registry = disk.load("events/registry.json", {"states": {}, "events": {}})
        for event_id in args.event_id:
            if event_id not in registry["events"]:
                parser.error("unknown event id")
        for event_id in args.event_id:
            registry["events"][event_id].update(delivery_status="delivered", delivered_at=iso())
        disk.save("events/registry.json", registry)
        print(json.dumps({"acknowledged": args.event_id}))


if __name__ == "__main__":
    main()

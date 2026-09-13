from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from .models import STORES, iso, utcnow
from .reporting import aggregate, merge_incoming, validation
from .runner import Collector
from .storage import Store, read_json


def main(argv=None):
    parser = argparse.ArgumentParser(description="10店の特価監視。楽天価格取得は対象外。")
    parser.add_argument("command", choices=["collect", "aggregate", "validate", "ack"])
    parser.add_argument("--state", type=Path, default=Path("data"))
    parser.add_argument("--public", type=Path, default=Path("public"))
    parser.add_argument("--config", type=Path, default=Path("config/sources.json"))
    parser.add_argument("--store", choices=STORES)
    parser.add_argument("--incoming", type=Path)
    parser.add_argument("--seconds", type=int, default=2100, help="Runner時間制限。中断位置は永続化し次回再開。Web呼出数の上限ではない。")
    parser.add_argument("--run-id", default=utcnow().strftime("%Y%m%dT%H%M%SZ"))
    parser.add_argument("--event-id", action="append", default=[])
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", args.run_id):
        parser.error("invalid run id")
    if args.command == "collect":
        if not args.store:
            parser.error("collect requires --store")
        config = read_json(args.config, None)
        if set(config["stores"]) != set(STORES):
            parser.error("configuration must contain exactly the ten supported stores")
        result = Collector(args.state, args.store, config, args.run_id).collect(args.seconds)
        print(json.dumps({"store": args.store, "status": result["status"], "offers": len(result["offers"]), "pending": len(result["queue"]), "errors": result["errors"]}, ensure_ascii=False))
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

"""Compare saved robots policies offline; URLs are deduplicated within each store."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sale_monitor.budget import Robots


def find_urls(value):
    """Include dictionary keys, since completed tasks can be keyed by URL."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from find_urls(key)
            yield from find_urls(item)
    elif isinstance(value, list):
        for item in value:
            yield from find_urls(item)
    elif isinstance(value, str):
        try:
            parsed = urlsplit(value)
            if parsed.scheme.lower() in ("http", "https") and parsed.hostname:
                yield value
        except ValueError:
            pass


def old_policy(response):
    parser = RobotFileParser()
    try:
        status, body = response
        if status in (401, 403) or status >= 500:
            return False, parser
        if 400 <= status < 500:
            parser.allow_all = True
        elif 200 <= status < 300:
            parser.parse(body.splitlines())
        else:
            return False, parser
        return True, parser
    except Exception:
        return False, parser


def compare(state_root):
    paths = sorted((Path(state_root) / "stores").glob("*.json"))
    if not paths:
        raise ValueError("no store JSON files found")
    hosts = {}
    missing = {}
    stores_without_responses = []
    for path in paths:
        state = json.loads(path.read_text(encoding="utf-8"))
        responses = state.get("robots", {}).get("responses", {})
        if not responses:
            stores_without_responses.append(path.stem)
        # Policy bodies may contain sitemap URLs, which are not store tasks.
        urls = set(find_urls({key: value for key, value in state.items() if key != "robots"}))
        old = {host: old_policy(response) for host, response in responses.items()}

        def saved_response(url):
            return responses[urlsplit(url).hostname]

        robots = Robots(saved_response)
        for host in sorted(set(responses) | {urlsplit(url).hostname for url in urls}):
            hosts.setdefault(host, {"urls_found": 0, "urls_checked": 0, "urls_skipped": 0,
                                    "differences_count": 0, "differences": []})
        for url in sorted(urls):
            host = urlsplit(url).hostname
            summary = hosts[host]
            summary["urls_found"] += 1
            if host not in responses:
                summary["urls_skipped"] += 1
                entry = missing.setdefault(host, {"stores": [], "urls_skipped": 0})
                if path.stem not in entry["stores"]:
                    entry["stores"].append(path.stem)
                entry["urls_skipped"] += 1
                continue
            available, parser = old[host]
            old_allowed = available and parser.can_fetch("PCSaleMonitor", url)
            new_allowed = robots.allowed(url)
            summary["urls_checked"] += 1
            if old_allowed != new_allowed:
                summary["differences_count"] += 1
                if len(summary["differences"]) < 50:
                    summary["differences"].append({"store": path.stem, "url": url,
                                                   "old_allowed": old_allowed,
                                                   "new_allowed": new_allowed})
    return {"stores_read": len(paths), "hosts": dict(sorted(hosts.items())),
            "missing_robots_responses": dict(sorted(missing.items())),
            "stores_without_robots_responses": stores_without_responses}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        summary = compare(args.state)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(summary, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()

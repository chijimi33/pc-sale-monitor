"""Pure queue component: shared resources, explicit dependency and original-age fairness."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from urllib.parse import urlsplit

from sale_monitor.models import allowed_url
from sale_monitor.parsing import confirmed_empty_search, discover, search_form
from .safety import digest


def resource_key(task):
    # A missing URL is not a shared resource: different search identities stay apart.
    return digest([task.get("lab_store"), task.get("url") or [task.get("lab_kind"), task.get("query"), task.get("lab_group")]])


def select_resource(tasks, hosts, now, cursor=0, policy="dependent"):
    groups = defaultdict(list)
    waiting = defaultdict(list)
    for identity, task in tasks.items():
        if task.get("lab_status", "pending") in {"complete", "external_wait"}:
            continue
        if not task.get("url"):
            continue
        host = urlsplit(task["url"]).hostname
        gate = hosts.get(host, {})
        if gate.get("blocked") or gate.get("until", 0) > now:
            waiting[host].append(identity)
        else:
            groups[resource_key(task)].append(identity)
    if policy not in {"cyclic", "dependent"}:
        raise ValueError("Unknown queue policy")
    order = lambda key: (min(tasks[t].get("created_at", "") for t in groups[key]), key)
    candidates = sorted(groups, key=order)
    if policy == "dependent" and candidates:
        requested = [k for k in candidates if any(tasks[t].get("requested") or tasks[t].get("lab_dependencies") for t in groups[k])]
        old = [k for k in candidates if k not in requested]
        lane = old if cursor % 4 == 3 and old else requested or old
        candidates = lane
    selected = candidates[0] if candidates else None
    return {"resource": selected, "task_ids": groups[selected] if selected else [],
            "next_cursor": cursor + bool(selected), "waiting_by_host": dict(waiting),
            "waiting_task_count": sum(len(v) for v in waiting.values()),
            "remaining_task_count": sum(len(v) for v in groups.values()) + sum(len(v) for v in waiting.values())}


def expand_unknown_search(task, home_page, cfg):
    url = search_form(home_page, task["query"])
    if not url or not allowed_url(url) or urlsplit(url).hostname != urlsplit(home_page.url).hostname:
        raise ValueError("Unverified search form destination")
    return {**deepcopy(task), "url": url, "lab_kind": "list", "type": "list", "kind": "comparison",
            "lab_status": "pending", "search_form_evidence": {"url": home_page.url, "observed_at": home_page.observed_at}}


def expand_shared_list(tasks, page, cfg):
    if not tasks:
        raise ValueError("A source dependency is required")
    products, pagination, _ = discover(page, cfg, sale_page=False, comparison=True)
    if not products and not pagination:
        reason = confirmed_empty_search(tasks[0]["lab_store"], page)
        if reason is None:
            raise ValueError("Missing fixture or parser failure is not an empty search result")
        return [], {"confirmed_empty": True, "reason": reason}
    age = min(t["created_at"] for t in tasks)
    parents = sorted({value for t in tasks for value in t.get("lab_dependencies", [])})
    children = []
    for item in products:
        children.append({"lab_store": tasks[0]["lab_store"], "url": item["url"], "type": "product",
                         "lab_kind": "product", "lab_status": "pending", "kind": "comparison",
                         "created_at": age, "lab_dependencies": parents,
                         "source": page.url, "lab_attempts": []})
    for url in pagination:
        children.append({**deepcopy(tasks[0]), "url": url, "created_at": age, "lab_dependencies": parents})
    return children, {"confirmed_empty": False, "shared_source_tasks": len(tasks), "source_url": page.url,
                      "observed_at": page.observed_at}

"""Pure queue component: shared resources, explicit dependency and original-age fairness."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sale_monitor.models import allowed_url, timestamp
from sale_monitor.parsing import confirmed_empty_search, discover, search_form
from .safety import digest


def resource_url(task):
    return task.get('lab_resource_url') or task.get('url')


def age_key(value):
    """Compare instants without rewriting their source timestamp strings.

    Missing ages retain the existing first-position policy, not an invented
    registration date. A populated but invalid timestamp cannot be ordered.
    """
    if value is None or value == '':
        return (0, datetime.min.replace(tzinfo=timezone.utc))
    if not isinstance(value, str) or timestamp(value) is None:
        raise ValueError('Invalid task age: expected an ISO timestamp')
    return (1, timestamp(value).astimezone(timezone.utc))


def earliest_age(values):
    populated = [value for value in values if value is not None and value != '']
    # Text breaks ties only between equal instants, making inherited source
    # representation deterministic across differently ordered parent maps.
    return min(populated, key=lambda value: (age_key(value), value), default='')


def task_age(task):
    return earliest_age((task.get('created_at'), task.get('lab_origin_created_at')))


def task_age_key(task):
    return age_key(task_age(task))


def resource_key(task):
    # A missing URL is not a shared resource: different search identities stay apart.
    return digest([task.get("lab_store"), resource_url(task) or [task.get("lab_kind"), task.get("query"), task.get("lab_group")]])


def select_resource(tasks, hosts, now, cursor=0, policy="dependent"):
    groups = defaultdict(list)
    waiting = defaultdict(list)
    for identity, task in tasks.items():
        if task.get("lab_status", "pending") in {"complete", "external_wait", "evidence_wait"}:
            continue
        if not resource_url(task):
            continue
        host = urlsplit(resource_url(task)).hostname
        gate = hosts.get(host, {})
        if gate.get("blocked") or gate.get("until", 0) > now:
            waiting[host].append(identity)
        else:
            groups[resource_key(task)].append(identity)
    if policy not in {"cyclic", "dependent"}:
        raise ValueError("Unknown queue policy")
    order = lambda key: (min(task_age_key(tasks[t]) for t in groups[key]), key)
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
    comparison = tasks[0].get('kind', 'comparison') == 'comparison'
    sale_page = tasks[0].get('sale_page', False)
    if any((task['lab_store'], task.get('kind', 'comparison'), task.get('sale_page', False)) !=
           (tasks[0]['lab_store'], tasks[0].get('kind', 'comparison'), sale_page) for task in tasks):
        raise ValueError('Shared listing tasks need the same parsing scope')
    products, pagination, campaigns = discover(page, cfg, sale_page=sale_page, comparison=comparison)
    if not products and not pagination and not campaigns:
        if comparison and tasks[0]['lab_store'] == 'sofmap':
            from .deferred_listing import deferred_listing
            deferred = deferred_listing(tasks, page, cfg)
            if deferred is not None:
                return deferred
        reason = confirmed_empty_search(tasks[0]["lab_store"], page) if comparison else None
        if reason is None:
            raise ValueError("Missing fixture or parser failure is not an empty search result")
        if any(task.get('query') and task['query'] != reason for task in tasks):
            raise ValueError('Empty search evidence does not match the requested query')
        return [], {"confirmed_empty": True, "reason": reason}
    age = earliest_age(task_age(t) for t in tasks)
    parents = sorted({value for t in tasks for value in t.get("lab_dependencies", [])})
    children = []
    for item in products:
        children.append({**item, "lab_store": tasks[0]["lab_store"], "type": "product",
                         "lab_kind": "product", "lab_status": "pending",
                         "lab_role": "comparison" if comparison else "candidate",
                         "created_at": age, "lab_dependencies": parents,
                         "source": page.url, "lab_attempts": []})
    for url in pagination:
        children.append({**deepcopy(tasks[0]), "url": url, "lab_resource_url": url, "created_at": age,
                         "lab_kind": "list", "lab_dependencies": parents})
    for url in campaigns:
        children.append({**deepcopy(tasks[0]), "url": url, "lab_resource_url": url, "created_at": age,
                         "lab_kind": "list", "sale_page": True, "lab_dependencies": parents})
    return children, {"confirmed_empty": False, "shared_source_tasks": len(tasks), "source_url": page.url,
                      "observed_at": page.observed_at}

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import os
import time
from urllib.parse import urlsplit

from . import adapters
from .flyers import collect_flyer
from .http import Client, FetchError
from .models import STORES, Offer, allowed_url, digest, fresh, iso, timestamp, utcnow
from .parsing import canonical, confirmed_empty_search, discover, parse_product, search_form
from .storage import Store
from .scheduling import plan_comparison_refresh, select_task
from .retrying import runnable, transient
from . import koubou_search


def task_order(task: dict) -> tuple:
    kind = task["type"]
    if task.get("kind") == "comparison":
        # Resume older comparisons before refreshing newer ones at the same
        # priority. Descendants keep the original request time so a comparison
        # can finish without moving to the back of the next cycle's queue.
        stage = 2
        step = {"product": 0, "list": 1, "yahoo": 1, "koubou_search": 1, "search": 2}.get(kind, 3)
    else:
        stage = 0 if kind in ("list", "dospara_list", "amazon_discovery") else 1
        step = 0
    return stage, task.get("priority", 3), task["created_at"], step


class Collector:
    def __init__(self, root: Path, store: str, config: dict, run_id: str, client: Client | None = None):
        if store not in STORES:
            raise ValueError("excluded_store")
        self.disk = Store(root)
        self.store = store
        self.cfg = config["stores"][store]
        self.config = config
        self.run_id = run_id
        self.client = client or Client(browser=self.cfg.get("browser_fallback", False))
        self.name = f"stores/{store}.json"
        self.state = self.disk.load(self.name, {"schema_version": 1, "store": store, "queue": {}, "done": [], "offers": {}, "journal": [], "cycle_complete": True})
        if isinstance(getattr(self.client, "retry_after", None), dict):
            for host, until in self.state.get("retry_after", {}).items():
                self.client.retry_after[host] = max(self.client.retry_after.get(host, 0), until)
        if isinstance(getattr(self.client, "transport_retry_after", None), dict):
            for host, failure in self.state.get("transport_retry_after", {}).items():
                if failure["until"] > self.client.transport_retry_after.get(host, {}).get("until", 0):
                    self.client.transport_retry_after[host] = failure.copy()
        self.new_run = self.state.get("run_id") != run_id
        scheduler = self.state.setdefault("scheduler", {"cursor": 0})
        if self.new_run:
            scheduler["selected_by_lane"] = {}
            scheduler["selected_by_attempt_kind"] = {}
            self.state["retry_activity"] = {"retries_started": 0, "recovered_tasks": 0, "wait_seconds": 0}
            self.state["recovered_errors"] = []
        self.state.setdefault("retry_activity", {"retries_started": 0, "recovered_tasks": 0, "wait_seconds": 0})
        self.state["run_id"] = run_id
        self.state["started_at"] = iso()
        self.state["status"] = "running"
        if self.new_run:
            self.state["errors"] = []
        self.state.setdefault("errors", [])
        self.attempted = {key for key, task in self.state["queue"].items()
                          if task.get("last_attempt_run_id") == run_id}
        self.pages = {}
        self.page_failures = {}
        self.page_failure_retry_at = {}

    def save(self):
        self.state["checkpoint_at"] = iso()
        self.state["pending_count"] = len(self.state["queue"])
        self.state["request_count"] = self.client.count
        if isinstance(getattr(self.client, "retry_after", None), dict):
            self.state["retry_after"] = {host: until for host, until in self.client.retry_after.items() if until > time.time()}
        if isinstance(getattr(self.client, "transport_retry_after", None), dict):
            self.state["transport_retry_after"] = {host: failure.copy() for host, failure in self.client.transport_retry_after.items() if failure["until"] > time.time()}
        self.disk.save(self.name, self.state)

    def enqueue(self, task: dict):
        if task.get("url") and not allowed_url(task["url"]):
            return
        task_id = digest([task["type"], task.get("url"), task.get("query"), task.get("start"), task.get("kind"), task.get("item", {}).get("product_id")])[:24]
        if task_id in self.state["queue"]:
            existing = self.state["queue"][task_id]
            if task.get("priority", 3) < existing.get("priority", 3):
                existing["priority"] = task["priority"]
            if task.get("created_at") and task["created_at"] < existing["created_at"]:
                existing["created_at"] = task["created_at"]
            if task.get("requested"):
                existing["requested"] = True
            if task.get("discovery_origin"):
                prior = existing.get("discovery_origin")
                if not prior or task["discovery_origin"]["created_at"] < prior["created_at"]:
                    existing["discovery_origin"] = deepcopy(task["discovery_origin"])
        elif task_id not in self.state["done"]:
            self.state["queue"][task_id] = {"created_at": iso(), "attempts": 0, **task}

    def record(self, offer: Offer):
        offer.observed_run_id = self.run_id
        aliases = self.config.get("seller_aliases", {})
        if offer.seller_id in aliases:
            offer.seller_id = aliases[offer.seller_id]
        elif offer.seller_id and ":" in offer.seller_id:
            offer.issues.append("seller_identity_review_needed")
        row = offer.to_dict()
        previous = self.state["offers"].get(offer.key)
        if previous and previous.get("discovery_kind") == "sale" and offer.discovery_kind == "comparison":
            offer.discovery_kind = "sale"
            offer.discovery_url = previous.get("discovery_url")
            row = offer.to_dict()
        self.state["offers"][offer.key] = row
        observation_id = digest([offer.key, offer.observed_at, row])
        self.state["journal"].append({"observation_id": observation_id, "run_id": self.run_id, "offer": deepcopy(row)})
        if previous is None:
            priority = "new"
        elif previous.get("stock") == "out_of_stock" and offer.stock == "in_stock":
            priority = "restocked"
        elif type(previous.get("price_yen")) is int and type(offer.price_yen) is int and offer.price_yen < previous["price_yen"]:
            priority = "price_down"
        else:
            priority = "unchanged"
        self.state.setdefault("changes", {})[offer.key] = priority

    def seed(self):
        requests = self.disk.load(f"requests/{self.store}.json", None)
        candidates = self.disk.load("requests/candidate_identities.json", [])
        candidates = {x for x in candidates if isinstance(x, str)} if isinstance(candidates, list) else set()
        priorities = {r["identity"]: r.get("priority", 3) for r in requests or []
                      if isinstance(r, dict) and isinstance(r.get("identity"), str)}
        if self.new_run or self.state.get("cycle_complete"):
            # Keep unfinished work (and its original age), while allowing fresh
            # list discovery even when one old URL repeatedly fails.
            self.state.update(done=[], changes={}, cycle_started_at=iso(), cycle_complete=False, list_pages=0, listed_candidates=0, discovery_gaps=[])
            for task in self.state["queue"].values():
                task.pop("requested", None)
            adapter = self.cfg["adapter"]
            if adapter == "html":
                for url in self.cfg.get("seed_urls", []):
                    home = urlsplit(url).path in ("", "/", "/bc/main/")
                    self.enqueue({"type": "list", "url": url, "sale_page": not home, "depth": 0})
            elif adapter == "dospara":
                for url in self.cfg["seed_urls"]:
                    self.enqueue({"type": "dospara_list", "url": url})
            elif adapter == "yahoo":
                for query in self.cfg["queries"]:
                    self.enqueue({"type": "yahoo", "query": query, "start": 1})
            elif adapter == "amazon":
                self.enqueue({"type": "amazon_discovery"})
            refresh, refresh_details = plan_comparison_refresh(self.state["offers"], requests, candidates)
            self.state["comparison_refresh"] = refresh_details
            for key, row in self.state["offers"].items():
                if row.get("channel") != "online" or adapter in ("dospara", "yahoo"):
                    continue
                # Recheck known sale products and exact comparison matches only.
                # Existing queued work is retained even when its old identity is
                # no longer requested; it must still complete normal verification.
                if row.get("discovery_kind") == "comparison" and key not in refresh:
                    continue
                task = {"type": "product", "url": row["url"], "kind": row.get("discovery_kind", "sale"), "source": row.get("discovery_url"), "title": row.get("title", "")}
                identity = Offer.from_dict(row).identity
                if task["kind"] == "comparison" and (identity in priorities or identity in candidates):
                    task.update(requested=True, priority=priorities.get(identity, 3))
                self.enqueue(task)
        for query in requests or []:
            if self.cfg["adapter"] == "yahoo":
                self.enqueue({"type": "yahoo", "query": query["query"], "start": 1, "kind": "comparison", "priority": query.get("priority", 3), "requested": True})
            elif self.cfg["adapter"] == "html":
                self.enqueue({"type": "search", "query": query["query"], "kind": "comparison", "priority": query.get("priority", 3), "requested": True})
        self.save()

    def page(self, url: str, *, allow_browser: bool = True):
        if url in self.page_failures:
            until = self.page_failure_retry_at.get(url)
            if until is None or time.time() < until:
                failure = self.page_failures[url]
                raise failure if isinstance(failure, FetchError) else FetchError(failure)
            # A later task using the same page may recover after the cooldown.
            # The client still enforces any newer host-wide waiting deadline.
            self.page_failures.pop(url)
            self.page_failure_retry_at.pop(url)
        if url not in self.pages:
            try:
                try:
                    self.pages[url] = self.client.get(url)
                except FetchError as exc:
                    if not allow_browser or str(exc) == "rate_limited_retry_later" or str(exc).startswith("transport_retry_later:") or (exc.page is not None and confirmed_empty_search(self.store, exc.page)) or not self.cfg.get("browser_fallback"):
                        raise
                    self.pages[url] = self.client.rendered(url)
            except Exception as exc:
                failure = exc if isinstance(exc, FetchError) else type(exc).__name__
                self.page_failures[url] = failure
                reason = str(failure)
                if transient(reason):
                    host = urlsplit(url).hostname
                    # Deduplicate immediate failures without caching a temporary
                    # outage for the entire run. Never shorten Retry-After.
                    self.page_failure_retry_at[url] = max(time.time() + 300,
                        getattr(self.client, "retry_after", {}).get(host, 0),
                        getattr(self.client, "transport_retry_after", {}).get(host, {}).get("until", 0))
                raise
        return self.pages[url]

    def process_koubou_search(self, task: dict, query: str, offset: int = 0):
        url = koubou_search.search_url(query, offset)
        products, next_offset, details = koubou_search.parse_search(
            self.page(url, allow_browser=False), query, offset)
        origin = {"created_at": task.get("created_at") or iso(), "priority": task.get("priority", 3)}
        for product in products:
            self.enqueue({"type": "product", **product, **origin})
        if next_offset is not None:
            self.enqueue({"type": "koubou_search", "kind": "comparison", "query": query,
                          "start": next_offset, "url": koubou_search.search_url(query, next_offset), **origin})
        self.state.setdefault("comparison_searches", {})[url] = {**details, "observed_run_id": self.run_id,
            "original_task_url": task.get("url"), "created_at": origin["created_at"]}
        self.state["list_pages"] += 1
        self.state["listed_candidates"] += len(products)

    def process(self, task: dict):
        kind = task["type"]
        if self.store == "koubou" and task.get("kind") == "comparison":
            query = (task.get("query") if kind in ("search", "koubou_search") else
                     koubou_search.legacy_query(task["url"]) if kind == "list" else None)
            if query:
                self.process_koubou_search(task, query, task.get("start", 0))
                return
        if kind == "dospara_list":
            items, links = adapters.dospara_list(self.client, task["url"])
            for item in items:
                self.enqueue({"type": "dospara_product", "item": item})
            for url in links:
                self.enqueue({"type": "dospara_list", "url": url})
            self.state["list_pages"] += 1
            self.state["listed_candidates"] += len(items)
        elif kind == "dospara_product":
            offer = adapters.dospara_product(self.client, task["item"])
            if offer:
                self.record(offer)
            else:
                raise FetchError("product_unavailable")
        elif kind == "amazon_discovery":
            candidates, details = adapters.amazon_candidates(self.client, self.cfg["source_url"])
            previous = self.state.get("source_metadata", {})
            details["source_unchanged"] = previous.get("source_updated_at") == details.get("source_updated_at")
            details["stale_source"] = not fresh(details.get("source_updated_at"), utcnow(), 18)
            self.state["source_metadata"] = details
            self.state["list_pages"] += 1
            self.state["listed_candidates"] += len(candidates)
            for candidate in candidates:
                self.enqueue({"type": "product", **candidate})
        elif kind == "yahoo":
            offers, next_page, details = adapters.yahoo_page(self.client, task["query"], task.get("start", 1), comparison=task.get("kind") == "comparison")
            for offer in offers:
                self.record(offer)
            self.state["list_pages"] += 1
            self.state["listed_candidates"] += len(offers)
            if details.get("provider_window_truncated"):
                self.state["discovery_gaps"].append({"reason": "yahoo_provider_window", "query": task["query"], **details})
            if next_page:
                self.enqueue({**task, "start": next_page})
        elif kind == "search":
            home = self.page(self.cfg["seed_urls"][0])
            url = search_form(home, task["query"])
            if not url:
                raise FetchError("search_form_not_found")
            self.enqueue({"type": "list", "url": url, "sale_page": False, "kind": "comparison", "depth": 0, "priority": task.get("priority", 3), "created_at": task.get("created_at") or iso()})
        elif kind == "list":
            # Explicit, reviewed entry changes apply only to sale discovery.
            # Keep the old queued task and age until replacement discovery
            # succeeds; an HTTP/parse failure must still leave it pending.
            replacement = self.cfg.get("list_url_replacements", {}).get(task["url"]) if task.get("sale_page") and task.get("kind") != "comparison" else None
            fetch_url = replacement or task["url"]
            try:
                page = self.page(fetch_url)
            except FetchError as exc:
                if task.get("kind") != "comparison" or exc.page is None or not confirmed_empty_search(self.store, exc.page):
                    raise
                page = exc.page
            empty_query = confirmed_empty_search(self.store, page) if task.get("kind") == "comparison" else None
            if empty_query:
                self.state.setdefault("comparison_searches", {})[canonical(task["url"])] = {"query": empty_query, "result": "no_results",
                    "observed_at": page.observed_at, "observed_run_id": self.run_id, "http_status": page.status, "content_hash": digest(page.text)}
                self.state["list_pages"] += 1
                return
            products, pagination, campaigns = discover(page, self.cfg, sale_page=task.get("sale_page", False), comparison=task.get("kind") == "comparison")
            if not products and task.get("sale_page") and self.cfg.get("browser_fallback") and page.method != "browser":
                page = self.client.rendered(fetch_url)
                products, pagination, campaigns = discover(page, self.cfg, sale_page=True, comparison=task.get("kind") == "comparison")
            if replacement and not products:
                raise FetchError("discovery_replacement_unverified")
            if not products and not campaigns and task.get("kind") != "comparison":
                self.state["discovery_gaps"].append({"url": task["url"], "reason": "no_sale_candidates_parser_review"})
            if self.store == "koubou" and task.get("kind") == "comparison" and not products:
                # A filtered/unsupported search URL must not silently complete
                # from an empty JavaScript shell, nor lose its filter conditions.
                raise FetchError("comparison_search_response_unverified")
            self.state["list_pages"] += 1
            self.state["listed_candidates"] += len(products)
            discovery_origin = task.get("discovery_origin")
            if replacement:
                discovery_origin = {"url": task["url"], "created_at": task["created_at"]}
            origin = {"created_at": task["created_at"]} if task.get("kind") == "comparison" and task.get("created_at") else {}
            if discovery_origin:
                discovery_origin = {**discovery_origin, "created_at": min(discovery_origin["created_at"], task["created_at"])}
                origin = {"created_at": discovery_origin["created_at"], "discovery_origin": discovery_origin}
            for product in products:
                self.enqueue({"type": "product", **product, "priority": task.get("priority", 3), **origin})
            for url in pagination:
                self.enqueue({**task, "url": url, **origin})
            # Follow explicitly linked sale pages, including nested sale portals.
            # The queue deduplicates cycles; ordinary category links stay excluded.
            for url in campaigns:
                campaign_origin = origin if discovery_origin else {}
                self.enqueue({"type": "list", "url": url, "sale_page": True, "depth": task.get("depth", 0) + 1, **campaign_origin})
            if replacement:
                self.state.setdefault("discovery_replacements", {})[task["url"]] = {
                    "original_task": deepcopy(task), "replacement_url": page.url,
                    "observed_at": page.observed_at, "observed_run_id": self.run_id,
                    "http_status": page.status, "content_hash": digest(page.text),
                    "product_count": len(products), "pagination": pagination,
                    "campaigns": campaigns, "result": "discovered_products_enqueued"}
        elif kind == "product":
            if self.store == "amazon":
                offer = adapters.amazon_product(self.client, task)
            else:
                page = self.page(task["url"])
                offer = parse_product(self.store, page, self.cfg, task)
                if not offer.verified and self.cfg.get("browser_fallback") and page.method != "browser":
                    offer = parse_product(self.store, self.client.rendered(task["url"]), self.cfg, task)
            self.record(offer)
        else:
            raise ValueError("unknown_task_type")

    def collect(self, seconds: int = 2100, review_root: Path = Path("config/flyer_reviews")) -> dict:
        self.seed()
        deadline = time.monotonic() + seconds
        if self.store == "yahoo":
            self.state["errors"] = [e for e in self.state["errors"]
                                    if e.get("reason") != "YAHOO_CLIENT_ID_missing"]
            if not os.environ.get("YAHOO_CLIENT_ID"):
                self.state["status"] = "configuration_needed"
                self.state["errors"].append({"reason": "YAHOO_CLIENT_ID_missing"})
                self.save()
                return self.state
        while (tick := time.monotonic()) < deadline:
            wall_now = time.time()
            pending, waits = runnable(self.state["queue"], self.attempted, self.cfg, self.client,
                                      self.page_failure_retry_at, wall_now)
            self.state["waiting_dependencies"] = waits
            if not pending:
                if not waits:
                    break
                # A dependency wait is not a request attempt or a resolved task.
                # Preserve it across interruption and use the remaining run time
                # to recover, instead of exhausting every task during cooldown.
                self.save()
                delay = min(w["until_epoch_seconds"] for w in waits) - wall_now
                if delay >= deadline - tick:
                    break
                delay = min(delay, 30)
                time.sleep(delay)
                self.state["retry_activity"]["wait_seconds"] += delay
                continue
            task_id, task = select_task(pending, self.state["scheduler"], task_order, utcnow(), self.attempted)
            self.attempted.add(task_id)
            if task.get("retry_at_epoch_seconds") is not None:
                self.state["retry_activity"]["retries_started"] += 1
            try:
                self.process(task)
                if task.get("last_error"):
                    self.state["retry_activity"]["recovered_tasks"] += 1
                    self.state.setdefault("recovered_errors", []).append({"task_id": task_id,
                        "reason": task["last_error"], "attempts": task.get("attempts", 0), "recovered_at": iso()})
                    self.state["errors"] = [e for e in self.state["errors"] if e.get("task_id") != task_id]
                self.state["done"].append(task_id)
                del self.state["queue"][task_id]
            except Exception as exc:
                # Only sanitized error types/codes are persisted; URLs with API
                # credentials and raw exception messages never enter the feed.
                reason = str(exc) if isinstance(exc, FetchError) and "http" not in str(exc)[5:] and len(str(exc)) < 100 else type(exc).__name__
                task.update(attempts=task.get("attempts", 0)+1, last_error=reason, last_attempt_at=iso(),
                            last_attempt_run_id=self.run_id)
                task.pop("retry_at_epoch_seconds", None)
                if transient(reason):
                    task["retry_at_epoch_seconds"] = time.time() + 300
                # Error counts describe unresolved tasks, not repeated attempts.
                self.state["errors"] = [e for e in self.state["errors"] if e.get("task_id") != task_id]
                self.state["errors"].append({"task_id": task_id, "reason": reason, "url": task.get("url")})
                if task["type"] == "product":
                    for row in self.state["offers"].values():
                        if canonical(row["url"]) == canonical(task["url"]):
                            row["issues"] = sorted(set(row.get("issues", []) + ["latest_fetch_failed"]))
            self.save()
        if self.store == "koubou":
            try:
                offers, metadata = collect_flyer(self.client, self.disk, self.config["flyer"], review_root)
                self.state["flyer"] = metadata
                for offer in offers:
                    self.record(offer)
            except Exception as exc:
                self.state["flyer"] = {"status": "fetch_failed", "reason": type(exc).__name__, "checked_at": iso()}
        self.state["cycle_complete"] = not self.state["queue"]
        _, self.state["waiting_dependencies"] = runnable(self.state["queue"], self.attempted, self.cfg,
            self.client, self.page_failure_retry_at, time.time())
        self.state["status"] = "complete" if self.state["cycle_complete"] and not self.state.get("discovery_gaps") else "partial"
        self.state["completed_at"] = iso()
        self.save()
        return self.state

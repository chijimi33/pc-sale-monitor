from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import os
import time
from urllib.parse import urlsplit

from . import adapters
from .flyers import collect_flyer
from .http import Client, FetchError
from .models import STORES, Offer, allowed_url, digest, fresh, iso, timestamp, utcnow
from .parsing import canonical, discover, parse_product, search_form
from .storage import Store


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
        self.new_run = self.state.get("run_id") != run_id
        self.state["run_id"] = run_id
        self.state["started_at"] = iso()
        self.state["status"] = "running"
        self.state["errors"] = []
        self.attempted = set()
        self.pages = {}
        self.page_failures = {}

    def save(self):
        self.state["checkpoint_at"] = iso()
        self.state["pending_count"] = len(self.state["queue"])
        self.state["request_count"] = self.client.count
        self.disk.save(self.name, self.state)

    def enqueue(self, task: dict):
        if task.get("url") and not allowed_url(task["url"]):
            return
        task_id = digest([task["type"], task.get("url"), task.get("query"), task.get("start"), task.get("kind"), task.get("item", {}).get("product_id")])[:24]
        if task_id not in self.state["queue"] and task_id not in self.state["done"]:
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
        self.state["journal"].append({"observation_id": observation_id, "run_id": self.run_id, "offer": row})
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
        if self.new_run or self.state.get("cycle_complete"):
            # Keep unfinished work (and its original age), while allowing fresh
            # list discovery even when one old URL repeatedly fails.
            self.state.update(done=[], changes={}, cycle_started_at=iso(), cycle_complete=False, list_pages=0, listed_candidates=0, discovery_gaps=[])
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
            for row in self.state["offers"].values():
                if row.get("channel") != "online" or adapter in ("dospara", "yahoo"):
                    continue
                # Recheck known sale products and exact comparison matches only.
                self.enqueue({"type": "product", "url": row["url"], "kind": row.get("discovery_kind", "sale"), "source": row.get("discovery_url"), "title": row.get("title", "")})
        for query in self.disk.load(f"requests/{self.store}.json", []):
            if self.cfg["adapter"] == "yahoo":
                self.enqueue({"type": "yahoo", "query": query["query"], "start": 1, "kind": "comparison"})
            elif self.cfg["adapter"] == "html":
                self.enqueue({"type": "search", "query": query["query"], "kind": "comparison", "priority": query.get("priority", 3)})
        self.save()

    def page(self, url: str):
        if url in self.page_failures:
            raise FetchError(self.page_failures[url])
        if url not in self.pages:
            try:
                try:
                    self.pages[url] = self.client.get(url)
                except FetchError:
                    if not self.cfg.get("browser_fallback"):
                        raise
                    self.pages[url] = self.client.rendered(url)
            except Exception as exc:
                self.page_failures[url] = str(exc) if isinstance(exc, FetchError) else type(exc).__name__
                raise
        return self.pages[url]

    def process(self, task: dict):
        kind = task["type"]
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
            self.enqueue({"type": "list", "url": url, "sale_page": False, "kind": "comparison", "depth": 0})
        elif kind == "list":
            page = self.page(task["url"])
            products, pagination, campaigns = discover(page, self.cfg, sale_page=task.get("sale_page", False), comparison=task.get("kind") == "comparison")
            if not products and task.get("sale_page") and self.cfg.get("browser_fallback") and page.method != "browser":
                page = self.client.rendered(task["url"])
                products, pagination, campaigns = discover(page, self.cfg, sale_page=True, comparison=task.get("kind") == "comparison")
            if not products and not campaigns and task.get("kind") != "comparison":
                self.state["discovery_gaps"].append({"url": task["url"], "reason": "no_sale_candidates_parser_review"})
            self.state["list_pages"] += 1
            self.state["listed_candidates"] += len(products)
            for product in products:
                self.enqueue({"type": "product", **product})
            for url in pagination:
                self.enqueue({**task, "url": url})
            # Follow explicitly linked sale pages, including nested sale portals.
            # The queue deduplicates cycles; ordinary category links stay excluded.
            for url in campaigns:
                self.enqueue({"type": "list", "url": url, "sale_page": True, "depth": task.get("depth", 0) + 1})
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
        if self.store == "yahoo" and not os.environ.get("YAHOO_CLIENT_ID"):
            self.state["status"] = "configuration_needed"
            self.state["errors"] = [{"reason": "YAHOO_CLIENT_ID_missing"}]
            self.save()
            return self.state
        while time.monotonic() < deadline:
            pending = [(k, v) for k, v in self.state["queue"].items() if k not in self.attempted]
            if not pending:
                break
            priorities = {"list": 0, "dospara_list": 0, "amazon_discovery": 0, "yahoo": 1, "product": 1, "dospara_product": 1, "search": 2}
            task_id, task = min(pending, key=lambda kv: (priorities.get(kv[1]["type"], 3), kv[1].get("priority", 3), kv[1]["created_at"]))
            self.attempted.add(task_id)
            try:
                self.process(task)
                self.state["done"].append(task_id)
                del self.state["queue"][task_id]
            except Exception as exc:
                # Only sanitized error types/codes are persisted; URLs with API
                # credentials and raw exception messages never enter the feed.
                reason = str(exc) if isinstance(exc, FetchError) and "http" not in str(exc)[5:] and len(str(exc)) < 100 else type(exc).__name__
                task.update(attempts=task.get("attempts", 0)+1, last_error=reason, last_attempt_at=iso())
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
        self.state["status"] = "complete" if self.state["cycle_complete"] and not self.state.get("discovery_gaps") else "partial"
        self.state["completed_at"] = iso()
        self.save()
        return self.state

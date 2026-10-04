"""Bounded, read-only Sofmap search/product trial; never reads production state."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import time
from urllib.parse import quote

from sale_monitor.http import Client, FetchError
from sale_monitor.runner import Collector

CASES = [('4711289500124', '23812490'), ('0195553309745', '100788840')]
# An exact unresolved comparison request from monitor-data f496e3b; keep the
# complete product string when checking Japanese query/empty-result handling.
CASES.append(('ドスパラセレクト PG5-010TA1-MC (M.2 2280 Gen5 1TB)', None))
HOST = 'https://www.sofmap.com'


class CaptureClient:
    def __init__(self, output, budget=120):
        self.inner = Client(delay=2, timeout=15)
        self.output, self.deadline = output, time.monotonic() + budget
        self.receipts, self.blocked = [], None
        self.allowed = set()
        for query, sku in CASES:
            encoded = quote(query, safe='')
            self.allowed.update([HOST+'/search_result.aspx?keyword='+encoded,
                HOST+'//product_list_parts.aspx?keyword='+encoded+'&is_page=serch_result&isFirst=true'])
            if sku:
                self.allowed.add(HOST+'/product_detail.aspx?sku='+sku)

    @property
    def count(self):
        return self.inner.count

    @property
    def retry_after(self):
        return self.inner.retry_after

    @property
    def transport_retry_after(self):
        return self.inner.transport_retry_after

    def get(self, url):
        if url not in self.allowed or len(self.receipts) >= 8:
            raise FetchError('probe_resource_outside_scope')
        if self.blocked:
            raise FetchError('probe_shared_access_stop')
        if time.monotonic() >= self.deadline:
            raise FetchError('probe_budget_exhausted')
        started, count = time.monotonic(), self.count
        row = {'url':url, 'started_at':datetime.now(timezone.utc).isoformat()}
        try:
            response = self.inner.get(url)
            body_hash = hashlib.sha256(response.body).hexdigest()
            path = 'bodies/'+body_hash+'.body'
            (self.output/path).parent.mkdir(parents=True, exist_ok=True)
            (self.output/path).write_bytes(response.body)
            row.update(status=response.status, response_url=response.url, observed_at=response.observed_at,
                       content_type=response.content_type, body_file=path, body_sha256=body_hash,
                       body_bytes=len(response.body))
            return response
        except FetchError as exc:
            row['error'] = str(exc)
            if str(exc) in {'http_401', 'http_403', 'rate_limited_retry_later'}:
                self.blocked = str(exc)
            raise
        finally:
            row.update(elapsed_seconds=time.monotonic()-started, http_attempts=self.count-count)
            self.receipts.append(row)


def run(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    config = json.loads(Path('config/sources.json').read_text(encoding='utf-8'))
    client = CaptureClient(output)
    run_id = 'probe-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
    collector = Collector(output/'isolated-state', 'sofmap', config, run_id, client)
    collector.state['list_pages'], collector.state['listed_candidates'] = 0, 0
    results = []
    for query, sku in CASES:
        search = {'type':'list', 'kind':'comparison', 'sale_page':False,
                  'url':HOST+'/search_result.aspx?keyword='+quote(query, safe=''), 'search_query':query,
                  'created_at':datetime.now(timezone.utc).isoformat()}
        row = {'query':query}
        try:
            collector.process(search)
            row['search'] = 'verified_results'
            if sku is None:
                row['product'] = 'outside_probe_scope'
                results.append(row)
                continue
            product_url = HOST+'/product_detail.aspx?sku='+sku
            products = [(key,t) for key,t in collector.state['queue'].items()
                        if t['type']=='product' and t['url']==product_url]
            if len(products) == 1:
                try:
                    key, task = products[0]
                    collector.process(task)
                    collector.state['done'].append(key)
                    del collector.state['queue'][key]
                    row['product'] = 'observed'
                except FetchError as exc:
                    row['product_error'] = str(exc)
            else:
                row['product_error'] = 'expected_product_not_discovered'
        except FetchError as exc:
            row['search_error'] = str(exc)
        results.append(row)
    collector.save()
    summary = {'schema':1, 'run_id':run_id, 'environment':platform.system(),
               'github_run_id':os.environ.get('GITHUB_RUN_ID'), 'github_event':os.environ.get('GITHUB_EVENT_NAME'),
               'source_commit':os.environ.get('SOURCE_COMMIT'), 'read_only_production':True,
               'http_attempts':client.count, 'resource_calls':len(client.receipts), 'access_stop':client.blocked,
               'results':results, 'receipts':client.receipts,
               'searches':collector.state.get('comparison_searches',{}),
               'observations':[{k:v for k,v in x.items() if k in
                   {'product_id','url','jan','condition','stock','price_yen','shipping_yen','points_yen','verified','issues','observed_at','observed_run_id'}}
                   for x in collector.state['offers'].values()],
               'unprocessed_dependencies':list(collector.state['queue'].values()),
               'limits':['Three fixed queries and only two known product URLs; at most eight resources.',
                         'Grouped/filter searches retained but not acquired by this bounded trial.',
                         'Probe observations are isolated and never used for production prices or scheduled stability.']}
    (output/'result.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print('SOFMAP_PROBE_RESULT='+json.dumps(summary, ensure_ascii=True, separators=(',',':')), flush=True)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    run(parser.parse_args().output)

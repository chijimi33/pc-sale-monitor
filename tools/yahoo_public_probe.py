"""Six anonymous public GETs; isolated evidence only, no production state."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode

from sale_monitor.http import Client, FetchError
from sale_monitor.models import iso
from sale_monitor import yahoo_public


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--form-entry-only', action='store_true', help='Compare the published search form entry; do not change production routing.')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    client = Client(browser=False)
    result = {'scope': 'isolated_public_html_probe', 'source_commit': os.environ.get('SOURCE_COMMIT'),
              'started_at': iso(), 'scheduled_credit': False, 'production_writes': False, 'results': []}
    targets = [('comparison', yahoo_public.search_url('0192876826362'), '0192876826362', 1),
               ('sale', yahoo_public.search_url('セール SSD'), 'セール SSD', 1),
               ('sale-page2', yahoo_public.search_url('セール SSD', 2), 'セール SSD', 2),
               ('product-0', 'https://store.shopping.yahoo.co.jp/pc-koubou/m5s1p760370.html', None, None),
               ('product-1', 'https://store.shopping.yahoo.co.jp/janpara/226079552-222.html', None, None),
               ('product-2', 'https://store.shopping.yahoo.co.jp/jnh/mc8012p1-500g.html', None, None)]
    if args.form_entry_only:
        targets = [(name, yahoo_public.SEARCH + '/search?' + urlencode({'p': query}), query, number)
                   for name, _, query, number in targets[:2]]
    for name, url, query, number in targets:
        row = {'name': name, 'requested_url': url}
        try:
            page = client.get(url)
            (args.output / (name + '.html')).write_bytes(page.body)
            row.update(final_url=page.url, observed_at=page.observed_at, http_status=page.status,
                       body_sha256=hashlib.sha256(page.body).hexdigest(), body_bytes=len(page.body))
            if query:
                products, next_url, details = yahoo_public.parse_search(page, query, number,
                    comparison=name == 'comparison')
                row.update(products=products, next_url=next_url, details=details)
            else:
                offer = yahoo_public.parse_product(page, {'url': url, 'kind': 'comparison'})
                row['offer'] = offer.to_dict()
                if not offer.verified or offer.price_yen is None or offer.stock == 'unknown':
                    raise FetchError('yahoo_probe_required_fields_unverified')
                if offer.shipping_yen is not None:
                    raise FetchError('yahoo_probe_unexpected_shipping_assumption')
            row['result'] = 'verified'
        except Exception as exc:
            row.update(result='unverified', error=str(exc) if isinstance(exc, FetchError) else type(exc).__name__)
            cause = exc.__cause__
            if isinstance(cause, HTTPError):
                row['http_status'] = cause.code
                try:
                    # Read the already received final error body; no extra
                    # request, header change, login or browser retry.
                    body = cause.read(262144)
                    (args.output / (name + '.error-body')).write_bytes(body)
                    row['error_body_sha256'] = hashlib.sha256(body).hexdigest()
                    row['error_body_bytes_up_to_262144'] = len(body)
                except Exception:
                    row['error_body_unavailable'] = True
        result['results'].append(row)
        # Durable after each GET; no raw HTML, session data or tokens in stdout.
        result.update(completed_at=iso(), requests=client.count, http_evidence=client.evidence.snapshot())
        (args.output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({k: row[k] for k in ('name', 'result', 'http_status', 'error') if k in row}), flush=True)
    return 0 if all(r['result'] == 'verified' for r in result['results']) else 1


if __name__ == '__main__':
    raise SystemExit(main())

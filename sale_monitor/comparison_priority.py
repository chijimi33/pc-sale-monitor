"""Carry active search priority through result pages without resolving tasks."""
from urllib.parse import parse_qs, urlsplit


def queries(task, store):
    if task.get('kind') != 'comparison':
        return []
    saved = task.get('comparison_queries', [])
    values = set(q for q in saved if isinstance(q, str) and q) if isinstance(saved, list) else set()
    if task.get('type') in ('search', 'koubou_search', 'yahoo', 'yahoo_public_search') and isinstance(task.get('query'), str):
        values.add(task['query'])
    if store == 'sofmap':
        query = task.get('search_query') or task.get('query')
        if isinstance(query, str):
            values.add(query)
    # Previously persisted Tsukumo result pages lack origin metadata. Recover
    # only the exact keyword from their known public search URL. This establishes
    # scheduling relevance, never a product match or a verified search result.
    if store == 'tsukumo':
        url = task.get('url') if task.get('type') == 'list' else task.get('source')
        try:
            parts = urlsplit(url if isinstance(url, str) else '')
        except ValueError:
            return sorted(values - {''})
        if parts.scheme == 'https' and parts.netloc == 'shop.tsukumo.co.jp' and parts.path == '/search':
            keyword = parse_qs(parts.query).get('keyword', [])
            if len(keyword) == 1:
                values.add(keyword[0])
    return sorted(values - {''})


def origin(task, store):
    if task.get('kind') != 'comparison':
        return {}
    result = {'priority': task.get('priority', 3)}
    if task.get('created_at'):
        result['created_at'] = task['created_at']
    related = queries(task, store)
    if related:
        result['comparison_queries'] = related
    if task.get('requested'):
        result['requested'] = True
    return result


def refresh(queue, requests, store):
    active = {}
    for row in requests or []:
        if isinstance(row, dict) and isinstance(row.get('query'), str) and row['query']:
            active[row['query']] = min(active.get(row['query'], 3), row.get('priority', 3))
    for task in queue.values():
        task.pop('requested', None)
        related = queries(task, store)
        priorities = [active[q] for q in related if q in active]
        if priorities:
            task['requested'] = True
            task['priority'] = min(task.get('priority', 3), *priorities)

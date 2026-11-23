"""Read-only, source-addressed historical evidence for the isolated evaluator.

The index retains row locations, not complete historical HTML/offer payloads.
Only matching rows are loaded for a decision. Current offers always come from
the collection's observations; historical prices never enter that namespace.
"""
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from sale_monitor.engine import amount
from sale_monitor.models import Offer, iso, same_product, timestamp
from .safety import digest

PROFILE = ('store', 'product_id', 'url', 'seller_id', 'channel', 'jan', 'brand', 'model', 'condition', 'variant', 'warranty')


def profile(offer):
    return {name: getattr(offer, name) for name in PROFILE}


class HistoryIndex:
    def __init__(self, source_files, now):
        self.sources = {name: deepcopy(value) for name, value in sorted(source_files.items())
                        if name.startswith('state/history/') and name.endswith('.json')}
        self.by_identity = defaultdict(list)
        self.latest = {}
        self.stats = Counter(files=len(self.sources), rows=0, duplicate_rows=0, identity_missing_rows=0)
        self.duplicates = []
        self._cache = {}
        seen = {}
        for name in self.sources:
            rows = self._read(name)
            self.stats['rows'] += len(rows)
            for index, row in enumerate(rows):
                if not isinstance(row, dict) or not isinstance(row.get('offer'), dict):
                    raise ValueError('Malformed historical observation: ' + name + ':' + str(index))
                source = (name, index)
                identity = row.get('observation_id')
                if not isinstance(identity, str) or not identity:
                    raise ValueError('Historical observation identity is missing')
                if identity in seen:
                    prior_name, prior_index = seen[identity]
                    prior = rows[prior_index] if prior_name == name else self._read(prior_name)[prior_index]
                    if prior != row:
                        raise ValueError('Conflicting historical observation identity: ' + identity)
                    self.duplicates.append({'observation_id': identity, 'original': list(seen[identity]), 'duplicate': list(source)})
                    self.stats['duplicate_rows'] += 1
                    continue
                seen[identity] = source
                offer = self._offer(row)
                if offer.identity:
                    self.by_identity[offer.identity].append(source)
                else:
                    self.stats['identity_missing_rows'] += 1
                checked = timestamp(offer.observed_at)
                if checked is None or checked > now or offer.channel != 'online':
                    continue
                # Match the existing missing-comparator policy: a later failed
                # fetch must not erase a formerly verified comparator.
                offer.issues = [reason for reason in offer.issues if reason != 'latest_fetch_failed']
                if offer.errors(checked) or amount(offer, False) is None:
                    continue
                key = (offer.store, offer.key)
                previous = self.latest.get(key)
                if previous is None or timestamp(previous['offer'].observed_at) <= checked:
                    self.latest[key] = {'offer': offer, 'source': self.reference(source, identity)}
        self.stats['unique_rows'] = len(seen)
        self.stats['identities'] = len(self.by_identity)
        self.stats['known_comparators'] = len(self.latest)

    @staticmethod
    def _offer(row):
        try:
            offer = Offer.from_dict(row['offer'])
            if not isinstance(offer.issues, list) or not isinstance(offer.evidence, list) or not isinstance(offer.coupon, dict):
                raise ValueError('Malformed evidence containers')
            if any(value is not None and not isinstance(value, str) for value in
                   (offer.store, offer.product_id, offer.url, offer.seller_id, offer.channel, offer.jan,
                    offer.brand, offer.model, offer.condition, offer.variant, offer.warranty)):
                raise ValueError('Malformed identity fields')
            offer.errors(timestamp(offer.observed_at) or timestamp('2000-01-01T00:00:00+00:00'))
            return offer
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise ValueError('Malformed historical offer') from error

    def _body(self, name):
        source = self.sources[name]
        body = Path(source['input_path']).read_bytes()
        if len(body) != source['bytes'] or hashlib.sha256(body).hexdigest() != source['sha256']:
            raise ValueError('Historical source changed: ' + name)
        return body

    def _read(self, name):
        rows = json.loads(self._body(name))
        if not isinstance(rows, list):
            raise ValueError('Historical source must be an observation list: ' + name)
        return rows

    def _cached_rows(self, name):
        body = self._body(name)
        if name not in self._cache:
            if len(self._cache) == 2:
                self._cache.pop(next(iter(self._cache)))
            self._cache[name] = json.loads(body)
        return self._cache[name]

    def reference(self, source, observation_id):
        name, index = source
        return {'source_file': name, 'source_sha256': self.sources[name]['sha256'],
                'row_index': index, 'observation_id': observation_id}

    def catalog(self):
        return {'history:' + offer.key: {'identity': offer.identity, **profile(offer),
                                      'profile': profile(offer), 'history_source': row['source']}
                for row in self.latest.values() for offer in [row['offer']]}

    def select(self, candidate, now, run_id, *, points=False):
        rows, references = [], []
        counts = Counter()
        loaded_name, loaded_rows = None, None
        for source in self.by_identity.get(candidate.identity, []):
            if source[0] != loaded_name:
                loaded_name, loaded_rows = source[0], self._cached_rows(source[0])
            row = loaded_rows[source[1]]
            offer = self._offer(row)
            checked = timestamp(offer.observed_at)
            reasons = []
            if row.get('run_id') == run_id or offer.observed_run_id == run_id:
                reasons.append('current_run_not_history')
            if checked is None:
                reasons.append('observation_time_unknown')
            elif checked >= now:
                reasons.append('not_before_decision_time')
            if not same_product(candidate, offer):
                reasons.append('product_configuration_mismatch')
            if checked is not None:
                reasons += offer.errors(checked)
            if amount(offer, False) is None:
                reasons.append('historical_payment_unknown')
            if points and amount(offer, True) is None:
                reasons.append('historical_points_unknown')
            reasons = sorted(set(reasons))
            reference = {**self.reference(source, row['observation_id']), 'url': offer.url,
                         'observed_at': offer.observed_at, 'run_id': row.get('run_id'),
                         'status': 'excluded' if reasons else 'eligible_for_B', 'reasons': reasons}
            references.append(reference)
            counts.update(reasons)
            if not reasons:
                rows.append(row)
        return rows, {'scope': 'pinned_history_for_B_only', 'basis': 'points' if points else 'payment', 'decision_time': iso(now),
                      'matching_identity_rows': len(references), 'eligible_rows': len(rows),
                      'excluded_rows': len(references) - len(rows), 'exclusion_reasons': dict(counts),
                      'sources': references}

    def summary(self):
        return {'scope': 'pinned_history_not_current_comparisons', 'counts': dict(self.stats),
                'source_files': self.sources, 'source_set_hash': digest({name: {k: value[k] for k in ('sha256', 'bytes')}
                                                                       for name, value in self.sources.items()}),
                'duplicates': self.duplicates, 'raw_history_preserved': True}

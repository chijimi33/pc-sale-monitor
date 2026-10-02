"""Immutable registry import and current-evidence-only lab notification projection."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path

from sale_monitor.models import Offer, iso, same_product, timestamp

KINDS = {'qualified', 'restocked', 'price_down', 'points_improved', 'remaining_threshold',
         'expires_24h', 'expires_4h', 'ended'}
TERMINAL_ERRORS = {'stock_out_of_stock', 'coupon_unavailable', 'expired'}


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate event registry JSON key')
        value[key] = item
    return value


def load_registry(files):
    name = 'state/events/registry.json'
    ref = files.get(name)
    summary = {'source_file': name if ref else None, 'states': 0, 'events': 0,
               'state_basis': 'pinned_snapshot_seed_not_asof_reconstruction'}
    if ref is None:
        return {}, {}, summary
    body = Path(ref['input_path']).read_bytes()
    if len(body) != ref['bytes'] or hashlib.sha256(body).hexdigest() != ref['sha256']:
        raise ValueError('Event registry source changed')
    value = json.loads(body, object_pairs_hook=unique_object)
    if not isinstance(value, dict) or not all(isinstance(value.get(k), dict) for k in ('states', 'events')):
        raise ValueError('Event registry requires states and events maps')
    states, events = value['states'], value['events']
    for key, state in states.items():
        if not isinstance(key, str) or not key or not isinstance(state, dict):
            raise ValueError('Invalid event state')
        if type(state.get('generation', 0)) is not int or state.get('generation', 0) < 0:
            raise ValueError('Invalid event state generation')
        facts = state.get('facts', {})
        if not isinstance(facts, dict) or not isinstance(state.get('expiry_buckets', {}), dict):
            raise ValueError('Invalid event state facts or expiry buckets')
        if any(field in state and type(state[field]) is not bool for field in ('ever_accepted', 'ended')):
            raise ValueError('Invalid event state boolean')
        if 'accepted' in facts and type(facts['accepted']) is not bool:
            raise ValueError('Invalid event state acceptance')
        if facts.get('accepted') and (type(facts.get('payment')) is not int or facts['payment'] <= 0):
            raise ValueError('Accepted event state requires a valid payment')
        for field in ('points', 'coupon_remaining'):
            if facts.get(field) is not None and type(facts[field]) is not int:
                raise ValueError('Invalid event state ' + field)
    for key, event in events.items():
        if (not isinstance(event, dict) or not key or event.get('event_id') != key
                or not isinstance(event.get('offer'), dict) or not isinstance(event.get('decision'), dict)):
            raise ValueError('Invalid event registry identity or shape')
        try:
            offer = Offer.from_dict(event['offer'])
        except (TypeError, ValueError):
            raise ValueError('Invalid event registry offer') from None
        if event.get('offer_key') != offer.key:
            raise ValueError('Event registry offer identity mismatch')
        if event['decision'].get('offer_key', offer.key) != offer.key:
            raise ValueError('Event registry decision identity mismatch')
    summary.update(source_sha256=ref['sha256'], source_bytes=ref['bytes'], states=len(states), events=len(events))
    return deepcopy(states), deepcopy(events), summary


def project(events, observations, decisions, now, run_id, *, basis_decisions=None):
    """Do not mutate the outbox or acknowledge delivery when publishing a projection."""
    offers = {key: Offer.from_dict(value['offer']) for key, value in observations.items()}
    notices, audit = [], []
    for key, event in sorted(events.items()):
        reasons = []
        occurred = timestamp(event.get('occurred_at'))
        if occurred is None:
            reasons.append('event_time_unknown')
        elif occurred > now:
            reasons.append('event_in_future')
        elif now - occurred > timedelta(days=7):
            reasons.append('event_older_than_7_days')
        delivery = event.get('delivery_status')
        if delivery == 'delivered' or event.get('delivered_at'):
            reasons.append('already_delivered')
        elif delivery not in {'unconfirmed', 'not_sent_lab_only'}:
            reasons.append('delivery_status_unknown')
        kind = event.get('kind')
        if kind not in KINDS:
            reasons.append('event_kind_unknown')
        offer = offers.get(event['offer_key'])
        decision = decisions.get(event['offer_key'])
        pair = (basis_decisions or {}).get(event['offer_key'])
        current = None
        if offer is None or decision is None:
            reasons.append('current_candidate_evidence_missing')
        else:
            errors = set(offer.errors(now))
            accepted = (any(pair[b]['status'] == 'accepted' for b in ('payment', 'points'))
                        if pair else decision.get('status') == 'accepted')
            if not same_product(Offer.from_dict(event['offer']), offer):
                reasons.append('current_product_configuration_changed')
            if offer.observed_run_id != run_id:
                reasons.append('current_run_observation_missing')
            observed = timestamp(offer.observed_at)
            if occurred and observed and observed < occurred:
                reasons.append('observation_precedes_event')
            if kind == 'ended':
                if occurred and now - occurred > timedelta(hours=8):
                    reasons.append('ended_event_older_than_8_hours')
                explicit_end = (offer.stock == 'out_of_stock' or offer.coupon.get('remaining') == 0
                                or bool(timestamp(offer.expires_at) and timestamp(offer.expires_at) <= now))
                if not explicit_end or errors - TERMINAL_ERRORS:
                    reasons.append('current_end_not_verified')
            elif not accepted or errors:
                reasons.append('current_acceptance_missing' if pair else 'current_payment_acceptance_missing')
            if kind in {'expires_24h', 'expires_4h'}:
                expires = timestamp(offer.expires_at)
                if (not expires or expires <= now or offer.expires_at != event['offer'].get('expires_at')
                        or expires - now > timedelta(hours=24 if kind == 'expires_24h' else 4)):
                    reasons.append('current_expiry_window_not_verified')
            if not reasons:
                current = {'offer': offer.to_dict(), 'payment': deepcopy(pair['payment'] if pair else decision),
                           'points': deepcopy(pair['points']) if pair else
                           {'status': 'insufficient', 'basis': 'points', 'rule': None,
                            'reasons': ['points_basis_not_evaluated_in_lab']}}
        audit.append({'event_id': key, 'offer_key': event['offer_key'], 'actionable': not reasons,
                      'reasons': sorted(set(reasons))})
        if not reasons:
            notices.append({**deepcopy(event), 'currently_actionable': True, 'current_evidence': current,
                            'presentation_experiment_id': run_id, 'presentation_mode': 'isolated_lab'})
    return notices, {'evaluated_at': iso(now), 'retained_events': len(events), 'notification_candidates': len(notices),
                     'suppressed_events': len(events) - len(notices), 'events': audit,
                     'delivery_attempts': 0, 'delivery_acknowledgements': 0}

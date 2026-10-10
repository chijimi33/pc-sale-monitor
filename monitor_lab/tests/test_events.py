from copy import deepcopy
import hashlib
import unittest
from unittest.mock import patch
import uuid

from sale_monitor.engine import update_events
from sale_monitor.models import timestamp
from monitor_lab.pipeline import run
from monitor_lab.events import load_registry, project
from monitor_lab.inputs import import_state
from monitor_lab.migration import restore_export
from monitor_lab.operations import publish
from monitor_lab.safety import allowed_root, read
from monitor_lab.stores import BACKENDS
from monitor_lab.tests.test_history import NOW, add_history, history_inputs


def registry_for(offer, *, price=10000, status='unconfirmed'):
    old = deepcopy(offer)
    old.price_yen, old.observed_at, old.observed_run_id = price, '2026-09-30T20:00:00+00:00', 'old-run'
    decision = {'offer_key': old.key, 'status': 'accepted', 'basis': 'payment', 'rule': 'A', 'reasons': []}
    events, state = update_events(old, decision, None, timestamp(old.observed_at))
    event = events[0]
    event['delivery_status'] = status
    if status == 'delivered':
        event['delivered_at'] = '2026-09-30T20:10:00+00:00'
    return {'states': {old.key: state}, 'events': {event['event_id']: event}}


def add_registry(inputs, registry):
    return add_history(inputs, registry, 'state/events/registry.json')


def publication(output):
    pointer = read(output / 'publication/current.json')
    return read(output / 'publication' / pointer['file'])


class EventTest(unittest.TestCase):
    def setUp(self):
        self.root = allowed_root() / 'tests' / ('events-' + uuid.uuid4().hex)
        self.inputs, self.output = self.root / 'inputs', self.root / 'output'

    def test_delivered_event_keeps_identity_and_ack_without_duplicate_qualification(self):
        offers = history_inputs(self.inputs, price_gap=True)
        registry = registry_for(offers['koubou-b550'], status='delivered')
        source = add_registry(self.inputs, registry)
        before = source.read_bytes()
        run(self.inputs, 'transport_failure', 'B', 'replay', self.output)
        records = read(self.output / 'state-export.json')['records']
        self.assertEqual(registry['events'], records['events'])
        self.assertEqual([], publication(self.output)['notifications'])
        self.assertEqual(before, source.read_bytes())

    def test_retained_unconfirmed_event_is_presented_with_current_price_and_evidence(self):
        offers = history_inputs(self.inputs, price_gap=True)
        registry = registry_for(offers['koubou-b550'], price=9000)
        add_registry(self.inputs, registry)
        run(self.inputs, 'transport_failure', 'B', 'replay', self.output)
        notices = publication(self.output)['notifications']
        self.assertEqual(set(registry['events']), {e['event_id'] for e in notices})
        self.assertEqual(9000, notices[0]['offer']['price_yen'])
        self.assertEqual(10000, notices[0]['current_evidence']['offer']['price_yen'])
        self.assertEqual('accepted', notices[0]['current_evidence']['payment']['status'])
        self.assertEqual('insufficient', notices[0]['current_evidence']['points']['status'])
        self.assertEqual(registry['events'], read(self.output / 'state-export.json')['records']['events'])

    def test_comparison_hold_preserves_accepted_state_without_representing_old_event(self):
        offers = history_inputs(self.inputs, price_gap=True)
        registry = registry_for(offers['koubou-b550'])
        registry['states'][offers['koubou-b550'].key]['user_metadata'] = {'retain': True}
        add_registry(self.inputs, registry)
        run(self.inputs, 'transport_failure', 'A', 'replay', self.output)
        records = read(self.output / 'state-export.json')['records']
        self.assertEqual(registry['states'][offers['koubou-b550'].key], records['event_state'][offers['koubou-b550'].key])
        self.assertEqual(registry['events'], records['events'])
        self.assertEqual([], publication(self.output)['notifications'])
        self.assertIn('current_acceptance_missing', read(self.output / 'event-projection.json')['events'][0]['reasons'])

    def test_real_price_drop_adds_one_lab_event_and_preserves_original_delivery(self):
        offers = history_inputs(self.inputs, price_gap=True)
        registry = registry_for(offers['koubou-b550'], price=11000, status='delivered')
        registry['states'][offers['koubou-b550'].key]['custom_metadata'] = 'keep'
        add_registry(self.inputs, registry)
        result = run(self.inputs, 'transport_failure', 'B', 'replay', self.output)
        records = read(self.output / 'state-export.json')['records']
        for key, event in registry['events'].items():
            self.assertEqual(event, records['events'][key])
        self.assertEqual(2, len(records['events']))
        notices = publication(self.output)['notifications']
        self.assertEqual(1, len(notices))
        self.assertEqual('price_down', notices[0]['kind'])
        self.assertEqual('not_sent_lab_only', notices[0]['delivery_status'])
        self.assertFalse(notices[0]['published'])
        self.assertEqual('keep', records['event_state'][offers['koubou-b550'].key]['custom_metadata'])
        self.assertEqual(0, result['event_projection']['delivery_attempts'])

    def test_existing_fixed_id_ack_is_not_overwritten_even_with_empty_seed_state(self):
        offers = history_inputs(self.inputs, price_gap=True)
        registry = registry_for(offers['koubou-b550'], status='delivered')
        registry['states'][offers['koubou-b550'].key] = {}
        add_registry(self.inputs, registry)
        run(self.inputs, 'transport_failure', 'B', 'replay', self.output)
        self.assertEqual(registry['events'], read(self.output / 'state-export.json')['records']['events'])
        self.assertEqual([], publication(self.output)['notifications'])

    def test_registry_resume_and_json_restore_preserve_every_original_event_on_all_backends(self):
        offers = history_inputs(self.inputs, price_gap=True)
        registry = registry_for(offers['koubou-b550'], price=9000)
        add_registry(self.inputs, registry)
        for backend, cls in BACKENDS.items():
            output = self.root / backend
            run(self.inputs, 'transport_failure', 'B', 'replay', output, backend=backend)
            before = read(output / 'state-export.json')
            shown = publication(output)
            resumed = run(self.inputs, 'transport_failure', 'B', 'replay', output, backend=backend)
            self.assertEqual(0, resumed['replayed_pages'])
            self.assertEqual(before, read(output / 'state-export.json'))
            self.assertEqual(shown, publication(output))
            self.assertEqual(registry['events'], before['records']['events'])
            restored_path = self.root / ('restore-' + backend)
            restore_export(output / 'state-export.json', backend, restored_path)
            with cls(restored_path) as restored:
                self.assertEqual(before, restored.snapshot())

    def test_publication_failure_then_resume_keeps_ack_and_current_evidence(self):
        offers = history_inputs(self.inputs, price_gap=True)
        registry = registry_for(offers['koubou-b550'], price=11000, status='delivered')
        add_registry(self.inputs, registry)
        def fail(stage):
            if stage == 'after_pointer':
                raise RuntimeError('simulated publication interruption')
        def interrupted(*args):
            return publish(*args, hook=fail)
        with patch('monitor_lab.pipeline.publish', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'simulated publication'):
                run(self.inputs, 'transport_failure', 'B', 'replay', self.output)
        visible = publication(self.output)
        resumed = run(self.inputs, 'transport_failure', 'B', 'replay', self.output)
        self.assertEqual(visible, publication(self.output))
        self.assertEqual(0, resumed['replayed_pages'])
        records = read(self.output / 'state-export.json')['records']
        self.assertEqual(2, len(records['events']))
        for key, event in registry['events'].items():
            self.assertEqual(event, records['events'][key])

    def test_projection_explains_stale_future_missing_delivered_and_changed_configuration(self):
        offers = history_inputs(self.inputs, price_gap=True)
        offer = offers['koubou-b550']
        registry = registry_for(offer)
        event = next(iter(registry['events'].values()))
        decision = {'status': 'accepted', 'basis': 'payment'}
        cases = [
            ('stale-event', {'occurred_at': '2026-09-01T00:00:00Z'}, None, 'event_older_than_7_days'),
            ('future-event', {'occurred_at': '2026-10-02T00:00:00Z'}, None, 'event_in_future'),
            ('unknown-time', {'occurred_at': 'bad'}, None, 'event_time_unknown'),
            ('unknown-delivery', {'delivery_status': 'unknown'}, None, 'delivery_status_unknown'),
            ('ack-only', {'delivered_at': NOW}, None, 'already_delivered'),
            ('new-kind', {'kind': 'unknown'}, None, 'event_kind_unknown'),
            ('stale-current', {}, {'observed_at': '2026-09-29T00:00:00Z'}, 'current_payment_acceptance_missing'),
            ('future-current', {}, {'observed_at': '2026-10-02T00:00:00Z'}, 'current_payment_acceptance_missing'),
            ('wrong-run', {}, {'observed_run_id': 'prior-run'}, 'current_run_observation_missing'),
            ('changed-bundle', {}, {'variant': '2 pack'}, 'current_product_configuration_changed'),
            ('older-evidence', {'occurred_at': NOW}, {'observed_at': '2026-10-01T19:59:00Z'}, 'observation_precedes_event'),
        ]
        for name, event_changes, offer_changes, reason in cases:
            with self.subTest(name=name):
                current = {**offer.to_dict(), **(offer_changes or {})}
                changed = {**event, **event_changes}
                notices, audit = project({event['event_id']: changed}, {offer.key: {'offer': current}},
                                         {offer.key: decision}, timestamp(NOW), 'lab-fixture')
                self.assertEqual([], notices)
                self.assertIn(reason, audit['events'][0]['reasons'])
        notices, audit = project(registry['events'], {}, {}, timestamp(NOW), 'lab-fixture')
        self.assertEqual([], notices)
        self.assertIn('current_candidate_evidence_missing', audit['events'][0]['reasons'])

    def test_ended_requires_fresh_current_end_and_eight_hour_window(self):
        offers = history_inputs(self.inputs)
        offer = offers['koubou-b550']
        registry = registry_for(offer)
        event = next(iter(registry['events'].values()))
        event.update(kind='ended', occurred_at='2026-10-01T19:00:00Z')
        for stock, observed, occurred, expected in [
            ('out_of_stock', NOW, event['occurred_at'], True),
            ('in_stock', NOW, event['occurred_at'], False),
            ('out_of_stock', '2026-09-30T00:00:00Z', event['occurred_at'], False),
            ('out_of_stock', NOW, '2026-10-01T11:59:59Z', False),
        ]:
            current = {**offer.to_dict(), 'stock': stock, 'observed_at': observed}
            changed = {**event, 'occurred_at': occurred}
            notices, _ = project({event['event_id']: changed}, {offer.key: {'offer': current}},
                                 {offer.key: {'status': 'insufficient', 'basis': 'payment'}}, timestamp(NOW), 'lab-fixture')
            self.assertEqual(expected, bool(notices))
            if expected:
                self.assertEqual('out_of_stock', notices[0]['current_evidence']['offer']['stock'])

    def test_expiry_event_requires_same_deadline_and_current_window(self):
        offers = history_inputs(self.inputs)
        offer = offers['koubou-b550']
        event = next(iter(registry_for(offer)['events'].values()))
        event['kind'] = 'expires_4h'
        event['offer']['expires_at'] = '2026-10-01T23:00:00Z'
        for expires, expected in [('2026-10-01T23:00:00Z', True), ('2026-10-02T22:00:00Z', False), (None, False)]:
            current = {**offer.to_dict(), 'expires_at': expires}
            notices, _ = project({event['event_id']: event}, {offer.key: {'offer': current}},
                                 {offer.key: {'status': 'accepted'}}, timestamp(NOW), 'lab-fixture')
            self.assertEqual(expected, bool(notices))

    def test_invalid_registry_fails_before_output_and_network(self):
        offers = history_inputs(self.inputs)
        original = registry_for(offers['koubou-b550'])
        bad_offer = deepcopy(original)
        next(iter(bad_offer['events'].values()))['offer_key'] = 'wrong'
        bad_generation = deepcopy(original)
        next(iter(bad_generation['states'].values()))['generation'] = -1
        for i, value in enumerate([{'states': [], 'events': {}}, bad_offer, bad_generation]):
            add_registry(self.inputs, value)
            output = self.root / ('refused-' + str(i))
            with self.assertRaises(ValueError):
                run(self.inputs, 'transport_failure', 'B', 'replay', output)
            self.assertFalse(output.exists())

    def test_registry_hash_and_duplicate_json_keys_are_not_silently_accepted(self):
        offers = history_inputs(self.inputs)
        source = add_registry(self.inputs, registry_for(offers['koubou-b550']))
        files, _, _ = import_state(self.inputs, 'transport_failure')
        source.write_bytes(b'{"states":{},"events":{},"events":{}}')
        with self.assertRaisesRegex(ValueError, 'source changed'):
            load_registry(files)
        ref = files['state/events/registry.json']
        ref.update(bytes=source.stat().st_size, sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        with self.assertRaisesRegex(ValueError, 'Duplicate event registry JSON key'):
            load_registry(files)

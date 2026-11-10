"""Timestamp boundaries and durable scheduling, using only synthetic transport."""
from copy import deepcopy
from itertools import permutations
import unittest
import uuid

from monitor_lab import queueing
from monitor_lab.safety import allowed_root
from monitor_lab.scheduler import collect
from monitor_lab.stores import BACKENDS, SQLite
from monitor_lab.tests.test_scheduler import (
    ARK, NOW, TSUKUMO, Clock, PlannedStop, client_for, on_page, task,
)


class QueueAgeEdgesTest(unittest.TestCase):
    def test_equivalent_offsets_fractions_and_japan_local_dates(self):
        equivalent = (
            ('2026-10-01T15:00:00Z', '2026-10-01T15:00:00+00:00',
             '2026-10-02T00:00:00+09:00', '2026-10-01T11:00:00-04:00',
             '2026-10-01T11:30:00-03:30', '2026-10-02', '2026-10-02T00:00:00'),
            ('2026-10-01T15:00:00.1Z', '2026-10-01T15:00:00.100000+00:00',
             '2026-10-02T00:00:00.100000+09:00'),
        )
        for values in equivalent:
            for raw in values:
                with self.subTest(raw=raw):
                    self.assertEqual(queueing.age_key(values[0]), queueing.age_key(raw))
                    self.assertEqual(queueing.age_key(raw), queueing.task_age_key({'created_at': raw}))
            self.assertEqual(min(values), queueing.earliest_age(iter(values)))
            self.assertEqual(min(values), queueing.earliest_age(reversed(values)))

        precise = ('2026-10-02T00:00:00.000001+09:00',
                   '2026-10-01T11:00:00.000002-04:00',
                   '2026-10-01T15:00:00.1Z', '2026-10-01T15:00:00.9Z')
        for older, newer in zip(precise, precise[1:]):
            with self.subTest(older=older, newer=newer):
                self.assertLess(queueing.age_key(older), queueing.age_key(newer))
                self.assertEqual(older, queueing.earliest_age([newer, older]))
                value = {'created_at': newer, 'lab_origin_created_at': older, 'attempts': 7}
                before = deepcopy(value)
                self.assertEqual(older, queueing.task_age(value))
                self.assertEqual(queueing.age_key(older), queueing.task_age_key(value))
                self.assertEqual(before, value)

    def test_equal_instants_use_resource_key_not_spelling_or_insertion_order(self):
        spellings = ('2026-10-01T15:00:00Z', '2026-10-01T15:00:00+00:00',
                     '2026-10-02T00:00:00+09:00')
        for ages in permutations(spellings):
            tasks = {str(i): task(ARK + str(100 + i) + '/', age=age)
                     for i, age in enumerate(ages)}
            original = deepcopy(tasks)
            expected = min(queueing.resource_key(value) for value in tasks.values())
            for policy in ('cyclic', 'dependent'):
                for reverse in (False, True):
                    with self.subTest(ages=ages, policy=policy, reverse=reverse):
                        ordered = dict(reversed(list(tasks.items()))) if reverse else tasks
                        selected = queueing.select_resource(ordered, {}, NOW, policy=policy)
                        self.assertEqual(expected, selected['resource'])
                        self.assertEqual(original, tasks)

    def test_missing_age_keeps_empty_sentinel_and_original_fields(self):
        dated = '2026-10-02'
        missing = ({}, {'created_at': None}, {'created_at': ''},
                   {'created_at': None, 'lab_origin_created_at': ''})
        self.assertEqual('', queueing.earliest_age([]))
        self.assertEqual('', queueing.earliest_age([None, '']))
        for fields in missing:
            with self.subTest(fields=fields):
                value = task(ARK + '101/')
                value.pop('created_at')
                value.update(fields)
                tasks = {'dated': task(ARK + '102/', age=dated), 'missing': value}
                before = deepcopy(tasks)
                self.assertEqual('', queueing.task_age(value))
                self.assertEqual(queueing.age_key(''), queueing.task_age_key(value))
                self.assertEqual(queueing.age_key(None), queueing.task_age_key(value))
                for policy in ('cyclic', 'dependent'):
                    self.assertEqual(['missing'], queueing.select_resource(
                        tasks, {}, NOW, policy=policy)['task_ids'])
                self.assertEqual(before, tasks)
        for fields in ({'created_at': None, 'lab_origin_created_at': dated},
                       {'created_at': dated, 'lab_origin_created_at': ''}):
            with self.subTest(fields=fields):
                before = deepcopy(fields)
                self.assertEqual(dated, queueing.task_age(fields))
                self.assertEqual(queueing.age_key(dated), queueing.task_age_key(fields))
                self.assertEqual(before, fields)

    def test_present_invalid_dates_raise_even_with_a_valid_other_age(self):
        valid = '2026-10-02T00:00:00Z'
        for raw in ('not-a-date', '2026-02-30', ' ', '2026-10-02T00:00:00+25:00', 0, False, 42):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    queueing.age_key(raw)
                with self.assertRaises(ValueError):
                    queueing.earliest_age([valid, raw])
            for field in ('created_at', 'lab_origin_created_at'):
                with self.subTest(raw=raw, field=field):
                    value = {'created_at': valid, 'lab_origin_created_at': valid,
                             'attempts': 7, field: raw}
                    before = deepcopy(value)
                    with self.assertRaises(ValueError):
                        queueing.task_age(value)
                    with self.assertRaises(ValueError):
                        queueing.task_age_key(value)
                    self.assertEqual(before, value)


class QueueAgeResumeEdgesTest(unittest.TestCase):
    def setUp(self):
        self.root = allowed_root() / 'tests' / ('queue-age-edges-' + uuid.uuid4().hex)

    def test_invalid_unselected_resource_stops_before_any_acquisition(self):
        # A valid requested task must not hide a bad age in the ordinary lane.
        for field in ('created_at', 'lab_origin_created_at'):
            with self.subTest(field=field):
                clock = Clock(NOW)
                tasks = {'good': task(ARK + '201/', requested=True, age='2020-01-01'),
                         'bad': task(TSUKUMO + '202/', age='2021-01-01')}
                tasks['bad'][field] = 'not-a-date'
                before = deepcopy(tasks)
                urls = {value['url'] for value in tasks.values()}
                client = client_for(clock, {url: [(200, {}, b'fixture')] for url in urls})
                with SQLite(self.root / field) as store:
                    store.commit('import', [('tasks', key, value) for key, value in tasks.items()])
                    with self.assertRaises(ValueError):
                        collect(store, client, 'lab-invalid-age', urls, on_page,
                                architecture='B', cycles=1, max_tasks=2, budget=120)
                    state = store.snapshot()['records']
                self.assertEqual([], client.transport.calls)
                self.assertEqual(0, client.requests)
                self.assertEqual(before, state['tasks'])
                self.assertFalse(state.get('dispatches'))
                self.assertFalse(state.get('observations'))

    def test_resume_preserves_age_fairness_host_wait_and_total_cap_on_all_backends(self):
        for backend, cls in BACKENDS.items():
            with self.subTest(backend=backend):
                clock = Clock(NOW)
                # These requested ages differ by one microsecond across offsets.
                ages = ('2026-10-02T01:00:00.000001+09:00',
                        '2026-10-01T16:00:00.000002Z',
                        '2026-10-01T12:00:00.000003-04:00',
                        '2026-10-02T01:00:00.000004',
                        '2026-10-01T16:00:00.000005+00:00',
                        '2026-10-01T16:00:00.000006Z')
                tasks = {'r' + str(i): task((ARK if i < 2 else TSUKUMO) + str(300 + i) + '/',
                                           requested=True, age=age)
                         for i, age in enumerate(ages)}
                tasks['r0']['lab_origin_created_at'] = tasks['r0']['created_at']
                tasks['r0']['created_at'] = '2026-10-02T08:00:00+09:00'
                tasks.update({
                    'old0': task(ARK + '400/', age='2026-10-02T00:30:00+09:00'),
                    'old1': task(TSUKUMO + '401/', age='2026-10-01T12:00:00-04:00'),
                    'old2': task(TSUKUMO + '402/', age='2026-10-01T16:10:00Z'),
                })
                original = deepcopy(tasks)
                urls = {value['url'] for value in tasks.values()}
                responses = {url: [(200, {}, b'fixture')] for url in urls}
                responses[tasks['r2']['url']] = [(429, {'Retry-After': '10'}, b'wait')]
                checkpoints = 0

                def stop(stage):
                    nonlocal checkpoints
                    if stage == 'after_checkpoint':
                        checkpoints += 1
                        if checkpoints == 3:
                            raise PlannedStop()

                options = dict(architecture='B', cycles=1, max_tasks=8, budget=120)
                first = client_for(clock, responses)
                with cls(self.root / backend) as store:
                    store.commit('import', [('tasks', key, value) for key, value in tasks.items()])
                    with self.assertRaises(PlannedStop):
                        collect(store, first, 'lab-age-resume', urls, on_page, hook=stop, **options)
                    checkpoint = store.snapshot()['records']
                self.assertEqual([tasks[key]['url'] for key in ('r0', 'r1', 'r2')],
                                 [row['url'] for row in first.transport.calls])
                self.assertEqual(3, checkpoint['scheduler']['collection']['cursor'])
                until = checkpoint['hosts']['shop.tsukumo.co.jp']['until']
                self.assertEqual(NOW + 13, until)

                second = client_for(clock, {url: [(200, {}, b'fixture')] for url in urls})
                with cls(self.root / backend) as store:
                    result = collect(store, second, 'lab-age-resume', urls, on_page, **options)
                    final = store.snapshot()
                state = final['records']
                calls = first.transport.calls + second.transport.calls
                expected = ('r0', 'r1', 'r2', 'old0', 'r2', 'r3', 'r4', 'old1')
                self.assertEqual([tasks[key]['url'] for key in expected], [row['url'] for row in calls])
                self.assertEqual(NOW + 3, calls[3]['started_at'])  # Other host works during the gate.
                self.assertEqual(until, calls[4]['started_at'])
                for row in second.transport.calls:
                    if row['url'].startswith(TSUKUMO):
                        self.assertGreaterEqual(row['started_at'], until)
                waits = result['scheduling']['waits']
                self.assertEqual(1, len(waits))
                self.assertEqual('completed', waits[0]['state'])
                self.assertEqual(until, waits[0]['finished_at_epoch'])
                self.assertEqual('resource_limit', result['scheduling']['stop_reason'])
                control = state['scheduler']['collection']
                for field in ('cursor', 'reserved_resources', 'confirmed_http_requests'):
                    self.assertEqual(8, control[field])
                self.assertEqual(7, len(state['observations']))
                for key, value in original.items():
                    for field in ('created_at', 'attempts', 'lab_origin_created_at'):
                        self.assertEqual(value.get(field), state['tasks'][key].get(field), (key, field))
                    count = 2 if key == 'r2' else int(key in expected)
                    self.assertEqual(count, len(state['tasks'][key]['lab_attempts']), key)
                    self.assertEqual('complete' if count else 'pending', state['tasks'][key]['lab_status'], key)
                self.assertEqual(original, tasks)

                third = client_for(clock, {})
                with cls(self.root / backend) as store:
                    result = collect(store, third, 'lab-age-resume', urls, on_page, **options)
                    self.assertEqual(final, store.snapshot())
                self.assertEqual('resource_limit', result['scheduling']['stop_reason'])
                self.assertEqual([], third.transport.calls)
                self.assertEqual(0, third.requests)

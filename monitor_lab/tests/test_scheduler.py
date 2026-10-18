from copy import deepcopy
import http.client
import os
import subprocess
import sys
import unittest
import uuid

from monitor_lab.acquire import Coordinator
from monitor_lab.pipeline import make_task
from monitor_lab.scheduler import collect
from monitor_lab.safety import allowed_root
from monitor_lab.stores import BACKENDS, SQLite
from monitor_lab.tests.test_pipeline_scheduling import Clock

NOW = 1791000000.0
ARK = 'https://www.ark-pc.co.jp/i/'
TSUKUMO = 'https://shop.tsukumo.co.jp/goods/'


class PlannedStop(BaseException):
    pass


class ScriptedTransport:
    method = 'synthetic'

    def __init__(self, clock, responses):
        self.clock, self.responses, self.calls = clock, deepcopy(responses), []

    def get(self, url, timeout):
        self.calls.append({'url': url, 'started_at': self.clock.time(), 'timeout': timeout})
        self.clock.sleep(min(1, timeout))
        response = self.responses[url].pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        pass


def client_for(clock, responses):
    transport = ScriptedTransport(clock, responses)
    return Coordinator(transport, delay=0, clock=clock.time, monotonic=clock.time, sleep=clock.sleep)


def on_page(task, page, receipt, tasks, cycle, records):
    return [('observations', task['url'], {'url': page.url, 'body': page.body.decode(), 'fixture': True})]


def task(url, *, requested=False, age='2020-01-01T00:00:00+00:00'):
    value = make_task('ark' if 'ark-pc' in url else 'tsukumo', url, age, original={'attempts': 7})
    value['requested'] = requested
    return value


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.root = allowed_root() / 'tests' / ('scheduler-' + uuid.uuid4().hex)

    def test_other_host_runs_during_shared_wait_then_both_waiters_recover(self):
        clock = Clock(NOW)
        one, two, old = TSUKUMO + '1/', TSUKUMO + '2/', ARK + '3/'
        client = client_for(clock, {one: [http.client.RemoteDisconnected('fixture'), (200, {}, b'one')],
                                    two: [(200, {}, b'two')], old: [(200, {}, b'old')]})
        tasks = {'one': task(one, requested=True), 'two': task(two, requested=True, age='2021-01-01'),
                 'old': task(old, age='2000-01-01')}
        with SQLite(self.root / 'store') as store:
            store.commit('import', [('tasks', k, v) for k, v in tasks.items()])
            result = collect(store, client, 'lab-waits', {one, two, old}, on_page,
                             architecture='B', cycles=1, max_tasks=20, budget=120)
            state = store.snapshot()['records']
        self.assertEqual([one, old, one, two], [row['url'] for row in client.transport.calls])
        self.assertEqual(NOW + 61, client.transport.calls[2]['started_at'])
        self.assertEqual(2, result['scheduling']['waits'][0]['waiting_tasks'])
        self.assertEqual('completed', result['scheduling']['waits'][0]['state'])
        self.assertEqual(4, state['scheduler']['collection']['confirmed_http_requests'])
        self.assertEqual(2, len(state['tasks']['one']['lab_attempts']))
        self.assertEqual(1, len(state['tasks']['two']['lab_attempts']))
        for key, original in tasks.items():
            self.assertEqual('complete', state['tasks'][key]['lab_status'])
            self.assertEqual(original['created_at'], state['tasks'][key]['created_at'])
            self.assertEqual(7, state['tasks'][key]['attempts'])

    def test_fairness_cursor_survives_restart_on_each_backend(self):
        for backend, cls in BACKENDS.items():
            clock = Clock(NOW)
            tasks = {'old': task(ARK + '1/', age='2000-01-01')}
            tasks.update({str(i): task(ARK + str(i + 10) + '/', requested=True) for i in range(6)})
            urls = {t['url'] for t in tasks.values()}
            calls = []
            count = 0

            def stop(stage):
                nonlocal count
                if stage == 'after_checkpoint':
                    count += 1
                    if count == 3:
                        raise PlannedStop()

            with cls(self.root / backend) as store:
                store.commit('import', [('tasks', k, v) for k, v in tasks.items()])
                first = client_for(clock, {url: [(200, {}, b'fixture')] for url in urls})
                with self.assertRaises(PlannedStop):
                    collect(store, first, 'lab-fairness', urls, on_page, architecture='B', cycles=1,
                            max_tasks=20, budget=120, hook=stop)
                calls.extend(first.transport.calls)
            with cls(self.root / backend) as store:
                second = client_for(clock, {url: [(200, {}, b'fixture')] for url in urls})
                collect(store, second, 'lab-fairness', urls, on_page,
                        architecture='B', cycles=1, max_tasks=20, budget=120)
                calls.extend(second.transport.calls)
                state = store.snapshot()['records']
            self.assertEqual(tasks['old']['url'], calls[3]['url'], backend)
            self.assertEqual(7, len(calls), backend)
            self.assertEqual(7, state['scheduler']['collection']['cursor'])
            self.assertEqual(7, len(state['observations']))

    def test_resource_limit_is_not_reset_by_reopening_store(self):
        clock = Clock(NOW)
        urls = {ARK + str(i) + '/' for i in range(3)}
        with SQLite(self.root / 'store') as store:
            store.commit('import', [('tasks', url, task(url)) for url in urls])
            first = client_for(clock, {url: [(200, {}, b'fixture')] for url in urls})
            collect(store, first, 'lab-cap', urls, on_page, architecture='B', cycles=1, max_tasks=2, budget=120)
            before = store.snapshot()
        with SQLite(self.root / 'store') as store:
            second = client_for(clock, {url: [(200, {}, b'fixture')] for url in urls})
            result = collect(store, second, 'lab-cap', urls, on_page, architecture='B', cycles=1, max_tasks=2, budget=120)
            self.assertEqual(before, store.snapshot())
        self.assertEqual(2, first.requests)
        self.assertEqual(0, second.requests)
        self.assertEqual('resource_limit', result['scheduling']['stop_reason'])

    def test_long_retry_after_and_denial_do_not_spend_waiter_attempts(self):
        for status, headers, reason in [(429, {'Retry-After': '3600'}, 'wait_exceeds_remaining_budget'),
                                        (403, {}, 'blocked_hosts')]:
            clock = Clock(NOW)
            urls = [TSUKUMO + '1/', TSUKUMO + '2/']
            client = client_for(clock, {url: [(status, headers, b'fixture')] for url in urls})
            with SQLite(self.root / str(status)) as store:
                store.commit('import', [('tasks', url, task(url)) for url in urls])
                result = collect(store, client, 'lab-hold', set(urls), on_page,
                                 architecture='B', cycles=1, max_tasks=20, budget=120)
                tasks = store.snapshot()['records']['tasks']
            self.assertEqual(1, client.requests)
            self.assertEqual(reason, result['scheduling']['stop_reason'])
            self.assertEqual(2, result['scheduling']['waiting_task_count'])
            self.assertEqual(1, sum(len(t['lab_attempts']) for t in tasks.values()))

    def test_zero_retry_after_allows_bounded_retry(self):
        clock = Clock(NOW)
        url = TSUKUMO + '1/'
        client = client_for(clock, {url: [(429, {'Retry-After': '0'}, b'wait'), (200, {}, b'fixture')]})
        with SQLite(self.root / 'store') as store:
            store.commit('import', [('tasks', 'one', task(url))])
            collect(store, client, 'lab-retry-zero', {url}, on_page, architecture='B', cycles=1, max_tasks=2, budget=10)
            self.assertEqual('complete', store.snapshot()['records']['tasks']['one']['lab_status'])
        self.assertEqual(2, client.requests)

    def test_held_host_does_not_strand_next_cycle_work_or_reset_its_gate_on_resume(self):
        for status, headers, reason in ((403, {}, 'blocked_hosts'),
                                        (429, {'Retry-After': '3600'}, 'wait_exceeds_remaining_budget')):
            clock = Clock(NOW)
            denied, waiter, deferred = TSUKUMO + '1/', TSUKUMO + '2/', ARK + '3/'
            tasks = {'denied': task(denied), 'waiter': task(waiter, age='2021-01-01'),
                     'deferred': {**task(deferred), 'lab_ready_cycle': 1}}
            client = client_for(clock, {denied: [(status, headers, b'hold')], deferred: [(200, {}, b'next cycle')]})
            path = self.root / ('next-cycle-' + str(status))
            with SQLite(path) as store:
                store.commit('import', [('tasks', key, value) for key, value in tasks.items()])
                result = collect(store, client, 'lab-next-cycle', {denied, waiter, deferred}, on_page,
                                 architecture='A', cycles=2, max_tasks=20, budget=120)
                before = store.snapshot()
            self.assertEqual([denied, deferred], [row['url'] for row in client.transport.calls])
            records = before['records']
            self.assertEqual('complete', records['tasks']['deferred']['lab_status'])
            self.assertNotEqual('complete', records['tasks']['waiter']['lab_status'])
            self.assertEqual([], records['tasks']['waiter']['lab_attempts'])
            self.assertEqual(reason, result['scheduling']['stop_reason'])
            self.assertEqual(NOW + 120, records['scheduler']['collection']['deadline_epoch'])
            self.assertEqual(1, records['scheduler']['collection']['cycle'])
            self.assertEqual(2, records['scheduler']['collection']['reserved_resources'])
            gate = records['hosts']['shop.tsukumo.co.jp']
            self.assertTrue(gate.get('blocked') if status == 403 else gate['until'] > NOW + 120)
            for key in tasks:
                self.assertEqual(tasks[key]['created_at'], records['tasks'][key]['created_at'])
                self.assertEqual(7, records['tasks'][key]['attempts'])
            resumed = client_for(clock, {})
            with SQLite(path) as store:
                collect(store, resumed, 'lab-next-cycle', {denied, waiter, deferred}, on_page,
                        architecture='A', cycles=2, max_tasks=20, budget=120)
                self.assertEqual(before, store.snapshot())
            self.assertEqual([], resumed.transport.calls)

    def test_parse_failure_is_checkpointed_without_completing_task(self):
        clock = Clock(NOW)
        url = ARK + '1/'
        client = client_for(clock, {url: [(200, {}, b'fixture')]})

        def broken(*args):
            raise ValueError('synthetic normalization failure')

        with SQLite(self.root / 'store') as store:
            store.commit('import', [('tasks', 'one', task(url))])
            with self.assertRaisesRegex(ValueError, 'normalization failure'):
                collect(store, client, 'lab-parse-error', {url}, broken, architecture='B', cycles=1, max_tasks=20, budget=10)
            state = store.snapshot()['records']
        self.assertEqual('waiting', state['tasks']['one']['lab_status'])
        self.assertIsNone(state['dispatches']['lab-parse-error:1']['receipt']['error'])
        self.assertEqual('ValueError', state['dispatches']['lab-parse-error:1']['processing_error'])
        self.assertFalse(state.get('observations'))

    def test_hard_exit_preserves_cap_cursor_and_unknown_outcome(self):
        script = r'''
import os,sys
from monitor_lab.tests.test_scheduler import Clock,client_for,task,on_page,ARK,NOW
from monitor_lab.scheduler import collect
from monitor_lab.stores import SQLite
url=ARK+'1/'
clock=Clock(NOW)
def stop(stage):
    if stage==sys.argv[2]:os._exit(73)
with SQLite(sys.argv[1]) as store:
    store.commit('import',[('tasks','one',task(url))])
    collect(store,client_for(clock,{url:[(200,{},b'fixture')]}),'lab-crash',{url},on_page,
            architecture='B',cycles=1,max_tasks=20,budget=10,hook=stop)
'''
        for stage in ('after_reservation', 'after_fetch', 'after_checkpoint'):
            root = self.root / stage
            child = subprocess.run([sys.executable, '-B', '-c', script, str(root), stage], capture_output=True, text=True,
                                   env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
            self.assertEqual(73, child.returncode, child.stderr)
            clock = Clock(NOW + 20)
            client = client_for(clock, {})
            with SQLite(root) as store:
                collect(store, client, 'lab-crash', {ARK + '1/'}, on_page, architecture='B', cycles=1, max_tasks=20, budget=10)
                state = store.snapshot()['records']
            self.assertEqual(0, client.requests)
            self.assertEqual(1, state['scheduler']['collection']['reserved_resources'])
            self.assertEqual(1, state['scheduler']['collection']['cursor'])
            committed = stage == 'after_checkpoint'
            self.assertEqual(int(committed), state['scheduler']['collection']['confirmed_http_requests'])
            self.assertEqual(int(committed), len(state.get('observations', {})))
            self.assertEqual('committed' if committed else 'interrupted_unknown', state['dispatches']['lab-crash:1']['state'])
            self.assertEqual(7, state['tasks']['one']['attempts'])

from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from sale_monitor.http import FetchError
from sale_monitor.models import UTC, iso
from sale_monitor.runner import Collector, task_order
from sale_monitor.scheduling import select_task

NOW = datetime(2026, 11, 1, tzinfo=UTC)


def task(key, *, days=5, priority=3, last_attempt=None, **extra):
    value = dict(type='product', kind='sale', url='https://www.ark-pc.co.jp/i/'+key+'/',
                 created_at=iso(NOW-timedelta(days=days)), priority=priority, attempts=0)
    if last_attempt:
        value.update(last_attempt_at=last_attempt, last_error='http_404', attempts=80)
    value.update(extra)
    return key, value


class CrossRunCoverage(unittest.TestCase):
    def test_short_runs_retry_old_failure_and_reach_unattempted_work(self):
        old = task('old', days=30, priority=0, last_attempt=iso(NOW-timedelta(hours=1)))
        unseen = task('unseen', days=5)
        pending = [old, unseen]
        original = deepcopy(pending)
        scheduler = {}; chosen = []
        for n in range(8):
            # Each run ends after one opportunity; serialize the cursor as a
            # real checkpoint does, while the run-local attempted set resets.
            scheduler = json.loads(json.dumps(scheduler))
            key, row = select_task(pending, scheduler, task_order, NOW+timedelta(hours=4*n), set())
            chosen.append(key)
            row['last_attempt_at'] = iso(NOW+timedelta(hours=4*n))
        self.assertEqual(chosen.count('unseen'), 2)
        self.assertEqual(chosen.count('old'), 6)
        self.assertEqual([t['created_at'] for _,t in pending], [t['created_at'] for _,t in original])
        self.assertEqual([t['attempts'] for _,t in pending], [t['attempts'] for _,t in original])
        self.assertEqual(len(pending), 2)

    def test_coverage_slot_prefers_least_recent_attempt_over_priority(self):
        recent = task('recent', days=30, priority=0, last_attempt=iso(NOW-timedelta(hours=1)))
        waiting = task('waiting', days=5, priority=3, last_attempt=iso(NOW-timedelta(days=3)))
        selected = select_task([recent, waiting], {'coverage_cursor_by_group': {'sale': 3}}, task_order, NOW)
        self.assertEqual(selected[0], 'waiting')

    def test_coverage_slot_keeps_overdue_work_ahead_of_fresh_unattempted(self):
        old = task('old', last_attempt=iso(NOW-timedelta(hours=1)))
        new = task('new', days=0, priority=0)
        self.assertEqual(select_task([new, old], {'coverage_cursor_by_group': {'sale': 3}}, task_order, NOW)[0], 'old')

    def test_same_run_retry_does_not_consume_cross_run_coverage_slot(self):
        pending = [task('retry', last_attempt=iso(NOW))]
        scheduler = {'coverage_cursor_by_group': {'sale': 3}}
        selected = select_task(pending, scheduler, task_order, NOW, {'retry'})
        self.assertEqual(selected[0], 'retry')
        self.assertEqual(scheduler['coverage_cursor_by_group']['sale'], 3)
        self.assertEqual(scheduler['selected_by_attempt_kind'], {'retry_same_run': 1})

    def test_comparison_groups_keep_independent_persisted_cursors(self):
        old = task('old', kind='comparison', requested=True, days=30, priority=0,
                   last_attempt=iso(NOW-timedelta(hours=1)))
        unseen = task('unseen', kind='comparison', requested=True)
        other = task('other', kind='comparison', requested=False)
        scheduler = {'comparison_cursor': 3, 'coverage_cursor_by_group': {'comparison:requested': 3}}
        self.assertEqual(select_task([old, unseen, other], scheduler, task_order, NOW)[0], 'other')
        self.assertEqual(scheduler['coverage_cursor_by_group']['comparison:requested'], 3)
        self.assertEqual(select_task([old, unseen, other], scheduler, task_order, NOW)[0], 'unseen')

    def test_selection_does_not_mutate_task_history_or_choose_attempted_as_untried(self):
        pending = [task('old', days=30, priority=0), task('unseen')]
        original = deepcopy(pending)
        scheduler = {'coverage_cursor_by_group': {'sale': 3}}
        self.assertEqual(select_task(pending, scheduler, task_order, NOW, {'old'})[0], 'unseen')
        self.assertEqual(pending, original)

    def test_collector_restart_persists_cursor_and_original_age(self):
        cfg = {'stores': {'ark': {'adapter': 'html', 'seed_urls': []}}}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); initial = Collector(root, 'ark', cfg, 'initial')
            old = task('11111111', days=30, priority=0, last_attempt=iso(NOW-timedelta(hours=1)))[1]
            unseen = task('22222222')[1]
            old_key = initial.enqueue(old); unseen_key = initial.enqueue(unseen)
            initial.state['offers'] = {'historical': {'price_yen': 1000, 'url': 'https://www.ark-pc.co.jp/i/99999999/', 'observed_run_id': 'old'}}
            original = deepcopy(initial.state['queue']); initial.save(); chosen = []
            for n in range(4):
                c = Collector(root, 'ark', cfg, str(n)); c.seed = lambda: None
                def fail(row):
                    chosen.append(row['url'])
                    raise FetchError('http_404')
                c.process = fail
                with patch('sale_monitor.runner.time.monotonic', side_effect=[0, 0, 2]), patch('sale_monitor.runner.utcnow', return_value=NOW):
                    c.collect(seconds=1)
            final = Collector(root, 'ark', cfg, 'next')
            self.assertIn(unseen['url'], chosen)
            self.assertEqual(set(final.state['queue']), set(original))
            for k in original:
                self.assertEqual(final.state['queue'][k]['created_at'], original[k]['created_at'])
            self.assertEqual(final.state['queue'][old_key]['attempts'], original[old_key]['attempts']+3)
            self.assertEqual(final.state['queue'][unseen_key]['attempts'], 1)
            self.assertEqual(final.state['offers']['historical']['observed_run_id'], 'old')
            self.assertEqual(final.state['scheduler']['coverage_cursor_by_group']['sale'], 0)
            self.assertEqual(final.state['scheduler']['selected_by_coverage_policy'], {})

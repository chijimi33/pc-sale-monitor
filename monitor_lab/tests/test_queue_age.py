from copy import deepcopy
import unittest

from sale_monitor.http import Page
from monitor_lab.discovery import inherit_descendants, merge_child
from monitor_lab.pipeline import existing_task_id, make_task
from monitor_lab.queueing import expand_shared_list, select_resource, task_age

OLDER = '2026-10-02T00:30:00+09:00'  # October 1 15:30 UTC.
NEWER = '2026-10-01T16:00:00Z'
URL = 'https://www.ark-pc.co.jp/i/12201487/'
LIST = 'https://www.ark-pc.co.jp/search/'


def task(url, created, **extra):
    return {**make_task('ark', url, created), **extra}


class QueueAgeTest(unittest.TestCase):
    def test_real_instant_not_timestamp_text_controls_resource_order(self):
        tasks = {'older': task(URL, OLDER), 'newer': task(URL.replace('12201487', '12201488'), NEWER)}
        original = deepcopy(tasks)
        for policy in ('cyclic', 'dependent'):
            self.assertEqual(['older'], select_resource(tasks, {}, 0, policy=policy)['task_ids'])
        self.assertEqual(original, tasks)

    def test_shared_list_descendants_keep_actual_oldest_parent_without_rewriting_sources(self):
        roots = [task(LIST, NEWER), task(LIST, OLDER)]
        before = deepcopy(roots)
        page = Page(LIST, b'<a href="/i/12201487/">Motherboard</a>', '2026-10-02T12:00:00Z')
        children, _ = expand_shared_list(roots, page, {'product_patterns': [r'/i/\d+/']})
        self.assertEqual(OLDER, children[0]['created_at'])
        self.assertEqual(before, roots)

    def test_existing_product_and_origin_age_use_actual_oldest_time(self):
        tasks = {'older': task(URL, OLDER), 'newer': task(URL, NEWER)}
        self.assertEqual('older', existing_task_id(tasks, 'ark', URL))
        child = task(URL, '2026-10-02T12:00:00Z')
        key, merged = merge_child(child, tasks, {URL})
        self.assertEqual('older', key)
        self.assertEqual(OLDER, merged['created_at'])
        self.assertEqual(OLDER, task_age({'created_at': NEWER, 'lab_origin_created_at': OLDER}))

    def test_late_parent_age_reaches_grandchildren_and_cycles_without_rewriting_originals(self):
        staged = {
            'root': task(LIST, OLDER, lab_child_keys=['page'], lab_dependencies=['sale']),
            'page': task(LIST + '?page=2', NEWER, lab_child_keys=['product', 'root']),
            'product': task(URL, NEWER, attempts=9, last_error='prior-failure'),
        }
        before = deepcopy(staged)
        changes = inherit_descendants('root', staged)
        self.assertTrue(changes)
        for key in ('page', 'product'):
            self.assertEqual(OLDER, staged[key]['lab_origin_created_at'])
            self.assertEqual(NEWER, staged[key]['created_at'])
            self.assertEqual(['sale'], staged[key]['lab_dependencies'])
        self.assertEqual(9, staged['product']['attempts'])
        self.assertEqual('prior-failure', staged['product']['last_error'])
        self.assertEqual(OLDER, before['root']['created_at'])
        self.assertEqual([], inherit_descendants('root', staged))

    def test_inherited_age_selects_resource_without_mutating_own_registration(self):
        tasks = {'inherited': task(URL, '2026-10-02T12:00:00Z', lab_origin_created_at=OLDER),
                 'newer': task(URL.replace('12201487', '12201488'), NEWER)}
        original = deepcopy(tasks)
        self.assertEqual(['inherited'], select_resource(tasks, {}, 0)['task_ids'])
        self.assertEqual(original, tasks)

    def test_shared_resource_still_preserves_both_task_records(self):
        tasks = {'newer': task(URL, NEWER, attempts=3), 'older': task(URL, OLDER, attempts=8),
                 'other': task(URL.replace('12201487', '12201488'), '2026-10-01T15:45:00Z')}
        original = deepcopy(tasks)
        selected = select_resource(tasks, {}, 0)
        self.assertEqual({'newer', 'older'}, set(selected['task_ids']))
        self.assertEqual(3, selected['remaining_task_count'])
        self.assertEqual(original, tasks)

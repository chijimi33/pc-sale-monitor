from copy import deepcopy
import hashlib
import unittest

from sale_monitor.models import STORES
from monitor_lab.coverage import coverage_report
from monitor_lab.evidence import FIELDS


RUN = 'lab-coverage'
PRODUCT = 'https://www.ark-pc.co.jp/i/1/'
SECOND = 'https://www.ark-pc.co.jp/i/2/'
HOME = 'https://www.ark-pc.co.jp/'
BODY_HASH = hashlib.sha256(b'product').hexdigest()
CONFIG = {store: {'adapter': 'html'} for store in STORES}


def observation(url=PRODUCT, run=RUN):
    return {'offer': {'store': 'ark', 'url': url, 'observed_run_id': run},
            'receipt': {'url': url, 'status': 200, 'body_sha256': BODY_HASH, 'error': None,
                        'evidence_mode': 'replay', 'attempts': []},
            'fields': {'price_yen': {'status': 'observed', 'value': 10000, 'sources': [{'body_sha256': BODY_HASH}]},
                       'stock': {'status': 'observed', 'value': 'unknown', 'sources': [{}]},
                       'shipping_yen': {'status': 'conflict', 'value': 0, 'selected_value': None, 'sources': [{}]}}}


def inputs():
    resources = {PRODUCT: {'store': 'ark', 'url': PRODUCT, 'kind': 'product'},
                 SECOND: {'store': 'ark', 'url': SECOND, 'kind': 'product'},
                 HOME: {'store': 'ark', 'url': HOME, 'kind': 'home'}}
    originals = {'early': {'lab_store': 'ark', 'created_at': '2000-01-01', 'attempts': 7},
                 'yahoo': {'lab_store': 'yahoo', 'created_at': '2001-01-01', 'attempts': 2}}
    records = {'tasks': {'early': {**originals['early'], 'lab_status': 'complete', 'lab_selected': True},
                         'yahoo': {**originals['yahoo'], 'lab_status': 'external_wait', 'lab_selected': False,
                                   'lab_reason': 'yahoo_client_id_unconfigured'}},
               'observations': {'current': observation()}, 'dispatches': {}}
    return resources, records, originals


class CoverageTest(unittest.TestCase):
    def report(self, resources, records, originals, **kwargs):
        return coverage_report(resources, records, originals, CONFIG, kwargs.get('mode', 'replay'), RUN)

    def test_fixed_denominator_keeps_unselected_and_external_wait_stores_without_mutation(self):
        resources, records, originals = inputs()
        before = deepcopy((resources, records, originals, CONFIG))
        result = self.report(resources, records, originals)
        self.assertEqual(before, (resources, records, originals, CONFIG))
        self.assertEqual(set(STORES), set(result['stores']))
        self.assertEqual(10, result['monitored_store_count'])
        self.assertEqual(1, result['stores_with_current_run_observations'])
        self.assertFalse(result['full_store_coverage_proven'])
        self.assertEqual(0, result['stores']['dospara']['current_run_observations'])
        self.assertEqual({'yahoo_client_id_unconfigured': 1}, result['stores']['yahoo']['pending_reason_counts'])
        self.assertEqual({'expected': 1, 'retained': 1, 'missing': 0, 'complete_in_lab': 1, 'pending_in_lab': 0},
                         result['stores']['ark']['original_tasks'])

    def test_old_prices_historical_catalog_and_non_product_pages_do_not_fill_product_gaps(self):
        resources, records, originals = inputs()
        records['observations'].update(old=observation(SECOND, 'old-production-run'), list_page=observation(HOME))
        records['identity_catalog'] = {'old': {'store': 'ark', 'url': SECOND, 'price_yen': 1}}
        records['source_files'] = {'state/history/old.json': {'sha256': BODY_HASH}}
        row = self.report(resources, records, originals)['stores']['ark']
        self.assertEqual([PRODUCT], row['product_resources_observed'])
        self.assertEqual([SECOND], row['product_resources_missing'])
        self.assertEqual(1, row['current_run_observations'])
        self.assertEqual(1, row['other_run_observations'])
        self.assertEqual({'other_run': 1, 'outside_permitted_product_resources': 1}, row['excluded_observation_counts'])

    def test_failed_incomplete_or_unattributed_receipt_never_counts_as_observed_product(self):
        mutations = [{'error': 'transport:TimeoutError'}, {'body_incomplete': True}, {'status': 403},
                     {'body_sha256': None}, {'url': SECOND}, {'body_unavailable': 'no_bytes'},
                     {'parser_body_kind': 'rendered_dom', 'rendered_dom': None},
                     {'parser_body_kind': 'rendered_dom', 'rendered_dom': {'body_sha256': BODY_HASH, 'body_incomplete': True}}]
        for mutation in mutations:
            resources, records, originals = inputs()
            records['observations']['current']['receipt'].update(mutation)
            with self.subTest(mutation=mutation):
                result = self.report(resources, records, originals)
                row = result['stores']['ark']
                self.assertEqual(0, result['stores_with_current_run_observations'])
                self.assertEqual([], row['product_resources_observed'])
                self.assertEqual([PRODUCT, SECOND], row['product_resources_missing'])
                self.assertEqual({'receipt_not_usable': 1}, row['excluded_observation_counts'])

    def test_one_shared_dispatch_is_not_counted_once_per_task_or_old_attempt(self):
        resources, records, originals = inputs()
        receipt = {'url': PRODUCT, 'status': 200, 'attempts': [{'sequence': 1}], 'evidence_mode': 'live'}
        records['tasks']['duplicate'] = {**deepcopy(records['tasks']['early']), 'lab_attempts': [receipt, receipt]}
        records['dispatches'] = {
            'request': {'url': PRODUCT, 'state': 'committed', 'receipt': receipt, 'task_ids': ['early', 'duplicate']},
            'unknown': {'url': SECOND, 'state': 'interrupted_unknown', 'task_ids': ['early']}}
        result = self.report(resources, records, originals, mode='live')
        row = result['stores']['ark']
        self.assertEqual(1, row['confirmed_http_attempts'])
        self.assertEqual(0, row['replay_receipts'])
        self.assertEqual({'committed': 1, 'interrupted_unknown': 1}, row['dispatch_state_counts'])
        self.assertEqual(2, result['totals']['recorded_dispatches'])
        self.assertEqual(1, result['totals']['confirmed_http_attempts'])

    def test_stored_replay_receipts_remain_counted_without_invocation_receipts(self):
        resources, records, originals = inputs()
        records['dispatches'] = {'one': {'url': PRODUCT, 'state': 'committed', 'receipt': observation()['receipt']},
                                'two': {'url': HOME, 'state': 'committed', 'receipt': {**observation(HOME)['receipt'], 'error': 'http_404'}}}
        result = self.report(resources, records, originals)
        self.assertEqual(2, result['stores']['ark']['replay_receipts'])
        self.assertEqual(0, result['totals']['confirmed_http_attempts'])
        self.assertEqual({'http_404': 1}, result['stores']['ark']['receipt_error_counts'])
        self.assertEqual(1, result['totals']['product_resources_observed'])

    def test_captured_source_attempts_are_not_reported_as_new_http_requests(self):
        resources, records, originals = inputs()
        receipt = {**observation()['receipt'], 'attempts': [{'sequence': 7}],
                   'evidence_mode': 'captured_queue_simulation'}
        records['dispatches'] = {'one': {'url': PRODUCT, 'state': 'committed', 'receipt': receipt}}
        result = self.report(resources, records, originals, mode='captured_queue_simulation')
        self.assertEqual(0, result['totals']['confirmed_http_attempts'])
        self.assertEqual(1, result['totals']['recorded_source_http_attempts'])
        self.assertEqual(1, result['stores']['ark']['replay_receipts'])

    def test_all_field_records_stay_distinct_from_unknown_values_and_verification(self):
        resources, records, originals = inputs()
        fields = self.report(resources, records, originals)['stores']['ark']['field_evidence']
        self.assertEqual(set(FIELDS), set(fields))
        self.assertEqual({'status_counts': {'observed': 1}, 'unknown_values': 0, 'with_sources': 1}, fields['price_yen'])
        self.assertEqual({'observed': 1}, fields['stock']['status_counts'])
        self.assertEqual(1, fields['stock']['unknown_values'])
        self.assertEqual({'conflict': 1}, fields['shipping_yen']['status_counts'])
        self.assertEqual(1, fields['shipping_yen']['unknown_values'])
        self.assertEqual({'missing_record': 1}, fields['model']['status_counts'])
        self.assertEqual(0, fields['model']['with_sources'])

    def test_missing_original_tasks_are_reported_instead_of_shrinking_the_denominator(self):
        resources, records, originals = inputs()
        del records['tasks']['yahoo']
        row = self.report(resources, records, originals)['stores']['yahoo']['original_tasks']
        self.assertEqual({'expected': 1, 'retained': 0, 'missing': 1, 'complete_in_lab': 0, 'pending_in_lab': 0}, row)
        invalid = dict(CONFIG); del invalid['yahoo']
        with self.assertRaises(ValueError):
            coverage_report(resources, records, originals, invalid, 'replay', RUN)

    def test_ambiguous_or_excluded_resource_ownership_is_rejected(self):
        resources, records, originals = inputs()
        resources['duplicate'] = {**resources[PRODUCT], 'store': 'koubou'}
        with self.assertRaises(ValueError): self.report(resources, records, originals)
        resources.pop('duplicate')
        resources[PRODUCT]['store'] = 'rakuten'
        with self.assertRaises(ValueError): self.report(resources, records, originals)


if __name__ == '__main__':
    unittest.main()

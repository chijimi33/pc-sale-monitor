"""Exercise review metadata through the complete public JSON writer."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from sale_monitor.models import Offer
from sale_monitor.reporting import aggregate
from sale_monitor.storage import Store, atomic_json, read_json

NOW = datetime(2026, 11, 9, 7, 0, tzinfo=timezone.utc)
RUN = '42572432102-1'

class FlyerReviewReporting(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'state'
        self.public = Path(self.temp.name) / 'public'
        self.flyer = {'edition': 'fixture', 'review_issues': [],
                      'assets': [{'sha256': 'image', 'extraction_path': 'flyers/ocr/image.json'}]}
        self.candidates = [{'candidate_id': 'a', 'price_yen': 9000},
                           {'candidate_id': 'b', 'price_yen': 10000}]
        atomic_json(self.root / 'flyers/ocr/image.json', {'candidates': self.candidates})
        offer = Offer('ark', 'item', 'https://example.invalid/item', title='Fixture',
                      observed_at=NOW.isoformat(), observed_run_id=RUN)
        row = {'observation_id': 'retained', 'run_id': RUN, 'offer': offer.to_dict()}
        self.state = {'store': 'ark', 'run_id': RUN, 'status': 'partial',
                      'queue': {'old': {'type': 'product', 'created_at': '2026-09-01T00:00:00+00:00',
                                        'attempts': 17, 'last_error': 'http_403'}},
                      'done': ['finished'], 'offers': {offer.key: offer.to_dict()},
                      'journal': [deepcopy(row), deepcopy(row)]}
        atomic_json(self.root / 'stores/ark.json', self.state)
        self.old = {**deepcopy(row), 'observation_id': 'older', 'run_id': 'old'}
        self.old['offer']['observed_run_id'] = 'old'
        atomic_json(self.root / 'history/ark/2026-11-09.json', [self.old])

    def publish(self, dismissals):
        self.flyer['dismissed_candidates'] = deepcopy(dismissals)
        atomic_json(self.root / 'flyers/latest.json', self.flyer)
        aggregate(self.root, self.public, RUN, NOW)
        result = read_json(self.public / 'flyer_review.json', {})
        self.assertEqual(read_json(self.root / 'flyers/latest.json', {}), self.flyer)
        self.assertEqual(result['flyer'], read_json(self.public / 'review_queue.json', {})['flyer'])
        return result

    def test_asset_wide_notes_retain_candidates_and_complete_publication(self):
        notes = [{'source_asset_hash': 'image', 'reason': 'Service advert, not a product price'}]
        result = self.publish(notes)
        self.assertEqual(result['assets'][0]['candidates'], self.candidates)
        self.assertEqual(result['flyer']['dismissed_candidates'], notes)
        self.assertIn('invalid_candidate_dismissals', result['flyer']['review_issues'])
        self.assertEqual(result['flyer']['dismissal_review_issues'],
                         [{'index': 0, 'reason': 'candidate_id_missing_or_invalid'}])
        index = read_json(self.public / 'latest.json', {})
        self.assertEqual(index['run_id'], RUN)
        self.assertEqual(index['monitored_store_count'], 10)
        self.assertEqual(index['excluded_stores'], ['rakuten'])
        for filename in index['files'].values():
            self.assertIsInstance(read_json(self.public / filename, None), dict)
        self.assertFalse(read_json(self.public / 'validation.json', {})['cutover_ready'])

    def test_only_explicit_candidate_id_is_dismissed(self):
        result = self.publish([{'candidate_id': 'a', 'reason': 'Manually checked service advert'}])
        self.assertEqual(result['assets'][0]['candidates'], [self.candidates[1]])
        self.assertEqual(result['flyer']['review_issues'], [])

    def test_malformed_rows_do_not_hide_candidates_or_stop_publication(self):
        rows = [None, 'note', [], {'candidate_id': []}, {'candidate_id': ' '},
                {'candidate_id': 'a'}, {'candidate_id': 'a', 'reason': ''},
                {'candidate_id': 'a', 'reason': 3}]
        result = self.publish(rows)
        self.assertEqual(result['assets'][0]['candidates'], self.candidates)
        self.assertEqual(len(result['flyer']['dismissal_review_issues']), len(rows))

    def test_malformed_container_is_visible_and_dismisses_nothing(self):
        for value in (None, {}, 'note', 12):
            with self.subTest(value=value):
                result = self.publish(value)
                self.assertEqual(result['assets'][0]['candidates'], self.candidates)
                self.assertEqual(result['flyer']['dismissal_review_issues'],
                                 [{'reason': 'dismissed_candidates_not_list'}])

    def test_mixed_rows_apply_only_valid_explicit_dismissals(self):
        result = self.publish([{'source_asset_hash': 'image', 'reason': 'Asset note'},
                               {'candidate_id': 'b', 'reason': 'Reviewed advert'}])
        self.assertEqual(result['assets'][0]['candidates'], [self.candidates[0]])
        self.assertEqual(len(result['flyer']['dismissal_review_issues']), 1)

    def test_repeated_publication_preserves_queue_and_archives_history_once(self):
        for _ in range(2):
            self.publish([{'source_asset_hash': 'image', 'reason': 'Legacy note'}])
            after = read_json(self.root / 'stores/ark.json', {})
            self.assertEqual({k: v for k, v in after.items() if k != 'journal'},
                             {k: v for k, v in self.state.items() if k != 'journal'})
            self.assertEqual(after['journal'], [])
            history = Store(self.root).history()
            self.assertEqual(sorted(r['observation_id'] for r in history), ['older', 'retained'])
            self.assertIn(self.old, history)

    def test_shipped_review_files_complete_publication(self):
        repo = Path(__file__).resolve().parents[1]
        for path in sorted((repo / 'config/flyer_reviews').glob('*.json')):
            with self.subTest(review=path.name):
                review = json.loads(path.read_text(encoding='utf-8'))
                result = self.publish(review.get('dismissed_candidates', []))
                self.assertNotIn('dismissal_review_issues', result['flyer'])

if __name__ == '__main__':
    unittest.main()

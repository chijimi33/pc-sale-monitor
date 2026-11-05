from copy import deepcopy
import hashlib
import unittest

from monitor_lab.evidence import normalize
from monitor_lab.tests.test_identity import CFG, JAN, OTHER, URL, ld, markup, page, product, row
from sale_monitor.parsing import parse_product


class IdentityIntegrationTest(unittest.TestCase):
    def normalize(self, body):
        value = page(body)
        receipt = {'body_sha256': hashlib.sha256(value.body).hexdigest(), 'body_kind': 'http_response'}
        return value, normalize('sofmap', value, CFG, 'lab-identity-integration', receipt)

    def test_scoped_jan_overrides_missing_baseline_without_mutating_other_fields(self):
        p, observation = self.normalize(markup())
        baseline = parse_product('sofmap', p, CFG)
        expected = baseline.to_dict()
        expected.update(jan=JAN, observed_run_id='lab-identity-integration')
        self.assertEqual(expected, observation.offer.to_dict())
        self.assertIsNone(observation.fields['jan']['value'])
        self.assertEqual(JAN, observation.fields['jan']['selected_value'])
        self.assertEqual('observed', observation.fields['jan']['status'])
        proof = next(s for s in observation.fields['jan']['sources'] if s['kind'] == 'primary_product_label')
        self.assertEqual(observation.receipt['body_sha256'], proof['body_sha256'])

    def test_unknown_primary_scope_does_not_retain_footer_jan(self):
        p, observation = self.normalize('<footer><table>' + row('JAN', JAN) + '</table></footer>')
        self.assertEqual(JAN, parse_product('sofmap', p, CFG).jan)
        self.assertIsNone(observation.offer.jan)
        self.assertEqual(JAN, observation.fields['jan']['value'])
        self.assertIsNone(observation.fields['jan']['selected_value'])

    def test_conflict_is_retained_as_offer_issue_without_changing_baseline(self):
        body = markup(after=ld(product(gtin13=OTHER)))
        p = page(body)
        before = deepcopy(parse_product('sofmap', p, CFG).to_dict())
        _, observation = self.normalize(body)
        self.assertIsNone(observation.offer.jan)
        self.assertEqual('conflict', observation.fields['jan']['status'])
        self.assertIn('jan_conflict_review_needed', observation.offer.issues)
        self.assertEqual(before, parse_product('sofmap', p, CFG).to_dict())

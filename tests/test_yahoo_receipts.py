"""Signed public discovery; synthetic responses, no network or shipping shortcuts."""
from pathlib import Path
from copy import deepcopy
import json,sys,unittest
from unittest.mock import patch
ROOT=Path(__file__).parent
import test_receipts as B
import test_yahoo_public as Y
from sale_monitor import receipts as R
from sale_monitor.models import Offer

class YahooReceipts(unittest.TestCase):
    add=B.Receipts.add
    setup_receiver=B.Receipts.setup_receiver
    load=B.Receipts.load
    receive=B.Receipts.receive
    edit_manifest=B.Receipts.edit_manifest
    rejected=B.Receipts.rejected
    def setUp(self):
        B.Receipts.setUp(self)
        self.enterContext(patch.dict('os.environ',{},clear=True))
        self.config=deepcopy(Y.CFG)
        self.state.update(store='yahoo',queue={},done=['old-done'],journal=[{'prior':True}])
        self.state['queue']={'head':{'type':'yahoo','query':Y.QUERY,'start':1,'kind':'comparison',
            'priority':1,'requested':True,'created_at':B.OLD,'attempts':3},
            'untouched':{'type':'yahoo','query':'別の製品','start':1,'created_at':B.OLD,'attempts':7}}
    def make(self,data=None,ids=None,pages=None):
        self.setup_receiver(ids or ['head'])
        pages=pages or {Y.search_url(Y.QUERY):Y.page(data or Y.search_data(),Y.search_url(Y.QUERY))}
        self.result=R.produce(self.bundle,self.lease,self.state,self.config,B.Fake(pages),self.key)
        self.bundle=Path(self.result['checkpoint'])
        return self.receive()
    def test_search_receipt_creates_native_children_preserving_original_age(self):
        receipt=self.make();state=self.load()['state']
        self.assertEqual((receipt['successes'],receipt['failures'],receipt['requests']),(1,0,1))
        self.assertEqual(state['queue']['untouched'],self.state['queue']['untouched'])
        child=next(t for t in state['queue'].values() if t['type']=='product')
        self.assertEqual((child['url'],child['created_at'],child['priority'],child['comparison_queries']),
                         (Y.URL,B.OLD,1,[Y.QUERY]))
        self.assertEqual(state['offers'],{})
        self.assertEqual(state['journal'],[{'prior':True}])
        self.assertEqual(state['done'],['old-done','head'])
        self.assertFalse(receipt['scheduled_credit']);self.assertFalse(receipt['formal_audit_added'])
    def test_explicit_no_results_completes_only_the_bound_search(self):
        receipt=self.make(Y.zero_search_data());state=self.load()['state']
        self.assertEqual((receipt['successes'],receipt['failures']),(1,0))
        self.assertEqual(state['queue'],{'untouched':self.state['queue']['untouched']})
        self.assertEqual(state['offers'],self.state['offers'])
        self.assertEqual(state['journal'],self.state['journal'])
        evidence=state['yahoo_public_searches'][Y.search_url(Y.QUERY)]
        self.assertEqual((evidence['result'],evidence['query'],evidence['observed_run_id']),('no_results',Y.QUERY,B.RUN))
        self.assertFalse(receipt['scheduled_credit'])
    def test_public_pagination_receipt_preserves_age_and_scope(self):
        receipt=self.make(Y.search_data(more=True));state=self.load()['state']
        child=next(t for t in state['queue'].values() if t['type']=='yahoo_public_search')
        self.assertEqual((child['query'],child['start'],child['url'],child['created_at']),
                         (Y.QUERY,2,Y.search_url(Y.QUERY,2),B.OLD))
        self.assertTrue(child['requested'])
    def test_existing_page_two_task_runs_with_its_exact_cursor(self):
        self.state['queue']['head'].update(type='yahoo_public_search',start=2,url=Y.search_url(Y.QUERY,2))
        receipt=self.make(ids=['head'],pages={Y.search_url(Y.QUERY,2):Y.page(Y.search_data(2),Y.search_url(Y.QUERY,2))})
        self.assertEqual(receipt['successes'],1)
        self.assertNotIn('head',self.load()['state']['queue'])
    def test_wrong_query_and_foreign_search_result_are_failed_native_attempts(self):
        data=Y.search_data();data['query']['params'][0]='different'
        receipt=self.make(data);state=self.load()['state']
        self.assertEqual((receipt['successes'],receipt['failures']),(0,1))
        self.assertEqual(set(state['queue']),{'head','untouched'})
        self.assertEqual((state['queue']['head']['created_at'],state['queue']['head']['attempts']),(B.OLD,4))
        self.assertEqual(state['done'],self.state['done']);self.assertEqual(state['journal'],self.state['journal'])
    def test_provider_window_remains_a_gap(self):
        receipt=self.make(Y.search_data(truncated=True));state=self.load()['state']
        self.assertEqual(receipt['successes'],1)
        self.assertEqual(state['discovery_gaps'][0]['reason'],'yahoo_public_provider_window')
        self.assertFalse(state['cycle_complete'])
    def test_unknown_shipping_product_is_not_adopted_or_marked_done(self):
        self.state['queue']['head'].update(type='product',url=Y.URL)
        old=Offer('yahoo','example_part-1',Y.URL,price_yen=123,observed_at=B.OLD,observed_run_id='prior').to_dict()
        self.state['offers']['old']=old
        data,schema=Y.product_data()
        receipt=self.make(pages={Y.URL:Y.page(data,Y.URL,schema)});state=self.load()['state']
        self.assertEqual((receipt['successes'],receipt['failures']),(0,1))
        self.assertEqual((state['offers']['old']['price_yen'],state['offers']['old']['observed_run_id']),(123,'prior'))
        self.assertEqual(state['journal'],self.state['journal'])
        self.assertEqual(state['done'],self.state['done'])
        self.assertEqual((state['queue']['head']['created_at'],state['queue']['head']['attempts']),(B.OLD,4))
    def test_legacy_api_offset_is_rejected_without_mutating_queue(self):
        self.state['queue']['head']['start']=101
        with self.assertRaisesRegex(R.Rejected,'unsupported_task'):self.setup_receiver(['head'])
        self.assertEqual(self.load()['state'],self.state)
        self.assertEqual(self.load()['leases'],{})
    def test_wrong_page_scope_cannot_be_leased(self):
        self.state['queue']['head'].update(type='yahoo_public_search',start=2,url=Y.search_url('other',2))
        with self.assertRaisesRegex(R.Rejected,'unsupported_task'):self.setup_receiver(['head'])
        self.assertEqual(self.load()['state'],self.state)
    def test_api_credential_cannot_enter_public_receipt_at_issue(self):
        with patch.dict('os.environ',{'YAHOO_CLIENT_ID':'synthetic-private-value'}):
            with self.assertRaisesRegex(R.Rejected,'public_receipt_requires_no_api_key'):self.setup_receiver(['head'])
        self.assertNotIn(b'synthetic-private-value',self.path.read_bytes())
        self.assertEqual(self.load()['leases'],{})
    def test_new_api_credential_after_lease_is_rejected_before_network(self):
        self.setup_receiver(['head']);client=B.Fake({});before=self.path.read_bytes()
        with patch.dict('os.environ',{'YAHOO_CLIENT_ID':'synthetic-private-value'}):
            with self.assertRaisesRegex(R.Rejected,'public_receipt_requires_no_api_key'):
                R.produce(self.bundle,self.lease,self.state,self.config,client,self.key)
        self.assertEqual(client.count,0);self.assertEqual(before,self.path.read_bytes())
    def test_public_flag_is_required(self):
        self.config['stores']['yahoo']['public_html_fallback']=False
        with self.assertRaisesRegex(R.Rejected,'unsupported_store'):self.setup_receiver(['head'])
    def test_duplicate_receipt_is_byte_stable(self):
        self.make();before=self.path.read_bytes()
        self.assertEqual(self.receive()['status'],'already_received')
        self.assertEqual(self.path.read_bytes(),before)
    def test_signed_wrong_request_url_cannot_complete_a_different_query(self):
        self.make();r=self.load();r['receipts']={};r['state']=deepcopy(self.state);self.path.write_bytes(R.encode(r))
        self.edit_manifest(lambda m:m['actions'][0].update(url=Y.search_url('other')))
        self.rejected('dependency_mismatch')

if __name__=='__main__':unittest.main()

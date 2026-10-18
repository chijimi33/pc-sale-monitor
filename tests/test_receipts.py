"""Auth and native state transitions; synthetic pages and no external network."""
from pathlib import Path
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timezone,timedelta
import sys, json, unittest, tempfile, socket
from unittest.mock import patch
ROOT=Path(__file__).parent
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sale_monitor import receipts as R
from sale_monitor.http import FetchError
from sale_monitor.http_evidence import ResponseEvidence
from sale_monitor.models import iso,Offer
from sale_monitor.storage import Store
from sale_monitor.runner import Collector
from sale_monitor.comparison_routing import scope,request_key
import test_tsukumo_shipping as T
import test_comparison_routing as C

NOW=datetime.now(timezone.utc).replace(microsecond=0)
OLD=iso(NOW-timedelta(days=3))
RUN='isolated-native-auth-test'

class Fake:
    def __init__(self,pages):
        self.pages=pages;self.count=0;self.evidence=ResponseEvidence();self.retry_after={};self.transport_retry_after={};self.access_retry_after={}
    def get(self,url):return self.call(url,'http')
    def rendered(self,url):return self.call(url,'browser')
    def call(self,url,method):
        if (method,url) not in self.pages and url not in self.pages:raise AssertionError('unspecified_fixture')
        self.count+=1;value=self.pages.get((method,url),self.pages.get(url))
        if isinstance(value,Exception):
            if isinstance(value,FetchError) and str(value).startswith('http_'):
                self.evidence.record(url,method,status=int(str(value)[5:]))
            else:self.evidence.record(url,method,error=value)
            raise value
        page=replace(value,observed_at=iso(NOW),method=method)
        self.evidence.record(url,method,status=page.status)
        return page

class Receipts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='auth-native-');self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.enterContext(patch('socket.create_connection',side_effect=AssertionError('network_forbidden')))
        self.enterContext(patch.object(R,'utcnow',return_value=NOW))
        self.enterContext(patch.object(R,'iso',side_effect=lambda t=None:iso(t or NOW)))
        self.enterContext(patch('sale_monitor.runner.iso',side_effect=lambda t=None:iso(t or NOW)))
        self.enterContext(patch('sale_monitor.runner.utcnow',return_value=NOW))
        self.enterContext(patch('sale_monitor.runner.time.time',return_value=NOW.timestamp()))
        self.enterContext(patch('sale_monitor.http_evidence.iso',return_value=iso(NOW)))
        self.key=Ed25519PrivateKey.generate();self.config=deepcopy(T.CONFIG);self.config['stores']['tsukumo']['browser_fallback']=False
        self.page=T.product_page(observed=iso(NOW));self.policy=T.policy_page(observed=iso(NOW))
        self.state={'schema_version':1,'store':'tsukumo','run_id':'prior','queue':{},'done':[], 'offers':{},'journal':[], 'errors':[], 'cycle_complete':False,'list_pages':0,'listed_candidates':0,'discovery_gaps':[]}
        self.add('z-main',self.page.url);self.add('a-untouched','https://shop.tsukumo.co.jp/goods/4006381333931/')
        self.path=self.root/'receiver.json';self.bundle=self.root/'bundle'
    def add(self,key,url,**extra):
        self.state['queue'][key]={'type':'product','url':url,'kind':'comparison','created_at':OLD,'attempts':2,**extra}
    def setup_receiver(self,ids=None):
        self.ids=ids or ['z-main','a-untouched']
        r=R.receiver(self.state,self.config,'a'*40,'b'*40,RUN,{'worker':{'enabled':True,'stores':[self.state['store']],'public_key_hex':self.key.public_key().public_bytes_raw().hex()}})
        self.path.write_bytes(R.encode(r));self.lease=R.issue(self.path,'worker',self.ids,now=NOW)
    def make(self,pages=None,limit=1,key=None):
        if not self.path.exists():self.setup_receiver()
        self.result=R.produce(self.bundle,self.lease,self.state,self.config,Fake(pages or {self.page.url:self.page,T.POLICY_URL:self.policy}),key or self.key,limit=limit)
        self.bundle=Path(self.result['checkpoint'])
        return self.result
    def load(self):return json.loads(self.path.read_bytes())
    def receive(self,**kwargs):return R.receive(self.bundle,self.path,now=kwargs.pop('now',NOW),**kwargs)
    def edit_manifest(self,fn,sign=True):
        p=self.bundle/'manifest.json';m=json.loads(p.read_bytes());fn(m);raw=R.encode(m);p.write_bytes(raw)
        if sign:(self.bundle/'signature.bin').write_bytes(self.key.sign(R.DOMAIN+raw))
    def rejected(self,reason,**kwargs):
        before=self.path.read_bytes()
        with self.assertRaisesRegex(R.Rejected,reason):self.receive(**kwargs)
        self.assertEqual(before,self.path.read_bytes())
    def test_native_dependency_success_and_unattempted_preserved(self):
        self.make();result=self.receive();s=self.load()['state'];row=next(iter(s['offers'].values()))
        self.assertEqual((result['successes'],result['unattempted'],result['requests']),(1,1,2));self.assertEqual(row['shipping_yen'],550)
        self.assertEqual(s['queue']['a-untouched'],self.state['queue']['a-untouched']);self.assertEqual(s['done'],['z-main'])
        self.assertEqual(s['journal'],self.result['worker_state']['journal']);self.assertEqual(s['offers'],self.result['worker_state']['offers'])
        self.assertEqual(row['evidence'][-1]['fields']['shipping_policy']['product_url'],self.page.url)
    def test_partial_failure_keeps_age_attempt_and_old_price(self):
        old=Offer('tsukumo',T.SECOND_JAN,self.state['queue']['a-untouched']['url'],price_yen=100,observed_at=OLD,observed_run_id='prior').to_dict()
        self.state['offers']['old']=old
        self.make({self.page.url:self.page,T.POLICY_URL:self.policy,self.state['queue']['a-untouched']['url']:TimeoutError('SECRET')},limit=2)
        result=self.receive();s=self.load()['state'];self.assertEqual((result['successes'],result['failures']),(1,1));task=s['queue']['a-untouched']
        self.assertEqual((task['created_at'],task['attempts']),(OLD,3));self.assertEqual(s['offers']['old']['observed_run_id'],'prior');self.assertNotIn(b'SECRET',(self.bundle/'manifest.json').read_bytes())
    def test_explicit_failed_policy_keeps_product_pending(self):
        self.make({self.page.url:self.page,T.POLICY_URL:TimeoutError()});result=self.receive();s=self.load()['state']
        self.assertEqual(result['failures'],1);self.assertEqual(s['queue']['z-main']['created_at'],OLD);self.assertEqual(s['queue']['z-main']['attempts'],3);self.assertFalse(s['offers']);self.assertFalse(s['journal'])
    def test_missing_policy_action_rejects_transaction(self):
        self.make();(self.bundle/'body-01.bin').unlink()
        self.edit_manifest(lambda m:(m['actions'].pop(),m['entries'][0].update(end=1)))
        self.rejected('dependency_missing')
    def test_wrong_product_is_native_failure_and_stays_pending(self):
        wrong=T.product_page(jan=T.SECOND_JAN,shipping=0,observed=iso(NOW));wrong=replace(wrong,url=self.page.url)
        self.make({self.page.url:wrong});result=self.receive();self.assertEqual(result['failures'],1);self.assertIn('z-main',self.load()['state']['queue']);self.assertFalse(self.load()['state']['offers'])
    def test_duplicate_is_byte_identical_even_after_expiry(self):
        self.make();self.receive();before=self.path.read_bytes();result=self.receive(now=NOW+timedelta(days=1));self.assertEqual(result['status'],'already_received');self.assertEqual(before,self.path.read_bytes())
    def test_wrong_key_rejected(self):self.make(key=Ed25519PrivateKey.generate());self.rejected('signature_invalid')
    def test_unsigned_manifest_change_rejected(self):self.make();self.edit_manifest(lambda m:m.update(ended_at=iso(NOW-timedelta(seconds=1))),sign=False);self.rejected('signature_invalid')
    def test_body_tamper_rejected(self):
        self.make();p=self.bundle/'body-00.bin';p.write_bytes(p.read_bytes().replace(b'2780',b'1780'));self.rejected('body_hash_mismatch')
    def test_unknown_lease_rejected(self):self.make();self.edit_manifest(lambda m:m['lease'].update(id='unknown'));self.rejected('lease_mismatch')
    def test_lease_worker_change_rejected(self):self.make();self.edit_manifest(lambda m:m['lease'].update(worker_id='attacker'));self.rejected('lease_mismatch')
    def test_run_change_rejected(self):self.make();self.edit_manifest(lambda m:m['lease'].update(run_id='real-run'));self.rejected('lease_mismatch')
    def test_source_change_rejected(self):self.make();self.edit_manifest(lambda m:m['lease'].update(source_sha='c'*40));self.rejected('lease_mismatch')
    def test_data_change_rejected(self):self.make();self.edit_manifest(lambda m:m['lease'].update(data_sha='c'*40));self.rejected('lease_mismatch')
    def test_trust_revocation_rejected(self):
        self.make();r=self.load();r['workers']['worker']['enabled']=False;self.path.write_bytes(R.encode(r));self.rejected('worker_not_authorized')
    def test_wrong_store_authorization_rejected(self):
        self.make();r=self.load();r['workers']['worker']['stores']=['ark'];self.path.write_bytes(R.encode(r));self.rejected('worker_not_authorized')
    def test_rotated_public_key_rejected(self):
        self.make();r=self.load();r['workers']['worker']['public_key_hex']=Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex();self.path.write_bytes(R.encode(r));self.rejected('signature_invalid')
    def test_config_changed_rejected(self):
        self.make();r=self.load();r['config']['stores']['tsukumo']['browser_fallback']=True;self.path.write_bytes(R.encode(r));self.rejected('config_changed')
    def test_base_changed_rejected(self):
        self.make();r=self.load();r['state']['queue']['a-untouched']['attempts']+=1;self.path.write_bytes(R.encode(r));self.rejected('base_state_changed')
    def test_expired_rejected(self):self.make();self.rejected('lease_or_batch_time_invalid',now=NOW+timedelta(minutes=11))
    def test_future_observation_rejected(self):self.make();self.edit_manifest(lambda m:m['actions'][0]['page'].update(observed_at=iso(NOW+timedelta(seconds=1))));self.rejected('page_time_invalid')
    def test_old_observation_rejected(self):self.make();self.edit_manifest(lambda m:m['actions'][0]['page'].update(observed_at=OLD));self.rejected('page_time_invalid')
    def test_extra_state_rejected(self):self.make();self.edit_manifest(lambda m:m.update(state={'queue':{}}));self.rejected('manifest_schema')
    def test_extra_file_rejected(self):self.make();(self.bundle/'unexpected').write_text('x');self.rejected('unexpected_artifact_files')
    def test_path_escape_rejected(self):self.make();self.edit_manifest(lambda m:m['actions'][0]['page'].update(path='../outside'));self.rejected('body_path_invalid')
    def test_dependency_url_swap_rejected(self):self.make();self.edit_manifest(lambda m:m['actions'][1].update(url=self.page.url));self.rejected('dependency_mismatch')
    def test_foreign_redirect_rejected(self):self.make();self.edit_manifest(lambda m:m['actions'][0]['page'].update(url='https://example.com/product'));self.rejected('redirect_host_mismatch')
    def test_request_accounting_mismatch_rejected(self):self.make();self.edit_manifest(lambda m:m['actions'][0].update(count=2));self.rejected('http_count_mismatch')
    def test_duplicate_task_rejected(self):self.make();self.edit_manifest(lambda m:m['entries'].append(deepcopy(m['entries'][0])));self.rejected('task_order_mismatch')
    def test_unknown_task_rejected(self):self.make();self.edit_manifest(lambda m:m['entries'][0].update(task_id='unknown'));self.rejected('task_order_mismatch')
    def test_unused_action_rejected(self):self.make();self.edit_manifest(lambda m:m['entries'][0].update(end=1));self.rejected('unused_actions')
    def test_raw_error_rejected(self):
        self.make({self.page.url:TimeoutError('secret')});self.edit_manifest(lambda m:m['actions'][0].update(reason='token=SECRET'));self.rejected('error_reason_invalid')
    def test_signature_domain_separation(self):
        self.make();raw=(self.bundle/'manifest.json').read_bytes();(self.bundle/'signature.bin').write_bytes(self.key.sign(raw));self.rejected('signature_invalid')
    def test_presave_failure_leaves_receiver_unchanged_and_retry_succeeds(self):
        self.make();before=self.path.read_bytes()
        def crash(stage):
            if stage=='before_replace':raise RuntimeError('injected')
        with self.assertRaisesRegex(RuntimeError,'injected'):self.receive(hook=crash)
        self.assertEqual(before,self.path.read_bytes());self.assertEqual(self.receive()['successes'],1)
    def test_postsave_failure_retry_does_not_duplicate(self):
        self.make()
        def crash(stage):
            if stage=='after_replace':raise RuntimeError('injected')
        with self.assertRaisesRegex(RuntimeError,'injected'):self.receive(hook=crash)
        before=self.path.read_bytes();self.assertEqual(self.receive()['status'],'already_received');self.assertEqual(before,self.path.read_bytes())
    def test_os_lock_contention(self):
        self.make()
        with R.writer_lock(self.path.with_suffix('.lock')):self.rejected('receiver_busy')
    def test_same_policy_cached_across_two_products(self):
        second=T.product_page(jan=T.SECOND_JAN,observed=iso(NOW));self.make({self.page.url:self.page,second.url:second,T.POLICY_URL:self.policy},limit=2)
        r=self.receive();self.assertEqual((r['successes'],r['requests']),(2,3));self.assertEqual(len(self.load()['state']['offers']),2)
    def test_comparison_failure_routes_original_search(self):
        target=C.offer('tsukumo',T.JAN,url=self.page.url,jan=T.JAN,model=None,brand=None,price_yen=2780,observed_at=OLD)
        candidate=C.candidate(jan=T.JAN,model=None,brand=None,observed_at=iso(NOW))
        req={'identity':candidate.identity,'query':T.JAN,'priority':1,'candidates':[candidate.to_dict()]};rid=request_key(req)
        self.state['offers'][target.key]=target.to_dict()
        self.state['queue']['z-main']['comparison_routes']={rid+':slot':{'request_id':rid,'request':req,'expected':scope(target),'created_at':OLD}}
        self.make({self.page.url:TimeoutError()});r=self.receive();s=self.load()['state'];search=[t for t in s['queue'].values() if t['type']=='search']
        self.assertEqual(r['failures'],1);self.assertEqual(len(search),1);self.assertEqual(search[0]['created_at'],OLD);self.assertEqual(search[0]['query'],T.JAN)
    def test_search_task_enqueues_native_list_with_original_age(self):
        from sale_monitor.http import Page
        home='https://shop.tsukumo.co.jp/';self.config['stores']['tsukumo']['seed_urls']=[home]
        self.state['queue']['z-main']={'type':'search','query':T.JAN,'kind':'comparison','created_at':OLD,'attempts':0,'priority':1}
        page=Page(home,b'<html><form method="get" action="/search"><input name="q" type="search"></form></html>',iso(NOW),content_type='text/html')
        self.make({home:page});r=self.receive();s=self.load()['state'];children=[t for t in s['queue'].values() if t['type']=='list'];self.assertEqual(r['successes'],1);self.assertEqual(len(children),1);self.assertEqual(children[0]['created_at'],OLD)
    def test_two_failure_receipt_does_not_promote_old_prices(self):
        self.make({self.page.url:TimeoutError(),self.state['queue']['a-untouched']['url']:TimeoutError()},limit=2);r=self.receive();s=self.load()['state'];self.assertEqual((r['successes'],r['failures'],len(s['queue'])),(0,2,2));self.assertFalse(s['offers']);self.assertFalse(s['done'])

    def test_future_task_retry_is_not_leased(self):
        self.state['queue']['z-main']['retry_at_epoch_seconds']=NOW.timestamp()+300
        with self.assertRaisesRegex(R.Rejected,'tasks_waiting_or_already_attempted'):self.setup_receiver()
    def test_server_wait_is_not_leased(self):
        self.state['retry_after']={'shop.tsukumo.co.jp':NOW.timestamp()+600}
        with self.assertRaisesRegex(R.Rejected,'tasks_waiting_or_already_attempted'):self.setup_receiver()
    def test_transport_wait_is_not_leased(self):
        self.state['transport_retry_after']={'shop.tsukumo.co.jp':{'until':NOW.timestamp()+600,'reason':'TimeoutError'}}
        with self.assertRaisesRegex(R.Rejected,'tasks_waiting_or_already_attempted'):self.setup_receiver()
    def test_access_wait_is_not_leased(self):
        self.state['access_retry_after']={'shop.tsukumo.co.jp':{'until':NOW.timestamp()+600,'reason':'http_403'}}
        with self.assertRaisesRegex(R.Rejected,'tasks_waiting_or_already_attempted'):self.setup_receiver()
    def test_browser_fallback_replayed_as_separate_response(self):
        self.config['stores']['tsukumo']['browser_fallback']=True
        page=T.product_page(shipping=0,observed=iso(NOW))
        self.make({('http',self.page.url):FetchError('http_403'),('browser',self.page.url):page})
        r=self.receive();h=self.load()['state']['http_evidence'];self.assertEqual((r['requests'],r['successes']),(2,1));self.assertEqual(h['response_status_counts'],{'http':{'403':1},'browser':{'200':1}})
    def test_verified_comparison_uses_current_receipt(self):
        target=C.offer('tsukumo',T.JAN,url=self.page.url,jan=T.JAN,model=None,brand=None,price_yen=2780,observed_at=OLD)
        candidate=C.candidate(jan=T.JAN,model=None,brand=None,observed_at=iso(NOW))
        req={'identity':candidate.identity,'query':T.JAN,'priority':1,'candidates':[candidate.to_dict()]};rid=request_key(req)
        self.state['offers'][target.key]=target.to_dict()
        self.state['queue']['z-main']['comparison_routes']={rid+':slot':{'request_id':rid,'request':req,'expected':scope(target),'created_at':OLD}}
        self.make();r=self.receive();s=self.load()['state'];routes=list(s['comparison_routing']['receipts'].values());self.assertEqual(r['successes'],1);self.assertEqual(routes[0]['request_id'],rid);self.assertEqual(routes[0]['result'],'verified_current_product');self.assertFalse([t for t in s['queue'].values() if t['type']=='search'])

    def test_first_checkpoint_survives_later_worker_failure(self):
        self.setup_receiver()
        def stop(checkpoint):raise RuntimeError('worker_interrupted')
        root=self.bundle
        with self.assertRaisesRegex(RuntimeError,'worker_interrupted'):
            R.produce(root,self.lease,self.state,self.config,Fake({self.page.url:self.page,T.POLICY_URL:self.policy}),self.key,on_checkpoint=stop)
        self.bundle=root/'checkpoint-0001'
        (root/'working'/'orphan-next-task.bin').write_bytes(b'partial')
        r=self.receive();self.assertEqual((r['successes'],r['unattempted']),(1,1));self.assertEqual(self.load()['state']['queue']['a-untouched'],self.state['queue']['a-untouched'])
    def test_checkpoints_are_immutable_and_only_one_is_consumed(self):
        second=T.product_page(jan=T.SECOND_JAN,observed=iso(NOW))
        root=self.bundle;self.make({self.page.url:self.page,second.url:second,T.POLICY_URL:self.policy},limit=2)
        self.assertTrue((root/'checkpoint-0001'/'manifest.json').exists());self.assertTrue((root/'checkpoint-0002'/'manifest.json').exists())
        self.receive();self.bundle=root/'checkpoint-0001';self.rejected('lease_already_consumed')

if __name__=='__main__':unittest.main(verbosity=2)

"""Authenticated batching, crash recovery, metrics, and publisher conflicts."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import io
import json
import os
import subprocess
import sys
import time
import unittest
import zipfile
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sale_monitor import receipts as R, native_sessions as S, native_channel as N
from sale_monitor.models import iso
from sale_monitor.reporting import checkpoint
from sale_monitor.storage import atomic_json
import test_receipts as B
import test_yahoo_public as Y


class Crash(BaseException):pass


class Sessions(unittest.TestCase):
    add=B.Receipts.add
    def setUp(self):
        B.Receipts.setUp(self)
        self.enterContext(patch.dict('os.environ',{'YAHOO_CLIENT_ID':''}))
        self.enterContext(patch.object(S,'utcnow',return_value=B.NOW))
        self.enterContext(patch.object(S,'iso',side_effect=lambda t=None:iso(t or B.NOW)))
        self.config=deepcopy(Y.CFG);self.run='123456-1';self.cloud=Ed25519PrivateKey.generate()
        self.state.update(store='yahoo',run_id=self.run,queue={},done=['prior-done'],journal=[{'prior':True}])
        self.pages={}
        for index in range(6):
            query=f'example-{index}'
            self.state['queue'][str(index)]={'type':'yahoo','query':query,'start':1,'created_at':B.OLD,
                'attempts':index,'kind':'sale' if index%2==0 else 'comparison','priority':index%2}
            data=json.loads(json.dumps(Y.zero_search_data(),ensure_ascii=False).replace(Y.QUERY,query))
            header=data['props']['initialState']['bff']['searchResultHeader']
            header['title']['main']['taxonomies'][0]['items'][0]['url']=Y.search_url(query)
            self.pages[Y.search_url(query)]=Y.page(data,Y.search_url(query))
        self.workers={N.WORKER:{'enabled':True,'stores':['yahoo'],'public_key_hex':self.key.public_key().public_bytes_raw().hex()}}
        self.path.write_bytes(R.encode(R.receiver(self.state,self.config,'a'*40,'b'*40,self.run,self.workers,scope=R.COLLECTION_SCOPE)))
        self.session=S.session_metadata(self.config,'a'*40,'b'*40,self.run,self.cloud,now=B.NOW)
        self.policy={'enabled':True,'source_sha':'a'*40,'runtime_sha256':R.runtime_hash()}
        self.worker=self.root/'worker'
    def load(self):return json.loads(self.path.read_bytes())
    def issue(self,sequence=1):
        root=self.root/f'input-{sequence}'
        body=S.signed_input(root,self.path,self.session,self.cloud,sequence,now=B.NOW)
        self.assertEqual(S.read_input(root,self.session,self.config,now=B.NOW),body)
        return body
    def execute(self,body,**kwargs):
        return S.execute_claim(self.worker,body,self.config,self.key,client_factory=lambda:B.Fake(self.pages),now=B.NOW,**kwargs)
    def accept(self,result):return R.receive(Path(result['checkpoint_path']),self.path,now=B.NOW)
    def export(self):
        self.incoming=self.root/'incoming'
        S.export_state(self.load(),self.session,'c'*40,R.sha(R.encode(self.state)),self.cloud,self.incoming)
        self.final=json.loads((self.incoming/'stores/yahoo.json').read_bytes())
        self.trust={'metadata':self.session,'commit':'c'*40}
    def verify(self,**kwargs):
        return S.verify_export(self.incoming,self.final,kwargs.pop('current',self.state),kwargs.pop('trust',self.trust),
            source_sha=kwargs.pop('source_sha','a'*40),run_id=kwargs.pop('run_id',self.run),data_sha=kwargs.pop('data_sha','b'*40))
    def test_two_batches_use_updated_state_and_sum_once(self):
        first=self.issue();one=self.execute(first);accepted=self.accept(one)
        self.assertEqual(accepted['successes'],4)
        second=self.issue(2)
        self.assertEqual(len(second['lease']['tasks']),2)
        self.assertEqual(second['state'],self.load()['state'])
        self.assertNotEqual(first['lease']['base_state_sha256'],second['lease']['base_state_sha256'])
        two=self.execute(second);self.accept(two)
        state=self.load()['state']
        self.assertEqual(state['request_count'],6)
        self.assertEqual(state['http_evidence']['response_status_counts']['http'],{'200':6})
        self.assertEqual(state['journal'],[{'prior':True}]);self.assertFalse(state['queue'])
        self.assertEqual(state['scheduler']['selected_by_lane'],{'sale':3,'comparison':3})
        before=self.path.read_bytes();self.accept(two);self.assertEqual(self.path.read_bytes(),before)
    def test_issue_does_not_move_durable_cursor(self):
        before=self.load()['state'];body=self.issue()
        self.assertEqual(self.load()['state'],before)
        self.assertEqual(len(body['lease']['scheduler_after']),4)
    def test_partial_receipt_commits_only_attempted_cursor(self):
        body=self.issue()
        def interrupt(stage):
            if stage=='after_checkpoint':raise Crash()
        with self.assertRaises(Crash):self.execute(body,hook=interrupt)
        result=self.execute(body);receipt=self.accept(result)
        self.assertEqual(receipt['unattempted'],3)
        self.assertEqual(self.load()['state']['scheduler'],body['lease']['scheduler_after'][0])
        for key in body['lease']['task_order'][1:]:self.assertEqual(self.load()['state']['queue'][key],self.state['queue'][key])
    def test_repeated_claim_never_fetches_again(self):
        body=self.issue();first=self.execute(body)
        with patch.object(B.Fake,'get',side_effect=AssertionError('duplicate_fetch')):
            repeated=self.execute(body)
        self.assertEqual(first['manifest_sha256'],repeated['manifest_sha256']);self.assertTrue(repeated['resumed'])
    def test_crash_after_claim_is_ambiguous_and_never_fetches(self):
        body=self.issue()
        def crash(stage):
            if stage=='after_claim':raise Crash()
        with self.assertRaises(Crash):self.execute(body,hook=crash)
        with patch.object(B.Fake,'get',side_effect=AssertionError('duplicate_fetch')):
            result=self.execute(body);again=self.execute(body)
        self.assertEqual(result['status'],again['status']);self.assertEqual(result['status'],'ambiguous')
        self.assertIsNone(result['requests']);self.assertEqual(self.load()['state'],self.state)
    def test_changed_input_cannot_reuse_claim(self):
        body=self.issue();self.execute(body);body['state']['done'].append('injected')
        with self.assertRaisesRegex(R.Rejected,'claim_input_changed'):self.execute(body)
    def test_uploaded_claim_is_noop(self):
        body=self.issue();first=self.execute(body)
        S.mark_uploaded(self.worker,body['lease']['id'],first['manifest_sha256'],{'id':42})
        with patch.object(B.Fake,'get',side_effect=AssertionError('duplicate_fetch')):result=self.execute(body)
        self.assertEqual(result['status'],'uploaded');self.assertEqual(result['upload'],{'id':42})
    def test_tampered_checkpoint_rejected_before_delivery(self):
        body=self.issue();result=self.execute(body)
        path=Path(result['checkpoint_path'])/'body-00.bin';path.write_bytes(path.read_bytes()+b'x')
        with self.assertRaisesRegex(R.Rejected,'body_size_or_path'):self.execute(body)
    def test_only_one_active_lease(self):
        self.issue();before=self.path.read_bytes()
        with self.assertRaisesRegex(R.Rejected,'lease_in_flight'):self.issue(2)
        self.assertEqual(self.path.read_bytes(),before)
    def test_abandoned_delivery_cannot_change_state(self):
        body=self.issue();result=self.execute(body)
        S.abandon(self.path,body['lease']['id'],'delivery_timeout');before=self.path.read_bytes()
        with self.assertRaisesRegex(R.Rejected,'lease_abandoned'):self.accept(result)
        self.assertEqual(self.path.read_bytes(),before)
        second=self.issue(2);self.assertEqual(second['state'],self.state)
    def test_worker_lock_excludes_second_acquisition(self):
        body=self.issue();self.worker.mkdir()
        with R.writer_lock(self.worker/'.claims.lock'):
            with self.assertRaisesRegex(R.Rejected,'receiver_busy'):self.execute(body)
        self.assertFalse((self.worker/body['lease']['id']).exists())
    def test_forged_input_rejected(self):
        body=self.issue();p=self.root/'input-1/request.json';body['state']['queue']={};p.write_bytes(R.encode(body))
        with self.assertRaisesRegex(R.Rejected,'controller_signature_invalid'):S.read_input(p.parent,self.session,self.config,now=B.NOW)
    def test_worker_key_cannot_sign_input(self):
        self.issue();root=self.root/'input-1';(root/'signature.bin').write_bytes(self.key.sign(S.INPUT_DOMAIN+(root/'request.json').read_bytes()))
        with self.assertRaisesRegex(R.Rejected,'controller_signature_invalid'):S.read_input(root,self.session,self.config,now=B.NOW)
    def test_wrong_session_rejected(self):
        self.issue();session={**self.session,'session_id':'f'*32}
        with self.assertRaisesRegex(R.Rejected,'input_session'):S.read_input(self.root/'input-1',session,self.config,now=B.NOW)
    def test_expired_input_rejected(self):
        self.issue()
        with self.assertRaisesRegex(R.Rejected,'input_expired'):S.read_input(self.root/'input-1',self.session,self.config,now=B.NOW+timedelta(minutes=10))
    def test_unapproved_source_rejected(self):
        with self.assertRaisesRegex(R.Rejected,'source_not_approved'):S.validate_session(self.session,{**self.policy,'source_sha':'d'*40},now=B.NOW)
    def test_session_validates_runtime_and_expiry(self):
        S.validate_session(self.session,self.policy,now=B.NOW)
        with self.assertRaisesRegex(R.Rejected,'session_expired'):S.validate_session(self.session,self.policy,now=B.NOW+timedelta(hours=1))
        with self.assertRaisesRegex(R.Rejected,'source_runtime_mismatch'):S.validate_session(self.session,{**self.policy,'runtime_sha256':'0'*64},now=B.NOW)
    def test_archive_path_rejected_before_writes(self):
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as archive:
            archive.writestr('../request.json',b'{}');archive.writestr('signature.bin',b'0'*64)
        with self.assertRaisesRegex(R.Rejected,'input_archive_members'):S.extract_input(buf.getvalue(),self.root/'unpack')
        self.assertFalse((self.root/'unpack').exists())
    def test_export_verifies_and_repeated_import_is_idempotent(self):
        result=self.execute(self.issue());self.accept(result);self.export()
        self.assertEqual(self.verify()['status'],'verified')
        self.assertEqual(self.verify(current=self.final)['status'],'already_current')
    def test_state_changed_after_collection_is_not_overwritten(self):
        result=self.execute(self.issue());self.accept(result);self.export();current=deepcopy(self.state)
        current['queue']['0']['attempts']+=1
        with self.assertRaisesRegex(R.Rejected,'native_state_conflict'):self.verify(current=current)
    def test_worker_cannot_sign_export(self):
        self.export();p=self.incoming/'native/yahoo.json'
        (self.incoming/'native/yahoo.sig').write_bytes(self.key.sign(S.EXPORT_DOMAIN+p.read_bytes()))
        with self.assertRaisesRegex(R.Rejected,'controller_signature_invalid'):self.verify()
    def test_raw_state_tamper_cannot_bypass_export(self):
        self.export();self.final['queue']={}
        with self.assertRaisesRegex(R.Rejected,'export_binding'):self.verify()
    def test_export_wrong_run_source_or_base_rejected(self):
        self.export()
        for values in ({'run_id':'123456-2'},{'source_sha':'d'*40},{'data_sha':'e'*40}):
            with self.subTest(values=values),self.assertRaisesRegex(R.Rejected,'export_binding'):self.verify(**values)
    def test_unreceived_work_makes_total_unknown(self):
        self.issue();self.export()
        self.assertIsNone(self.final['request_count'])
        self.assertEqual(self.final['native_collection']['received_request_count'],0)
        self.assertFalse(self.final['native_collection']['request_count_complete'])
        self.assertEqual(self.final['queue'],self.state['queue'])
    def test_isolated_scope_cannot_publish_collection_state(self):
        value=self.load();value['scope']=R.SCOPE;self.path.write_bytes(R.encode(value))
        self.session['scope']=R.SCOPE;self.export()
        with self.assertRaisesRegex(R.Rejected,'export_binding'):self.verify()
    def test_checkpoint_requires_trust_even_when_worker_omits_native_flag(self):
        self.export();del self.final['native_collection']
        atomic_json(self.incoming/'stores/yahoo.json',self.final)
        state_root=self.root/'state';atomic_json(state_root/'stores/yahoo.json',self.state)
        before=(state_root/'stores/yahoo.json').read_bytes()
        result=checkpoint(state_root,self.incoming,self.run,native_required=True)
        self.assertEqual(result['rejected_stores'],[{'store':'yahoo','reason':'native_export_unverified'}])
        self.assertEqual((state_root/'stores/yahoo.json').read_bytes(),before)
    def test_checkpoint_commits_verified_export_and_keeps_other_stores(self):
        self.accept(self.execute(self.issue()));self.export()
        state_root=self.root/'state';atomic_json(state_root/'stores/yahoo.json',self.state)
        atomic_json(state_root/'stores/ark.json',{'manual':'preserve'})
        result=checkpoint(state_root,self.incoming,self.run,native_required=True,native_trust=self.trust,source_sha='a'*40,data_sha='b'*40)
        self.assertEqual(result['accepted_stores'],['yahoo'])
        self.assertEqual(json.loads((state_root/'stores/yahoo.json').read_bytes()),self.final)
        self.assertEqual(json.loads((state_root/'stores/ark.json').read_bytes()),{'manual':'preserve'})
    def test_partial_prefix_keeps_received_count_but_marks_total_unknown(self):
        body=self.issue()
        def crash(stage):
            if stage=='after_checkpoint':raise Crash()
        with self.assertRaises(Crash):self.execute(body,hook=crash)
        self.accept(self.execute(body));self.export()
        self.assertIsNone(self.final['request_count'])
        self.assertEqual(self.final['native_collection']['received_request_count'],1)
        self.assertEqual(self.final['native_collection']['partial_batches'],1)
    def test_completion_must_be_signed_by_controller_for_this_session(self):
        root=self.root/'completion';S.finish_session(root,self.session,self.cloud)
        self.assertEqual(S.read_completion(root,self.session,now=B.NOW)['session_id'],self.session['session_id'])
        with self.assertRaisesRegex(R.Rejected,'completion_session'):S.read_completion(root,{**self.session,'session_id':'f'*32},now=B.NOW)
        (root/'signature.bin').write_bytes(self.key.sign(S.END_DOMAIN+(root/'request.json').read_bytes()))
        with self.assertRaisesRegex(R.Rejected,'controller_signature_invalid'):S.read_completion(root,self.session,now=B.NOW)
    def test_killed_worker_restarts_from_durable_prefix_without_fetching(self):
        body=self.issue();payload=self.root/'child-input.json';script=self.root/'child.py';marker=self.root/'ready'
        # A newly generated synthetic test key, never a registered worker key.
        payload.write_bytes(R.encode({'body':body,'config':self.config,'key':self.key.private_bytes_raw().hex(),'now':iso(B.NOW)}))
        script.write_text('''from pathlib import Path
import json,sys,time
from unittest.mock import patch
sys.path.insert(0,str(Path(sys.argv[1]).parent))
sys.path.insert(0,sys.argv[1])
import test_receipts as B
import test_yahoo_public as Y
from sale_monitor import native_sessions as S,receipts as R
from sale_monitor.models import iso
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
p=json.loads(Path(sys.argv[2]).read_bytes());B.NOW=R.moment(p['now'])
pages={}
for task in p['body']['lease']['tasks'].values():
    query=task['query'];data=json.loads(json.dumps(Y.zero_search_data(),ensure_ascii=False).replace(Y.QUERY,query))
    data['props']['initialState']['bff']['searchResultHeader']['title']['main']['taxonomies'][0]['items'][0]['url']=Y.search_url(query)
    pages[Y.search_url(query)]=Y.page(data,Y.search_url(query))
def checkpoint(stage):
    if stage=='after_checkpoint' and sys.argv[5]=='first':
        Path(sys.argv[4]).write_text('checkpoint_saved')
        while True:time.sleep(1)
def client():
    if sys.argv[5]!='first':raise AssertionError('restart_must_not_fetch')
    return B.Fake(pages)
with patch('socket.create_connection',side_effect=AssertionError('network_forbidden')),patch.object(R,'utcnow',return_value=B.NOW),patch.object(R,'iso',side_effect=lambda t=None:iso(t or B.NOW)),patch('sale_monitor.runner.iso',return_value=iso(B.NOW)),patch('sale_monitor.runner.utcnow',return_value=B.NOW),patch('sale_monitor.http_evidence.iso',return_value=iso(B.NOW)):
    result=S.execute_claim(Path(sys.argv[3]),p['body'],p['config'],Ed25519PrivateKey.from_private_bytes(bytes.fromhex(p['key'])),client_factory=client,hook=checkpoint,now=B.NOW)
    print(json.dumps(result))
''',encoding='utf-8')
        command=[sys.executable,'-B',str(script),str(Path(__file__).parent),str(payload),str(self.worker),str(marker)]
        flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        process=subprocess.Popen([*command,'first'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=flags)
        try:
            end=time.monotonic()+15
            while not marker.exists() and process.poll() is None and time.monotonic()<end:time.sleep(0.05)
            if not marker.exists() and process.poll() is not None:
                stdout,stderr=process.communicate(timeout=5)
                self.fail('worker exited before checkpoint: '+stderr.decode(errors='replace')+' '+stdout.decode(errors='replace'))
            self.assertTrue(marker.exists(),'worker did not reach durable checkpoint')
            # Terminate only the exact child object created by this test.
            process.terminate();process.communicate(timeout=10)
            resumed=subprocess.run([*command,'resume'],capture_output=True,text=True,timeout=15,creationflags=flags)
            self.assertEqual(resumed.returncode,0,resumed.stderr)
            result=json.loads(resumed.stdout);receipt=self.accept(result)
            self.assertEqual((receipt['successes'],receipt['unattempted'],receipt['requests']),(1,3,1))
            self.assertTrue(result['resumed'])
        finally:
            if process.poll() is None:process.terminate();process.communicate(timeout=10)
            for stream in (process.stdout,process.stderr):
                if stream:stream.close()


if __name__=='__main__':unittest.main()

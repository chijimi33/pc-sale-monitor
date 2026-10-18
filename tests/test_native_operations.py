"""Publisher artifact identity and trusted-mailbox ancestry boundaries."""
from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sale_monitor import receipts as R, native_operations as O, native_sessions as S, native_channel as N
from sale_monitor.storage import atomic_json
from sale_monitor.models import STORES

REPO=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('native_ids',REPO/'scripts/native_artifact_ids.py')
IDS=importlib.util.module_from_spec(spec);spec.loader.exec_module(IDS)


class ArtifactIds(unittest.TestCase):
    def test_parallel_outputs_select_ten_distinct_ids(self):
        regular={store:str(index+1) for index,store in enumerate(STORES) if store!='yahoo'}
        self.assertEqual(len(IDS.selected(regular,{'yahoo':'999'})),10)
    def test_missing_jobs_never_fall_back_to_name_or_all_artifacts(self):
        self.assertEqual(IDS.selected({},{}),[])
        self.assertEqual(IDS.selected({'ark':'123','tsukumo':''},{}),['123'])
    def test_wrong_store_id_type_and_reused_id_rejected(self):
        for regular,yahoo in [({'yahoo':'1'},{}),({'unknown':'1'},{}),({'ark':1},{}),({'ark':'1,2'},{}),({'ark':'1'},{'yahoo':'1'})]:
            with self.subTest(regular=regular,yahoo=yahoo),self.assertRaises(ValueError):IDS.selected(regular,yahoo)


class Operations(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='native-operations-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.args=SimpleNamespace(root=self.root/'session',state=self.root/'state-repo',outgoing=self.root/'outgoing',
            config=self.root/'sources.json',worker_config=self.root/'worker.json',controller_key=self.root/'private.key',
            isolated=False,seconds=600,batches=3,incoming=self.root/'incoming',trust=self.root/'trust.json')
        self.config={'stores':{'yahoo':{'adapter':'yahoo','public_html_fallback':True,'native_collection':True,'queries':[]}}}
        self.state={'schema_version':1,'store':'yahoo','run_id':'old','queue':{'manual':{'type':'yahoo','query':'old','start':1,
            'created_at':'2026-01-01T00:00:00+00:00','attempts':5}},'done':['prior'],'offers':{},'journal':[{'prior':True}]}
        atomic_json(self.args.config,self.config)
        atomic_json(self.args.worker_config,{'worker_id':N.WORKER,'enabled':True,'stores':['yahoo'],'exchange_key_hex':'1'*64,'public_key_hex':'2'*64})
        atomic_json(self.args.state/'state/stores/yahoo.json',self.state)
        atomic_json(self.args.state/'state/requests/yahoo.json',[{'query':'requested-product','identity':'jan:123','priority':1}])
        atomic_json(self.args.state/'state/requests/candidate_identities.json',[])
        self.enterContext(patch.dict(os.environ,{'GITHUB_REPOSITORY':N.REPOSITORY,'SOURCE_COMMIT':'a'*40,'GITHUB_RUN_ID':'1234',
            'GITHUB_RUN_ATTEMPT':'1','YAHOO_CLIENT_ID':''}))
        self.enterContext(patch('socket.create_connection',side_effect=AssertionError('network_forbidden')))
    def prepare(self):
        with patch.object(O,'git',return_value=b'b'*40),patch.object(N,'publish_packet',return_value='c'*40),\
             patch.object(N,'seal',side_effect=lambda public,credentials,metadata:{'metadata':metadata,'encrypted':True}),\
             patch('sys.stdin',io.StringIO('{"runtime_token":"synthetic","results_url":"https://example.actions.githubusercontent.com"}')):
            return O.prepare(self.args)
    def test_seed_reads_requests_and_preserves_manual_queue_and_journal(self):
        self.prepare();receiver=O.load(self.args.root/'receiver.json');state=receiver['state']
        self.assertEqual(state['queue']['manual'],self.state['queue']['manual']);self.assertEqual(state['journal'],self.state['journal'])
        self.assertTrue(any(t.get('query')=='requested-product' for t in state['queue'].values()))
        self.assertEqual(O.load(self.args.root/'control.json')['initial_state_sha256'],R.sha(R.encode(self.state)))
        self.assertEqual(len(self.args.controller_key.read_bytes()),32)
        self.assertFalse(any(p.name.endswith('.key') for p in self.args.root.rglob('*')))
    def test_controller_key_cannot_be_under_an_uploaded_root(self):
        self.args.controller_key=self.args.root/'leak.key'
        with self.assertRaisesRegex(R.Rejected,'private_key_inside_artifacts'):self.prepare()
        self.assertFalse(self.args.controller_key.exists())
    def test_disabled_native_collection_cannot_prepare(self):
        self.config['stores']['yahoo']['native_collection']=False;atomic_json(self.args.config,self.config)
        with self.assertRaisesRegex(R.Rejected,'native_collection_disabled'):self.prepare()
    def test_issued_input_contains_updated_controller_state_and_only_public_files(self):
        self.prepare();result=O.next_batch(self.args)
        self.assertEqual(result['status'],'issued')
        self.assertEqual({p.name for p in Path(result['directory']).iterdir()},{'request.json','signature.bin'})
        session=O.load(self.args.root/'control.json')['session']
        body=S.read_input(Path(result['directory']),session,self.config)
        self.assertEqual(body['state'],O.load(self.args.root/'receiver.json')['state'])
        final=O.load(self.args.outgoing/'stores/yahoo.json')
        self.assertIsNone(final['request_count']);self.assertEqual(final['native_collection']['unreceived_batches'],1)
    def test_trust_requires_successful_ancestor_check_before_reading_key(self):
        atomic_json(self.args.incoming/'native/yahoo.json',{'mailbox_commit':'c'*40})
        calls=[]
        def fake(cwd,*args):
            calls.append(args)
            if args[0]=='merge-base':raise R.Rejected('trusted_git_failed')
            return b''
        with patch.object(O,'git',side_effect=fake),self.assertRaisesRegex(R.Rejected,'trusted_git_failed'):O.trust(self.args)
        self.assertEqual([x[0] for x in calls],['fetch','merge-base']);self.assertFalse(self.args.trust.exists())
    def test_trust_rejects_arbitrary_commit_argument_before_git(self):
        atomic_json(self.args.incoming/'native/yahoo.json',{'mailbox_commit':'--output=/tmp/injected'})
        with patch.object(O,'git',side_effect=AssertionError('must_not_run')),self.assertRaisesRegex(R.Rejected,'mailbox_commit'):O.trust(self.args)
    def test_real_git_history_accepts_ancestor_and_rejects_unrelated_commit(self):
        # This entire Git graph is disposable and permits file transport only.
        with patch.dict(os.environ,{'GIT_ALLOW_PROTOCOL':'file','GIT_TERMINAL_PROMPT':'0',
                                  'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':str(self.root/'empty-config')}):
            remote=self.root/'remote.git';source=self.root/'mailbox';source.mkdir()
            O.git(self.root,'init','--bare',str(remote))
            O.git(source,'init','-b',N.MAILBOX_BRANCH)
            O.git(source,'config','user.name','Fixture');O.git(source,'config','user.email','fixture@example.invalid')
            metadata=S.session_metadata(self.config,'a'*40,'b'*40,'1234-1',Ed25519PrivateKey.generate())
            packet={'metadata':metadata,'ephemeral':'synthetic','nonce':'synthetic','ciphertext':'synthetic'}
            atomic_json(source/N.MAILBOX_PATH,packet)
            O.git(source,'add','.');O.git(source,'commit','-m','trusted-bootstrap')
            trusted=O.git(source,'rev-parse','HEAD').decode().strip()
            O.git(source,'remote','add','origin',str(remote));O.git(source,'push','origin',N.MAILBOX_BRANCH)
            O.git(self.args.state,'init');O.git(self.args.state,'remote','add','origin',str(remote))
            atomic_json(self.args.incoming/'native/yahoo.json',{'mailbox_commit':trusted})
            O.trust(self.args);self.assertEqual(O.load(self.args.trust)['metadata'],metadata)
            O.git(source,'checkout','--orphan','unrelated-object')
            packet['metadata']['controller_key_hex']='f'*64;atomic_json(source/N.MAILBOX_PATH,packet)
            O.git(source,'add','.');O.git(source,'commit','-m','unrelated-key')
            forged=O.git(source,'rev-parse','HEAD').decode().strip()
            O.git(source,'push','origin','unrelated-object');O.git(self.args.state,'fetch','origin','unrelated-object')
            atomic_json(self.args.incoming/'native/yahoo.json',{'mailbox_commit':forged})
            before=self.args.trust.read_bytes()
            with self.assertRaisesRegex(R.Rejected,'trusted_git_failed'):O.trust(self.args)
            self.assertEqual(self.args.trust.read_bytes(),before)


if __name__=='__main__':unittest.main()

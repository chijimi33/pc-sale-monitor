"""Boundaries for encrypted transport, local approval and safe archive intake."""
from copy import deepcopy
from datetime import timedelta
import io
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from sale_monitor import native_channel as N, receipts as R
from sale_monitor.models import utcnow, iso


class Channel(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('socket.create_connection',side_effect=AssertionError('network_forbidden')))
        self.private = X25519PrivateKey.generate()
        self.public = self.private.public_key().public_bytes_raw().hex()
        self.now = utcnow()
        self.policy = {'enabled':True,'source_sha':'a'*40,'runtime_sha256':R.runtime_hash()}
        lease = {'id':'f'*32,'worker_id':N.WORKER,'store':'yahoo','scope':R.SCOPE,
                 'source_sha':'a'*40,'data_sha':'b'*40,'runtime_sha256':R.runtime_hash(),
                 'run_id':'isolated-native-github-123-1','created_at':iso(self.now),
                 'expires_at':iso(self.now+timedelta(minutes=10)),
                 'task_order':['task'], 'tasks':{'task':{'type':'yahoo','query':'SSD','start':1}}}
        self.metadata = {'version':1,'repository':N.REPOSITORY,'worker_id':N.WORKER,
                         'source_sha':'a'*40,'data_sha':'b'*40,'runtime_sha256':R.runtime_hash(),'lease':lease}
        self.credentials = {'runtime_token':'synthetic.secret.value','results_url':'https://results-receiver.actions.githubusercontent.com/'}
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def packet(self):
        return N.seal(self.public,self.credentials,self.metadata)

    def open(self,packet=None,**kwargs):
        return N.open_packet(packet or self.packet(),self.private,self.policy,now=kwargs.pop('now',self.now),**kwargs)

    def test_credentials_encrypted_and_survive_valid_round_trip(self):
        packet = self.packet()
        self.assertNotIn(self.credentials['runtime_token'].encode(),R.encode(packet))
        self.assertEqual(self.open(packet),(self.metadata,self.credentials))
        self.assertNotEqual(self.packet()['ciphertext'],packet['ciphertext'])

    def test_wrong_private_key_cannot_decrypt(self):
        with self.assertRaisesRegex(R.Rejected,'encrypted_packet_invalid'):
            N.open_packet(self.packet(),X25519PrivateKey.generate(),self.policy,now=self.now)

    def test_mutated_ciphertext_is_rejected(self):
        packet = self.packet()
        packet['ciphertext'] = ('A' if packet['ciphertext'][0]!='A' else 'B')+packet['ciphertext'][1:]
        with self.assertRaisesRegex(R.Rejected,'encrypted_packet_invalid'):self.open(packet)

    def test_metadata_is_authenticated(self):
        packet = self.packet()
        packet['metadata']['lease']['tasks']['task']['query']='GPU'
        with self.assertRaisesRegex(R.Rejected,'encrypted_packet_invalid'):self.open(packet)

    def test_unapproved_source_disabled_worker_and_runtime_are_rejected(self):
        for field,value,reason in [('source_sha','c'*40,'source_not_approved'),('enabled',False,'source_not_approved'),('runtime_sha256','0'*64,'source_runtime_mismatch')]:
            with self.subTest(field=field):
                before = deepcopy(self.policy)
                self.policy[field]=value
                with self.assertRaisesRegex(R.Rejected,reason):self.open()
                self.policy=before

    def test_expired_future_and_excessive_lease_are_rejected(self):
        for now in [self.now-timedelta(seconds=1),self.now+timedelta(minutes=11)]:
            with self.assertRaisesRegex(R.Rejected,'lease_expired'):self.open(now=now)
        self.metadata['lease']['expires_at']=iso(self.now+timedelta(minutes=11))
        with self.assertRaisesRegex(R.Rejected,'lease_expired'):self.open()

    def test_production_run_and_wrong_repository_are_rejected(self):
        self.metadata['lease']['run_id']='123-1'
        with self.assertRaisesRegex(R.Rejected,'isolated_github_run_required'):self.open()
        self.metadata['repository']='attacker/repo'
        with self.assertRaisesRegex(R.Rejected,'packet_scope'):self.open()

    def test_foreign_url_and_legacy_api_offset_are_rejected(self):
        for task in [{'type':'product','url':'https://example.com/item'},{'type':'yahoo','query':'SSD','start':51}]:
            self.metadata['lease']['tasks']['task']=task
            with self.assertRaisesRegex(R.Rejected,'unsupported_task'):self.open()

    def test_credentials_cannot_be_directed_to_another_host(self):
        for url in ['http://results-receiver.actions.githubusercontent.com/',
                    'https://results-receiver.actions.githubusercontent.com.evil.test/',
                    'https://evil.test/','https://u:p@results-receiver.actions.githubusercontent.com/',
                    'https://results-receiver.actions.githubusercontent.com:444/',
                    'https://results-receiver.actions.githubusercontent.com/?secret=value']:
            with self.subTest(url=url):
                self.credentials['results_url']=url
                with self.assertRaisesRegex(R.Rejected,'artifact_service_host'):self.open()

    def test_token_not_sent_to_foreign_http_host(self):
        with self.assertRaisesRegex(R.Rejected,'credential_host'):
            N.request('https://example.com/',token='synthetic')

    def test_mailbox_reads_latest_ref_then_immutable_packet(self):
        packet=self.packet()
        with patch.object(N,'request',side_effect=[R.encode({'object':{'type':'commit','sha':'c'*40}}),R.encode(packet)]) as fetch:
            self.assertEqual(N.mailbox_packet(),packet)
        self.assertEqual(fetch.call_args_list[0].args,(f'https://api.github.com/repos/{N.REPOSITORY}/git/ref/heads/{N.MAILBOX_BRANCH}',))
        self.assertEqual(fetch.call_args_list[1].args,(f'https://raw.githubusercontent.com/{N.REPOSITORY}/{"c"*40}/{N.MAILBOX_PATH}',))

    def test_malformed_ref_never_builds_an_arbitrary_download_url(self):
        for value in ['../other','a'*39,'A'*40,'https://example.com/']:
            with patch.object(N,'request',return_value=R.encode({'object':{'type':'commit','sha':value}})) as fetch:
                with self.assertRaisesRegex(R.Rejected,'mailbox_ref'):N.mailbox_packet()
            self.assertEqual(fetch.call_count,1)

    def test_extra_secret_fields_and_whitespace_token_rejected(self):
        self.credentials['repository_token']='synthetic'
        with self.assertRaisesRegex(R.Rejected,'credential_schema'):self.open()
        self.credentials.pop('repository_token')
        self.credentials['runtime_token']='header\ninjection'
        with self.assertRaisesRegex(R.Rejected,'credential_format'):self.open()

    def archive(self,extra=None):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:
            archive.writestr('manifest.json',b'{}')
            archive.writestr('signature.bin',b'x'*64)
            if extra is not None:archive.writestr(*extra)
        return stream.getvalue()

    def test_archive_accepts_only_bounded_receipt_files(self):
        N.safe_extract(self.archive(('body-00.bin',b'response')),self.root/'packet')
        self.assertEqual((self.root/'packet/body-00.bin').read_bytes(),b'response')

    def test_archive_traversal_rejected_before_any_output(self):
        for name in ['../outside','/outside','C:/outside','nested/body-00.bin','body-00.bin/','extra.txt']:
            with self.subTest(name=name):
                with self.assertRaisesRegex(R.Rejected,'archive_path'):
                    N.safe_extract(self.archive((name,b'x')),self.root/'packet')
                self.assertFalse((self.root/'packet').exists())

    def test_archive_symlink_rejected(self):
        link=zipfile.ZipInfo('body-00.bin');link.create_system=3;link.external_attr=0o120777<<16
        with self.assertRaisesRegex(R.Rejected,'archive_file_type'):
            N.safe_extract(self.archive((link,b'target')),self.root/'packet')
        self.assertFalse((self.root/'packet').exists())

    def test_archive_oversize_body_rejected_before_output(self):
        with self.assertRaisesRegex(R.Rejected,'archive_entry_size'):
            N.safe_extract(self.archive(('body-00.bin',b'x'*2_000_001)),self.root/'packet')
        self.assertFalse((self.root/'packet').exists())

    def test_archive_duplicate_rejected(self):
        with self.assertWarns(UserWarning):raw=self.archive(('manifest.json',b'{}'))
        with self.assertRaisesRegex(R.Rejected,'archive_members'):
            N.safe_extract(raw,self.root/'packet')

    @unittest.skipUnless(os.name=='nt','Windows DPAPI')
    def test_persistent_keys_are_protected_and_can_be_reloaded(self):
        path=self.root/'worker.dpapi'
        public=N.generate_keys(path)
        signing,exchange=N.load_keys(path)
        self.assertEqual(signing.public_key().public_bytes_raw().hex(),public['public_key_hex'])
        self.assertEqual(exchange.public_key().public_bytes_raw().hex(),public['exchange_key_hex'])
        self.assertNotIn(signing.private_bytes_raw().hex().encode(),path.read_bytes())
        self.assertNotIn(exchange.private_bytes_raw().hex().encode(),path.read_bytes())
        with self.assertRaisesRegex(R.Rejected,'key_already_exists'):N.generate_keys(path)


if __name__=='__main__':unittest.main()

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sale_monitor import storage
from sale_monitor.storage import Store, atomic_json


def checkpoint():
    return {'run_id': 'r1', 'store': 'tsukumo', 'queue': {
        'first': {'created_at': '2026-10-01T00:00:00+00:00', 'attempts': 7, 'title': '旧価格・送料 ¥1,234'}},
        'offers': {'old': {'price_yen': 1234, 'observed_run_id': 'previous', 'shipping_yen': None}},
        'journal': [{'observation_id': 'one'}, {'observation_id': 'two'}],
        'waiting_dependencies': [{'until_epoch_seconds': 1791641000.125}],
        'cycle_complete': False}


class CompactCheckpointTests(unittest.TestCase):
    def test_existing_pretty_checkpoint_round_trips_every_value(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); path=root/'stores/tsukumo.json'; old=checkpoint()
            atomic_json(path,old)
            old_size=path.stat().st_size
            Store(root).save('stores/tsukumo.json',Store(root).load('stores/tsukumo.json',{}))
            self.assertEqual(Store(root).load('stores/tsukumo.json',{}),old)
            self.assertLess(path.stat().st_size,old_size)
            self.assertEqual(old['journal'],[{'observation_id':'one'},{'observation_id':'two'}])

    def test_public_and_audit_files_keep_existing_representation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); value=checkpoint(); expected=json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n'
            for name in ['public/latest.json','validation/manual_review.json','events/registry.json']:
                Store(root).save(name,value)
                self.assertEqual((root/name).read_text(encoding='utf-8'),expected)

    def test_commits_only_after_fsync_of_complete_new_document(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'; original=checkpoint(); atomic_json(path,original)
            changed=deepcopy(original); changed['queue']['first']['attempts']=8
            order=[]; real_sync=storage.os.fsync; real_replace=storage.os.replace
            def sync(fd):
                order.append('sync'); real_sync(fd)
                self.assertEqual(json.loads(path.read_text(encoding='utf-8')),original)
            def replace(src,dst):
                self.assertEqual(order,['sync'])
                self.assertEqual(json.loads(src.read_text(encoding='utf-8')),changed)
                order.append('replace');real_replace(src,dst)
            with patch('sale_monitor.storage.os.fsync',side_effect=sync),patch('sale_monitor.storage.os.replace',side_effect=replace):
                atomic_json(path,changed,compact=True)
            self.assertEqual(order,['sync','replace'])
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')),changed)

    def test_fsync_failure_retains_previous_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json';original=checkpoint();atomic_json(path,original)
            before=path.read_bytes()
            with patch('sale_monitor.storage.os.fsync',side_effect=OSError('disk failure')),patch('sale_monitor.storage.os.replace') as replace:
                with self.assertRaises(OSError):atomic_json(path,{'new':True},compact=True)
            replace.assert_not_called()
            self.assertEqual(path.read_bytes(),before)

    def test_replace_failure_retains_previous_checkpoint_and_next_save_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json';original=checkpoint();atomic_json(path,original)
            before=path.read_bytes()
            with patch('sale_monitor.storage.os.replace',side_effect=PermissionError('interrupted replacement')):
                with self.assertRaises(PermissionError):atomic_json(path,{'new':True},compact=True)
            self.assertEqual(path.read_bytes(),before)
            atomic_json(path,original,compact=True)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')),original)

    def test_partial_write_interruption_does_not_replace_or_lose_original_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'stores/tsukumo.json';original=checkpoint();Store(root).save('stores/tsukumo.json',original)
            before=path.read_bytes(); real_open=Path.open
            class Interrupted:
                def __init__(self,handle):self.handle=handle
                def __enter__(self):return self
                def __exit__(self,*args):self.handle.close()
                def write(self,text):
                    self.handle.write(text[:23]);raise KeyboardInterrupt
            def open_file(target,*args,**kwargs):
                handle=real_open(target,*args,**kwargs)
                return Interrupted(handle) if target.suffix=='.tmp' and args and args[0]=='w' else handle
            with patch.object(Path,'open',open_file),patch('sale_monitor.storage.os.replace') as replace:
                with self.assertRaises(KeyboardInterrupt):Store(root).save('stores/tsukumo.json',{'new':True,'long':'x'*100})
            replace.assert_not_called()
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(Store(root).load('stores/tsukumo.json',{}),original)
            Store(root).save('stores/tsukumo.json',original)
            self.assertEqual(Store(root).load('stores/tsukumo.json',{}),original)

    def test_serialization_error_leaves_committed_state_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json';atomic_json(path,checkpoint(),compact=True);before=path.read_bytes()
            with self.assertRaises(TypeError):atomic_json(path,{'bad':object()},compact=True)
            self.assertEqual(path.read_bytes(),before)


if __name__=='__main__':
    unittest.main()

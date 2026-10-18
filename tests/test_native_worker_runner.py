from pathlib import Path
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

REPO=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('native_worker_runner',REPO/'scripts/native_worker_runner.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)


class NativeInstallation(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'source';self.source.mkdir()
        self.files={
            'scripts/native_worker_runner.py':(REPO/'scripts/native_worker_runner.py').read_bytes(),
            'scripts/qa/process_guard.py':(REPO/'scripts/qa/process_guard.py').read_bytes(),
            'tools/native-channel/session.mjs':b'// offline fixture\n',
            'sale_monitor/__init__.py':b'',
            'sale_monitor/native_operations.py':b'import time\nfrom pathlib import Path\ndef main():\n Path("started.txt").write_text(str(__import__("os").getpid()))\n time.sleep(60)\n'}
        for name,raw in self.files.items():
            target=self.source/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
        digest=runner.sha(json.dumps({Path(p).name:runner.sha(b) for p,b in self.files.items() if p.startswith('sale_monitor/')},sort_keys=True,separators=(',',':')).encode())
        self.policy={'enabled':True,'source_sha':'a'*40,'runtime_sha256':digest}
        (self.root/'policy.json').write_text(json.dumps(self.policy))
        (self.root/'key.dpapi').write_bytes(b'offline fixture; never loaded')
        self.manifest={'commit':'a'*40,'files':[{'path':p,'sha256':runner.sha(b)} for p,b in self.files.items()]}
        (self.root/'manifest.json').write_text(json.dumps(self.manifest))
        self.config={'schema_version':1,'source_sha':'a'*40,'root':str(self.root),'source':str(self.source),
                     'python':str(Path(sys.executable).resolve()),'node':str(Path(sys.executable).resolve()),'sdk':str(self.source/'tools/native-channel/session.mjs'),
                     'key':str(self.root/'key.dpapi'),'policy':str(self.root/'policy.json'),'manifest':str(self.root/'manifest.json')}
        self.path=self.root/'installation.json';self.path.write_text(json.dumps(self.config))

    def test_exact_source_and_policy_validate_without_key_access(self):
        self.assertEqual(runner.validate(self.path)[0],self.config)
        self.assertEqual((self.root/'key.dpapi').read_bytes(),b'offline fixture; never loaded')

    def test_changed_or_extra_runtime_file_stops_before_execution(self):
        (self.source/'sale_monitor/extra.py').write_text('unexpected=True')
        with self.assertRaisesRegex(ValueError,'unapproved_runtime_file'):runner.validate(self.path)
        (self.source/'sale_monitor/extra.py').unlink()
        (self.source/'sale_monitor/native_operations.py').write_text('changed=True')
        with self.assertRaisesRegex(ValueError,'source_bytes_changed'):runner.validate(self.path)
        self.assertFalse((self.source/'started.txt').exists())

    def test_wrong_source_pin_or_sdk_path_is_rejected(self):
        self.config['source_sha']='b'*40;self.path.write_text(json.dumps(self.config))
        with self.assertRaisesRegex(ValueError,'source_policy_mismatch'):runner.validate(self.path)
        self.config['source_sha']='a'*40;self.config['sdk']=str(self.root/'key.dpapi');self.path.write_text(json.dumps(self.config))
        with self.assertRaisesRegex(ValueError,'sdk_path_mismatch'):runner.validate(self.path)

    def test_traversal_and_source_outside_installation_are_rejected(self):
        self.manifest['files'].append({'path':'../key.dpapi','sha256':runner.sha((self.root/'key.dpapi').read_bytes())})
        (self.root/'manifest.json').write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError,'manifest_path'):runner.validate(self.path)
        self.config['source']=str(self.root.parent);self.path.write_text(json.dumps(self.config))
        with self.assertRaisesRegex(ValueError,'source_outside_installation'):runner.validate(self.path)

    @unittest.skipUnless(os.name=='nt','Windows process ownership')
    def test_owned_worker_ends_when_its_launcher_is_killed(self):
        process=subprocess.Popen([sys.executable,'-B',str(self.source/'scripts/native_worker_runner.py'),'--config',str(self.path)],
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+15
            while not (self.source/'started.txt').exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.05)
            self.assertIsNone(process.poll())
            self.assertTrue((self.source/'started.txt').exists())
            pid=int((self.source/'started.txt').read_text())
            import ctypes
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong];kernel.OpenProcess.restype=ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_ulong];kernel.WaitForSingleObject.restype=ctypes.c_ulong
            kernel.CloseHandle.argtypes=[ctypes.c_void_p]
            handle=kernel.OpenProcess(0x100000,False,pid);self.assertTrue(handle)
            try:
                process.kill();process.wait(timeout=5)
                self.assertEqual(kernel.WaitForSingleObject(handle,5000),0)
            finally:kernel.CloseHandle(handle)
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=5)
            process.communicate()


if __name__=='__main__':unittest.main()

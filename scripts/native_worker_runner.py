"""Run only a locally pinned native worker and contain its owned child processes."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
import uuid

START_FREE_BYTES=40*1024**3
STOP_FREE_BYTES=8*1024**3
STORAGE_CHECK_SECONDS=10


def sha(raw):return hashlib.sha256(raw).hexdigest()


def load(path):return json.loads(Path(path).read_bytes())


def validate(path):
    value=load(path)
    if value.get('schema_version')!=1:raise ValueError('installation_schema')
    paths={key:Path(value[key]) for key in ('root','source','python','node','sdk','key','policy','manifest')}
    if any(not p.is_absolute() or p.is_symlink() for p in paths.values()):raise ValueError('installation_path')
    root=paths['root'].resolve();source=paths['source'].resolve()
    if source==root or not source.is_relative_to(root):raise ValueError('source_outside_installation')
    for key in ('sdk','policy','manifest'):
        if not paths[key].resolve().is_relative_to(root):raise ValueError('asset_outside_installation')
    for key in ('python','node','sdk','key','policy','manifest'):
        if not paths[key].is_file():raise ValueError('installation_file_missing')
    manifest=load(paths['manifest']);policy=load(paths['policy'])
    if manifest['commit']!=value['source_sha'] or policy['source_sha']!=value['source_sha']:
        raise ValueError('source_policy_mismatch')
    seen=set()
    for item in manifest['files']:
        name=item['path'];relative=PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name or name in seen:
            raise ValueError('manifest_path')
        seen.add(name);target=source.joinpath(*relative.parts)
        if not target.resolve().is_relative_to(source) or target.is_symlink() or sha(target.read_bytes())!=item['sha256']:
            raise ValueError('source_bytes_changed')
    required={'scripts/native_worker_runner.py','scripts/qa/process_guard.py','tools/native-channel/session.mjs'}
    if not required<=seen or not any(p.startswith('sale_monitor/') for p in seen):raise ValueError('manifest_incomplete')
    actual={p.relative_to(source).as_posix() for p in (source/'sale_monitor').glob('*.py')}
    approved={p for p in seen if p.startswith('sale_monitor/') and '/' not in p[len('sale_monitor/'): ] and p.endswith('.py')}
    if actual!=approved:raise ValueError('unapproved_runtime_file')
    runtime={p.name:sha(p.read_bytes()) for p in sorted((source/'sale_monitor').glob('*.py'))}
    digest=sha(json.dumps(runtime,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())
    if policy.get('enabled') is not True or policy['runtime_sha256']!=digest:raise ValueError('runtime_policy_mismatch')
    if paths['sdk'].resolve()!=(source/'tools/native-channel/session.mjs').resolve():raise ValueError('sdk_path_mismatch')
    return value,paths


def atomic(path,value):
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('x',encoding='utf-8') as output:
            json.dump(value,output,ensure_ascii=False);output.write('\n');output.flush();os.fsync(output.fileno())
        os.replace(temporary,path)
    finally:
        if temporary.exists():temporary.unlink()


def now():return datetime.now(timezone.utc).isoformat()


def child(config):
    # The parent assigns the waiting child to its Windows Job before permitting
    # any imports of collector code, key access, or network activity.
    if sys.stdin.readline()!='start\n':raise ValueError('parent_handshake')
    value,paths=validate(config)
    os.chdir(paths['source']);sys.path.insert(0,str(paths['source']))
    sys.argv=['native_operations','work','--root',str(paths['root']/'state'),
              '--key',str(paths['key']),'--policy',str(paths['policy']),
              '--sdk',str(paths['sdk']),'--node',str(paths['node'])]
    from sale_monitor.native_operations import main as worker
    worker()


def run(config):
    if os.name!='nt':raise ValueError('windows_worker_required')
    value,paths=validate(config);root=paths['root'];(root/'tmp').mkdir(exist_ok=True)
    started=now();status=root/'last-status.json'
    free=shutil.disk_usage(root).free
    if free<START_FREE_BYTES:
        atomic(status,{'status':'storage_required','started_at':started,'ended_at':now(),
                       'source_sha':value['source_sha'],'free_bytes':free,'required_free_bytes':START_FREE_BYTES})
        return 1
    guard_path=paths['source']/'scripts/qa/process_guard.py'
    spec=importlib.util.spec_from_file_location('native_process_guard',guard_path)
    guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)
    env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','COMSPEC','PATH','PATHEXT','USERPROFILE','LOCALAPPDATA','APPDATA'}}
    env.update(TEMP=str(root/'tmp'),TMP=str(root/'tmp'),PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1')
    atomic(status,{'status':'running','started_at':started,'pid':os.getpid(),'source_sha':value['source_sha']})
    process=subprocess.Popen([str(paths['python']),'-B',str(paths['source']/'scripts/native_worker_runner.py'),
                              '--child','--config',str(Path(config).resolve())],cwd=paths['source'],env=env,
                             stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                             encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW)
    guard.attach(process)
    deadline=time.monotonic()+48*60
    process.stdin.write('start\n');process.stdin.flush();process.stdin.close();process.stdin=None
    while True:
        try:
            stdout,stderr=process.communicate(timeout=STORAGE_CHECK_SECONDS);break
        except subprocess.TimeoutExpired:
            free=shutil.disk_usage(root).free
            if free<STOP_FREE_BYTES or time.monotonic()>=deadline:
                process.kill();stdout,stderr=process.communicate()
                atomic(status,{'status':'worker_storage_stop' if free<STOP_FREE_BYTES else 'worker_timeout',
                               'started_at':started,'ended_at':now(),'source_sha':value['source_sha'],'free_bytes':free})
                return 1
    result={'status':'worker_completed' if process.returncode==0 else 'worker_failed',
            'started_at':started,'ended_at':now(),'exit_code':process.returncode,'source_sha':value['source_sha']}
    if process.returncode==0:
        try:result['worker']=json.loads(stdout)
        except (ValueError,TypeError):result['status']='worker_output_invalid'
    else:
        # Preserve only the fixed worker's bounded, sanitized rejection code.
        result['reason']=next((line[:180] for line in stderr.splitlines() if
                               line.startswith(('native_operation_rejected:','native_operation_failed:'))),'worker_process_failed')
    atomic(status,result)
    return 0 if result['status']=='worker_completed' else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--verify-only',action='store_true');parser.add_argument('--child',action='store_true')
    args=parser.parse_args()
    if args.child:return child(args.config)
    if args.verify_only:
        value,_=validate(args.config);print(json.dumps({'status':'installation_verified','source_sha':value['source_sha']}));return
    raise SystemExit(run(args.config))


if __name__=='__main__':main()

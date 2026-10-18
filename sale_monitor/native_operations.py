"""Fixed commands for the native session coordinator and Windows worker."""
from __future__ import annotations

import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from . import native_sessions as S, native_channel as N, receipts as R
from .http import Client
from .models import iso, utcnow
from .storage import Store


def load(path):return json.loads(Path(path).read_bytes())


def git(cwd,*args):
    process=subprocess.run(['git',*args],cwd=cwd,capture_output=True,timeout=90)
    R.require(process.returncode==0,'trusted_git_failed')
    return process.stdout


def controller_key(args):
    path=args.controller_key.resolve()
    R.require(not path.is_relative_to(args.root.resolve()) and not path.is_relative_to(args.outgoing.resolve()),'private_key_inside_artifacts')
    R.require(not path.is_symlink() and path.stat().st_size==32,'controller_key_file')
    return Ed25519PrivateKey.from_private_bytes(path.read_bytes())


def prepare(args):
    R.require(os.environ.get('GITHUB_REPOSITORY')==N.REPOSITORY,'repository_scope')
    R.require(60<=args.seconds<=2400 and 1<=args.batches<=200,'session_bounds')
    args.root.mkdir();(args.root/'inputs').mkdir();(args.root/'receipts').mkdir()
    source=os.environ['SOURCE_COMMIT'];data=git(args.state,'rev-parse','HEAD').decode().strip()
    run=f'{os.environ["GITHUB_RUN_ID"]}-{os.environ["GITHUB_RUN_ATTEMPT"]}'
    scope=R.SCOPE if args.isolated else R.COLLECTION_SCOPE
    if args.isolated:run='isolated-native-github-'+run
    config=load(args.config);worker=load(args.worker_config)
    R.require(worker['enabled'] is True and worker['worker_id']==N.WORKER,'worker_disabled')
    R.require(args.isolated or config['stores']['yahoo'].get('native_collection') is True,'native_collection_disabled')
    R.supported_runtime('yahoo',config)
    disk=Store(args.state/'state');before=disk.load('stores/yahoo.json',None)
    R.require(isinstance(before,dict),'native_initial_state_missing')
    initial=R.sha(R.encode(before))
    # This is the normal seeding path, including requests and candidate identities.
    collector=R.StrictCollector(args.state/'state','yahoo',config,run,Client(),disk=disk)
    collector.seed()
    receiver=R.receiver(collector.state,config,source,data,run,{N.WORKER:worker},scope=scope)
    S.atomic(args.root/'receiver.json',receiver)
    key_path=args.controller_key.resolve()
    R.require(not key_path.is_relative_to(args.root.resolve()) and not key_path.is_relative_to(args.outgoing.resolve()),'private_key_inside_artifacts')
    key=Ed25519PrivateKey.generate()
    fd=os.open(key_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as output:
        output.write(key.private_bytes_raw());output.flush();os.fsync(output.fileno())
    now=utcnow()
    session=S.session_metadata(config,source,data,run,key,scope=scope,now=now,seconds=args.seconds+90)
    credentials=json.load(sys.stdin)
    packet=N.seal(worker['exchange_key_hex'],credentials,session)
    commit=N.publish_packet(packet)
    S.atomic(args.root/'request.json',packet)
    control={'session':session,'mailbox_commit':commit,'initial_state_sha256':initial,'sequence':0,
             'collection_deadline':iso(now+timedelta(seconds=args.seconds)),'max_batches':args.batches,'active_lease':None}
    S.atomic(args.root/'control.json',control)
    result=export(args)
    return {'session_id':session['session_id'],'expires_at':session['expires_at'],
            'collection_deadline':control['collection_deadline'],'max_batches':args.batches,
            'mailbox_commit':commit,'source_sha':source,'data_sha':data,'scheduled_credit':False,**result}


def next_batch(args):
    control=load(args.root/'control.json');session=control['session'];now=utcnow()
    receiver=load(args.root/'receiver.json')
    active=control['active_lease']
    R.require(not active or active in receiver['receipts'] or active in receiver.get('abandoned',{}),'previous_batch_in_flight')
    if now+timedelta(seconds=60)>=R.moment(control['collection_deadline']):return {'status':'finished','reason':'time_budget'}
    if control['sequence']>=control['max_batches']:return {'status':'finished','reason':'batch_budget'}
    seq=control['sequence']+1;root=args.root/'inputs'/f'{seq:04d}'
    try:body=S.signed_input(root,args.root/'receiver.json',session,controller_key(args),seq,now=now)
    except R.Rejected as error:
        if str(error)!='no_runnable_tasks':raise
        c=R.collector(receiver['state'],receiver['config'],session['run_id'],Client())
        _,waits=R.runnable(c.state['queue'],c.attempted,c.cfg,c.client,{},now.timestamp())
        if waits:
            delay=max(1,min(item['until_epoch_seconds'] for item in waits)-now.timestamp())
            return {'status':'waiting','seconds':min(30,delay)}
        return {'status':'finished','reason':'no_runnable_tasks'}
    lease=body['lease'];control.update(sequence=seq,active_lease=lease['id'])
    S.atomic(args.root/'control.json',control)
    # Publish a safe checkpoint before waiting for an untrusted worker.
    export(args)
    return {'status':'issued','sequence':seq,'lease_id':lease['id'],'expires_at':lease['expires_at'],
            'directory':str(root),'input_name':f'native-input-{session["session_id"]}-{seq:04d}',
            'receipt_name':'native-receipt-'+lease['id']}


def receive(args):
    control=load(args.root/'control.json');lease_id=control['active_lease']
    R.require(re.fullmatch('[0-9a-f]{32}',lease_id or ''),'active_lease_missing')
    directory=args.root/'receipts'/lease_id
    paths=list(args.download.iterdir())
    R.require(len(paths)==1 and paths[0].is_file() and not paths[0].is_symlink() and paths[0].stat().st_size<=N.MAX_ZIP,'receipt_download')
    if not directory.exists():N.safe_extract(paths[0].read_bytes(),directory)
    result=R.receive(directory,args.root/'receiver.json')
    export(args)
    return result


def export(args):
    control=load(args.root/'control.json');receiver=load(args.root/'receiver.json')
    attestation=S.export_state(receiver,control['session'],control['mailbox_commit'],control['initial_state_sha256'],
                               controller_key(args),args.outgoing)
    return {'state_sha256':attestation['final_state_sha256'],'accepted_batches':len(receiver['receipts']),
            'pending':len(receiver['state']['queue']),'scope':receiver['scope']}


def finish(args):
    control=load(args.root/'control.json')
    result=export(args);directory=args.root/'completion'
    S.finish_session(directory,control['session'],controller_key(args))
    return {**result,'directory':str(directory),'name':'native-end-'+control['session']['session_id']}


def trust(args):
    # A valid signature alone is insufficient: the attacker could supply a key.
    # Fetch only the known branch, prove ancestry, then read its committed key.
    attestation=load(args.incoming/'native/yahoo.json');commit=attestation.get('mailbox_commit','')
    R.require(bool(re.fullmatch('[0-9a-f]{40}',commit)),'mailbox_commit')
    ref='refs/remotes/origin/'+N.MAILBOX_BRANCH
    git(args.state,'fetch','--no-tags','origin',N.MAILBOX_BRANCH+':'+ref)
    git(args.state,'merge-base','--is-ancestor',commit,ref)
    packet=json.loads(git(args.state,'show',commit+':'+N.MAILBOX_PATH))
    R.require(set(packet)=={'metadata','ephemeral','nonce','ciphertext'} and set(packet['metadata'])==S.SESSION_FIELDS,'trust_packet')
    trusted={'commit':commit,'metadata':packet['metadata']}
    S.atomic(args.trust,trusted)
    return {'status':'mailbox_ancestry_verified','commit':commit}


def sdk(args,command,credentials,payload):
    child_env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','PATH','TEMP','TMP','HOME','USERPROFILE'}}
    child_env.update(ACTIONS_RUNTIME_TOKEN=credentials['runtime_token'],ACTIONS_RESULTS_URL=credentials['results_url'],
                     GITHUB_WORKSPACE=str(args.root.resolve()),GITHUB_SERVER_URL='https://github.com',
                     ACTIONS_ARTIFACT_UPLOAD_CONCURRENCY='1',NATIVE_PYTHON=sys.executable)
    process=subprocess.run([args.node,str(args.sdk),command],input=json.dumps(payload),env=child_env,
        capture_output=True,text=True,encoding='utf-8',timeout=180)
    R.require(process.returncode==0,'artifact_transfer_failed')
    lines=[line.removeprefix('NATIVE_SESSION_RESULT=') for line in process.stdout.splitlines() if line.startswith('NATIVE_SESSION_RESULT=')]
    R.require(len(lines)==1,'artifact_transfer_result')
    return json.loads(lines[0])


def work(args):
    R.require(os.name=='nt','windows_worker_required')
    args.root.mkdir(parents=True,exist_ok=True)
    with R.writer_lock(args.root/'.worker.lock'):
        policy=load(args.policy);config=load(args.config);signing,exchange=N.load_keys(args.key)
        R.require(signing.public_key().public_bytes_raw().hex()==policy['public_key_hex'] and
                  exchange.public_key().public_bytes_raw().hex()==policy['exchange_key_hex'],'registered_key_mismatch')
        packet=N.mailbox_packet()
        session,credentials=N.open_packet(packet,exchange,policy,validator=S.validate_session)
        R.require(session['config_sha256']==R.sha(R.encode(config)),'worker_config_mismatch')
        root=args.root/session['session_id'];root.mkdir(exist_ok=True)
        S.atomic(root/'request.json',packet)  # Encrypted credentials only.
        progress_path=root/'progress.json'
        progress=load(progress_path) if progress_path.exists() else {'sequence':0,'session_id':session['session_id']}
        end=R.moment(session['expires_at'])-timedelta(seconds=15);uploaded=0
        while utcnow()<end and progress['sequence']<200:
            seq=progress['sequence']+1;directory=root/f'input-{seq:04d}'
            if not directory.exists():
                download=root/('download-'+uuid.uuid4().hex)
                result=sdk(args,'input',credentials,{'name':f'native-input-{session["session_id"]}-{seq:04d}',
                    'end_name':'native-end-'+session['session_id'],'directory':str(download)})
                if not result['found']:
                    time.sleep(8);continue
                paths=list(download.iterdir())
                R.require(len(paths)==1 and paths[0].is_file() and paths[0].stat().st_size<=S.MAX_INPUT+100_000,'input_download')
                temporary=root/('input-pending-'+uuid.uuid4().hex)
                S.extract_input(paths[0].read_bytes(),temporary)
                if result.get('finished'):
                    S.read_completion(temporary,session)
                    return {'status':'controller_finished','session_id':session['session_id'],'uploaded_batches':uploaded,'last_sequence':progress['sequence']}
                os.rename(temporary,directory)
            body=S.read_input(directory,session,config)
            R.require(body['sequence']==seq,'input_sequence_mismatch')
            result=S.execute_claim(args.root/'ledger',body,config,signing)
            if result['status']=='ambiguous':return {'status':'ambiguous_attempt','lease_id':result['lease_id'],'uploaded_batches':uploaded}
            if result['status']!='uploaded':
                R.require(utcnow()<R.moment(body['lease']['expires_at']),'lease_expired_before_upload')
                upload=sdk(args,'receipt',credentials,{'name':'native-receipt-'+result['lease_id'],
                    'directory':result['checkpoint_path'],'verification_root':str(root/('verify-'+uuid.uuid4().hex))})
                S.mark_uploaded(args.root/'ledger',result['lease_id'],result['manifest_sha256'],upload)
                uploaded+=1
            progress['sequence']=seq;S.atomic(progress_path,progress)
        return {'status':'session_finished','session_id':session['session_id'],'uploaded_batches':uploaded,'last_sequence':progress['sequence']}


def compare_upload(args):
    data=json.load(sys.stdin);directory=Path(data['directory']);raw=Path(data['archive']).read_bytes()
    temp=Path(data['verification_root']);N.safe_extract(raw,temp)
    names={p.name for p in directory.iterdir()}
    R.require(names=={p.name for p in temp.iterdir()} and all((directory/name).read_bytes()==(temp/name).read_bytes() for name in names),'existing_artifact_conflict')
    return {'status':'identical'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['prepare','next','receive','export','finish','trust','work','compare-upload'])
    parser.add_argument('--root',type=Path,default=Path('native-session'))
    parser.add_argument('--state',type=Path,default=Path('state-repo'))
    parser.add_argument('--config',type=Path,default=Path('config/sources.json'))
    parser.add_argument('--worker-config',type=Path,default=Path('config/native-worker.json'))
    parser.add_argument('--outgoing',type=Path,default=Path('outgoing'))
    parser.add_argument('--incoming',type=Path,default=Path('incoming'))
    parser.add_argument('--trust',type=Path,default=Path('native-trust.json'))
    parser.add_argument('--controller-key',type=Path)
    parser.add_argument('--download',type=Path)
    parser.add_argument('--key',type=Path)
    parser.add_argument('--policy',type=Path)
    parser.add_argument('--node',default='node')
    parser.add_argument('--sdk',type=Path,default=Path('tools/native-channel/session.mjs'))
    parser.add_argument('--seconds',type=int,default=2100)
    parser.add_argument('--batches',type=int,default=200)
    parser.add_argument('--isolated',action='store_true')
    args=parser.parse_args()
    try:
        commands={'prepare':prepare,'next':next_batch,'receive':receive,'export':export,'finish':finish,'trust':trust,'work':work,'compare-upload':compare_upload}
        print(json.dumps(commands[args.command](args),ensure_ascii=False))
    except R.Rejected as error:
        print('native_operation_rejected:'+str(error),file=sys.stderr);raise SystemExit(1)
    except Exception as error:
        print('native_operation_failed:'+type(error).__name__,file=sys.stderr);raise SystemExit(1)


if __name__=='__main__':main()

"""Controller-signed batches, durable worker claims, and attested state export.

Artifact credentials grant upload access, so artifact names and worker-provided
state cannot authorize publication. Only the cloud controller's private key can
sign inputs and exports. Its public key is anchored in the trusted mailbox ref.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import io
import json
import os
import re
import uuid
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.exceptions import InvalidSignature
from . import receipts as R, native_channel as N
from .http import Client
from .models import iso, utcnow

INPUT_DOMAIN=b'pc-sale-monitor.native-input.v2\x00'
EXPORT_DOMAIN=b'pc-sale-monitor.native-export.v2\x00'
END_DOMAIN=b'pc-sale-monitor.native-end.v2\x00'
MAX_INPUT=20_000_000
SESSION_FIELDS={'version','repository','worker_id','source_sha','data_sha','runtime_sha256',
                'config_sha256','scope','run_id','session_id','created_at','expires_at','controller_key_hex'}


def atomic(path, value):
    path=Path(path)
    tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with tmp.open('xb') as output:
            output.write(R.encode(value)+b'\n');output.flush();os.fsync(output.fileno())
        os.replace(tmp,path)
    finally:
        if tmp.exists():tmp.unlink()


def verify(public, signature, domain, raw):
    try:Ed25519PublicKey.from_public_bytes(bytes.fromhex(public)).verify(signature,domain+raw)
    except (ValueError,InvalidSignature,TypeError):raise R.Rejected('controller_signature_invalid') from None


def session_metadata(config, source, data, run_id, key, *, scope=R.COLLECTION_SCOPE, now=None, seconds=2400):
    now=now or utcnow()
    R.require(60<=seconds<=2700,'session_lifetime')
    value={'version':2,'repository':N.REPOSITORY,'worker_id':N.WORKER,'source_sha':source,'data_sha':data,
           'runtime_sha256':R.runtime_hash(),'config_sha256':R.sha(R.encode(config)),'scope':scope,'run_id':run_id,
           'session_id':uuid.uuid4().hex,'created_at':iso(now),'expires_at':iso(now+timedelta(seconds=seconds)),
           'controller_key_hex':key.public_key().public_bytes_raw().hex()}
    return value


def validate_session(metadata, policy, *, now=None):
    now=now or utcnow()
    R.require(set(metadata)==SESSION_FIELDS and metadata['version']==2,'session_schema')
    R.require(metadata['repository']==N.REPOSITORY and metadata['worker_id']==N.WORKER,'session_scope')
    R.require(metadata['scope'] in {R.SCOPE,R.COLLECTION_SCOPE},'receipt_scope')
    R.require(bool(re.fullmatch(r'isolated-native-github-[0-9]+-[0-9]+' if metadata['scope']==R.SCOPE else r'[0-9]+-[0-9]+',metadata['run_id'])),'session_run')
    R.require(policy['enabled'] is True and policy['source_sha']==metadata['source_sha'],'source_not_approved')
    R.require(all(re.fullmatch('[0-9a-f]{40}',metadata[key]) for key in ('source_sha','data_sha')),'source_binding')
    R.require(metadata['runtime_sha256']==policy['runtime_sha256']==R.runtime_hash(),'source_runtime_mismatch')
    R.require(re.fullmatch('[0-9a-f]{32}',metadata['session_id']) and re.fullmatch('[0-9a-f]{64}',metadata['controller_key_hex']),'session_keys')
    created,expires=R.moment(metadata['created_at']),R.moment(metadata['expires_at'])
    R.require(created<=now<expires and 0<(expires-created).total_seconds()<=2700,'session_expired')


def signed_input(root, receiver_path, session, key, sequence, *, now=None):
    now=now or utcnow()
    R.require(type(sequence) is int and 1<=sequence<=400,'sequence_limit')
    lifetime=min(timedelta(minutes=8),R.moment(session['expires_at'])-now-timedelta(seconds=30))
    R.require(lifetime>=timedelta(seconds=30),'session_ending')
    lease=R.issue(receiver_path,N.WORKER,now=now,lifetime=lifetime)
    receiver=json.loads(Path(receiver_path).read_bytes())
    R.require(all(receiver[key]==session[key] for key in ('scope','source_sha','data_sha','runtime_sha256'))
              and receiver['active_run_id']==session['run_id'],'session_receiver_mismatch')
    body={'version':2,'session_id':session['session_id'],'sequence':sequence,'lease':lease,'state':receiver['state']}
    raw=R.encode(body)
    R.require(len(raw)<=MAX_INPUT,'input_size')
    root=Path(root);root.mkdir()
    (root/'request.json').write_bytes(raw)
    (root/'signature.bin').write_bytes(key.sign(INPUT_DOMAIN+raw))
    return body


def read_input(root, session, config, *, now=None):
    now=now or utcnow();root=Path(root)
    R.require(not root.is_symlink() and {p.name for p in root.iterdir()}=={'request.json','signature.bin'},'input_files')
    p=root/'request.json';sig=root/'signature.bin'
    R.require(not p.is_symlink() and not sig.is_symlink() and p.stat().st_size<=MAX_INPUT and sig.stat().st_size==64,'input_size_or_path')
    raw=p.read_bytes();verify(session['controller_key_hex'],sig.read_bytes(),INPUT_DOMAIN,raw)
    body=json.loads(raw)
    R.require(set(body)=={'version','session_id','sequence','lease','state'} and body['version']==2 and body['session_id']==session['session_id'],'input_session')
    R.require(type(body['sequence']) is int and 1<=body['sequence']<=400,'input_sequence')
    lease=body['lease'];state=body['state']
    required={'id','worker_id','scope','store','source_sha','data_sha','runtime_sha256','config_sha256','base_state_sha256',
              'run_id','created_at','expires_at','tasks','task_order','scheduler_after'}
    R.require(set(lease)==required,'input_lease_schema')
    R.require(all(lease[k]==session[k] for k in ('worker_id','scope','source_sha','data_sha','runtime_sha256','config_sha256','run_id')),'input_binding')
    R.require(lease['runtime_sha256']==R.runtime_hash(),'source_runtime_mismatch')
    R.require(lease['store']==state['store']=='yahoo' and re.fullmatch('[0-9a-f]{32}',lease['id']),'input_store_or_id')
    R.require(1<=len(lease['task_order'])<=4 and len(set(lease['task_order']))==len(lease['task_order'])
              and set(lease['task_order'])==set(lease['tasks']) and len(lease['scheduler_after'])==len(lease['tasks']),'input_tasks')
    R.require(R.sha(R.encode(config))==lease['config_sha256'] and R.sha(R.encode(state))==lease['base_state_sha256'],'worker_input_mismatch')
    R.require(all(state['queue'].get(k)==task and R.supported_task('yahoo',task) and
                  (not task.get('url') or R.valid_url(task['url'])) for k,task in lease['tasks'].items()),'input_task_mismatch')
    created,expires=R.moment(lease['created_at']),R.moment(lease['expires_at'])
    R.require(R.moment(session['created_at'])<=created<=now<expires<=R.moment(session['expires_at'])
              and 0<(expires-created).total_seconds()<=600,'input_expired')
    R.supported_runtime('yahoo',config)
    return body


def extract_input(raw, target):
    R.require(len(raw)<=MAX_INPUT+100_000,'input_archive_size')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries=archive.infolist()
        R.require(len(entries)==2 and {e.filename for e in entries}=={'request.json','signature.bin'},'input_archive_members')
        for entry in entries:
            R.require(not entry.is_dir() and ((entry.external_attr>>16)&0o170000) in (0,0o100000)
                      and not entry.flag_bits&1 and entry.file_size<=(64 if entry.filename=='signature.bin' else MAX_INPUT),'input_archive_entry')
        values={entry.filename:archive.read(entry) for entry in entries}
    target=Path(target);target.mkdir()
    for name,value in values.items():(target/name).write_bytes(value)


def finish_session(root,session,key):
    root=Path(root);root.mkdir()
    raw=R.encode({'version':2,'session_id':session['session_id'],'finished_at':iso()})
    (root/'request.json').write_bytes(raw);(root/'signature.bin').write_bytes(key.sign(END_DOMAIN+raw))


def read_completion(root,session,*,now=None):
    root=Path(root);now=now or utcnow()
    R.require({p.name for p in root.iterdir()}=={'request.json','signature.bin'},'completion_files')
    p=root/'request.json';sig=root/'signature.bin'
    R.require(not p.is_symlink() and p.stat().st_size<=1024 and not sig.is_symlink() and sig.stat().st_size==64,'completion_size_or_path')
    raw=p.read_bytes();verify(session['controller_key_hex'],sig.read_bytes(),END_DOMAIN,raw)
    value=json.loads(raw)
    R.require(set(value)=={'version','session_id','finished_at'} and value['version']==2 and value['session_id']==session['session_id'],'completion_session')
    R.require(R.moment(session['created_at'])<=R.moment(value['finished_at'])<=now,'completion_time')
    return value


def execute_claim(root, body, config, signing, **kwargs):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    with R.writer_lock(root/'.claims.lock'):
        return _execute_claim(root,body,config,signing,**kwargs)


def _execute_claim(root, body, config, signing, *, client_factory=Client, hook=None, now=None):
    """Caller holds the worker-root OS lock throughout acquisition/upload.

    A claim is durable before any retail request. Restart never reruns a claim:
    it selects a completed immutable prefix, or reports an ambiguous attempt.
    An upload failure leaves the selected prefix unchanged for later delivery.
    """
    now=now or utcnow();hook=hook or (lambda stage:None)
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    lease=body['lease'];R.require(re.fullmatch('[0-9a-f]{32}',lease['id']),'lease_id')
    job=root/lease['id'];job.mkdir(exist_ok=True)
    ledger_path=job/'claim.json';pin=R.sha(R.encode(body))
    if ledger_path.exists():
        ledger=json.loads(ledger_path.read_bytes());R.require(ledger['input_sha256']==pin,'claim_input_changed')
        resumed=True
    else:
        ledger={'lease_id':lease['id'],'input_sha256':pin,'status':'claimed','claimed_at':iso(now)}
        atomic(ledger_path,ledger);resumed=False;hook('after_claim')
        try:
            R.produce(job/'bundle',lease,body['state'],config,client_factory(),signing,
                      on_checkpoint=lambda p:hook('after_checkpoint'))
        except Exception as error:
            # No arbitrary exception text (URLs and SDK messages may be secrets).
            ledger['interruption_type']=type(error).__name__
    if ledger['status']=='uploaded':return {**ledger,'resumed':True}
    if ledger['status']=='ambiguous':return {**ledger,'resumed':True}
    checkpoints=sorted((job/'bundle').glob('checkpoint-*')) if (job/'bundle').exists() else []
    if ledger.get('checkpoint'):
        chosen=job/ledger['checkpoint']
        R.require(chosen in checkpoints,'checkpoint_missing')
    elif checkpoints:
        chosen=checkpoints[-1]
    else:
        ledger.update(status='ambiguous',requests=None);atomic(ledger_path,ledger)
        return {**ledger,'resumed':resumed}
    # Validate our durable bytes, signature, body hashes, and native replay before
    # re-upload. Expired work is retained on disk but never retimestamped.
    receiver=R.receiver(body['state'],config,lease['source_sha'],lease['data_sha'],lease['run_id'],
                        {lease['worker_id']:{'enabled':True,'stores':['yahoo'],
                         'public_key_hex':signing.public_key().public_bytes_raw().hex()}},scope=lease['scope'])
    receiver['leases'][lease['id']]=lease
    _,receipt=R.read_bundle(chosen,receiver,now if resumed else utcnow())
    ledger.update(status='ready',checkpoint=chosen.relative_to(job).as_posix(),
                  manifest_sha256=receipt['manifest_sha256'],requests=receipt['requests'])
    atomic(ledger_path,ledger);hook('after_ready')
    return {**ledger,'resumed':resumed,'checkpoint_path':str(chosen)}


def mark_uploaded(root, lease_id, manifest, upload):
    R.require(re.fullmatch('[0-9a-f]{32}',lease_id),'lease_id')
    path=Path(root)/lease_id/'claim.json';ledger=json.loads(path.read_bytes())
    R.require(ledger['status'] in {'ready','uploaded'} and ledger['manifest_sha256']==manifest,'upload_claim_mismatch')
    ledger.update(status='uploaded',upload=deepcopy(upload));atomic(path,ledger)


def abandon(receiver_path, lease_id, reason):
    R.require(reason in {'worker_unavailable','delivery_timeout','ambiguous_attempt','session_ended'},'abandon_reason')
    def update(value):
        R.require(lease_id in value['leases'] and lease_id not in value['receipts'],'abandon_lease')
        value.setdefault('abandoned',{})[lease_id]={'reason':reason,'at':iso()}
        return value,{'status':'abandoned','lease_id':lease_id}
    return R.transaction(receiver_path,update)


def export_state(receiver, session, mailbox_commit, initial_state_hash, key, destination):
    R.require(receiver['scope']==session['scope'] and receiver['active_run_id']==session['run_id'],'export_session')
    result=deepcopy(receiver);R.apply_totals(result);state=result['state']
    unresolved=[k for k in result['leases'] if k not in result['receipts']]
    partial=sum(bool(item['unattempted']) for item in result['receipts'].values())
    received=sum(len(item['results']) for item in result['receipts'].values())
    state.update(status='native_partial' if state['queue'] or unresolved else 'complete',pending_count=len(state['queue']),
                 cycle_complete=not state['queue'] and not unresolved)
    state['native_collection']={'session_id':session['session_id'],'accepted_batches':len(result['receipts']),
                                'accepted_tasks':received,'unreceived_batches':len(unresolved),
                                'partial_batches':partial,'request_count_complete':not unresolved and not partial,
                                'received_request_count':state['request_count']}
    if unresolved or partial:state['request_count']=None
    root=Path(destination);(root/'stores').mkdir(parents=True,exist_ok=True);(root/'native').mkdir(exist_ok=True)
    atomic(root/'stores/yahoo.json',state)
    attestation={'version':2,'repository':N.REPOSITORY,'store':'yahoo','scope':session['scope'],'run_id':session['run_id'],
                 'source_sha':session['source_sha'],'data_sha':session['data_sha'],'runtime_sha256':session['runtime_sha256'],
                 'session_id':session['session_id'],'mailbox_commit':mailbox_commit,
                 'initial_state_sha256':initial_state_hash,'final_state_sha256':R.sha(R.encode(state))}
    raw=R.encode(attestation);(root/'native/yahoo.json').write_bytes(raw)
    (root/'native/yahoo.sig').write_bytes(key.sign(EXPORT_DOMAIN+raw))
    return attestation


def verify_export(incoming, state, current, trust, *, source_sha, run_id, data_sha):
    """trust must come from an ancestor of the real mailbox branch, not input."""
    root=Path(incoming);p=root/'native/yahoo.json';sig=root/'native/yahoo.sig'
    R.require(not p.is_symlink() and not sig.is_symlink() and p.stat().st_size<=4096 and sig.stat().st_size==64,'export_files')
    raw=p.read_bytes();attestation=json.loads(raw)
    verify(trust['metadata']['controller_key_hex'],sig.read_bytes(),EXPORT_DOMAIN,raw)
    m=trust['metadata']
    expected={'version':2,'repository':N.REPOSITORY,'store':'yahoo','scope':R.COLLECTION_SCOPE,'run_id':run_id,
              'source_sha':source_sha,'data_sha':data_sha,'runtime_sha256':R.runtime_hash(),
              'session_id':m['session_id'],'mailbox_commit':trust['commit'],
              'initial_state_sha256':attestation.get('initial_state_sha256'),'final_state_sha256':R.sha(R.encode(state))}
    R.require(attestation==expected,'export_binding')
    R.require(all(m[k]==expected[k] for k in ('repository','scope','run_id','source_sha','data_sha','runtime_sha256','session_id')),'export_trust_binding')
    actual=R.sha(R.encode(current))
    R.require(actual in {attestation['initial_state_sha256'],attestation['final_state_sha256']},'native_state_conflict')
    R.require(state.get('run_id')==run_id and state.get('store')=='yahoo','export_state_binding')
    return {'status':'already_current' if actual==attestation['final_state_sha256'] else 'verified','session_id':m['session_id']}

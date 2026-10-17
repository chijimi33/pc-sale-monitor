"""Isolated authenticated native replay; no scheduler or publisher entry point.
A registered Ed25519 key authenticates the worker. It is not proof that a shop
served truthful content. The controller owns tasks, run/source/config bindings
and trust roots; the receiver reparses bodies and never imports worker state.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from contextlib import contextmanager
from urllib.parse import urlsplit
import hashlib, json, os, re, uuid
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature
from .http import Page, FetchError, Client
from .retrying import runnable
from .models import Offer, timestamp, iso, utcnow, allowed_url
from .runner import Collector
from . import yahoo_public

DOMAIN=b'pc-sale-monitor.isolated-native-receipt.v1\x00'
SCOPE='isolated_native_receipt_no_scheduled_credit'
ERRORS={'RemoteDisconnected','TimeoutError','ConnectionResetError','ConnectionAbortedError',
        'ConnectionRefusedError','BrokenPipeError','IncompleteRead','URLError','OSError','Error',
        'http_403','http_404','http_429','http_500','http_502','http_503','http_504',
        'rate_limited_retry_later','excluded_redirect_host','excluded_source'}

class Rejected(ValueError): pass
class InvalidTape(BaseException): pass

def require(ok, reason):
    if not ok: raise Rejected(reason)

def encode(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()

def sha(value): return hashlib.sha256(value).hexdigest()
def moment(value):
    date=timestamp(value)
    require(date is not None and date.tzinfo is not None,'invalid_time')
    return date

def runtime_hash():
    root=Path(__file__).parent
    return sha(encode({p.name:sha(p.read_bytes()) for p in sorted(root.glob('*.py'))}))

def valid_url(url):
    try:
        parsed=urlsplit(url)
        return parsed.scheme=='https' and bool(parsed.hostname) and not parsed.username and not parsed.password and allowed_url(url)
    except (TypeError,ValueError): return False

@contextmanager
def writer_lock(path):
    with Path(path).open('a+b') as lock:
        if lock.tell()==0: lock.write(b'0');lock.flush()
        lock.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError: raise Rejected('receiver_busy') from None
        yield

def transaction(path,update,hook=None):
    path=Path(path);hook=hook or (lambda stage:None)
    with writer_lock(path.with_suffix('.lock')):
        hook('after_lock');before=path.read_bytes();changed,result=update(json.loads(before))
        if changed is None:return result
        tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            with tmp.open('xb') as f:f.write(encode(changed)+b'\n');f.flush();os.fsync(f.fileno())
            hook('before_replace');require(path.read_bytes()==before,'concurrent_state_change')
            os.replace(tmp,path);hook('after_replace');return result
        finally:
            if tmp.exists():tmp.unlink()

def receiver(state,config,source_sha,data_sha,run_id,workers):
    require(run_id.startswith('isolated-native-'),'isolated_run_required')
    require(all(re.fullmatch('[0-9a-f]{40}',s) for s in [source_sha,data_sha]),'invalid_source_binding')
    return {'scope':SCOPE,'state':deepcopy(state),'config':deepcopy(config),'source_sha':source_sha,
            'data_sha':data_sha,'runtime_sha256':runtime_hash(),'active_run_id':run_id,
            'workers':deepcopy(workers),'leases':{},'receipts':{}}

def supported_runtime(store,config):
    cfg=config['stores'][store]
    if cfg['adapter']=='html' and store!='yahoo':return
    require(store=='yahoo' and cfg['adapter']=='yahoo' and cfg.get('public_html_fallback') is True,
            'unsupported_store')
    # The receipt path captures public requests only. Do not let a later
    # environment change select the API and put its credential in a tape.
    require(not os.environ.get('YAHOO_CLIENT_ID'),'public_receipt_requires_no_api_key')


def supported_task(store,task):
    if store!='yahoo':return task['type'] in {'product','search','list'}
    try:
        kind=task['type']
        if kind=='product':
            yahoo_public.product_scope(task['url']);return True
        if kind not in {'yahoo','yahoo_public_search'}:return False
        number=task.get('start',1)
        yahoo_public.search_url(task['query'],number)
        if kind=='yahoo':return number==1 and not task.get('url')
        return yahoo_public.search_scope(task['url'],task['query'],number)
    except (KeyError,ValueError,TypeError,FetchError):return False


def issue(path,worker_id,task_ids,*,now=None,lifetime=timedelta(minutes=10)):
    now=now or utcnow()
    require(0<lifetime.total_seconds()<=600,'invalid_lifetime')
    require(isinstance(task_ids,list) and 1<=len(task_ids)<=4 and len(set(task_ids))==len(task_ids),'invalid_selection')
    def update(r):
        worker=r['workers'].get(worker_id);store=r['state']['store']
        require(worker and worker.get('enabled') is True and store in worker['stores'],'worker_not_authorized')
        require(r['scope']==SCOPE and r['runtime_sha256']==runtime_hash(),'source_runtime_mismatch')
        supported_runtime(store,r['config'])
        c=collector(r['state'],r['config'],r['active_run_id'],Client())
        ready,_=runnable(c.state['queue'],c.attempted,c.cfg,c.client,{},now.timestamp())
        ready_ids={key for key,task in ready}
        require(set(task_ids)<=ready_ids,'tasks_waiting_or_already_attempted')
        tasks={}
        for key in task_ids:
            task=r['state']['queue'].get(key)
            require(task and supported_task(store,task),'unsupported_task')
            require(not task.get('url') or valid_url(task['url']),'invalid_task_url')
            tasks[key]=deepcopy(task)
        lease={'id':uuid.uuid4().hex,'worker_id':worker_id,'scope':SCOPE,'store':store,
               'source_sha':r['source_sha'],'data_sha':r['data_sha'],'runtime_sha256':r['runtime_sha256'],
               'config_sha256':sha(encode(r['config'])),'base_state_sha256':sha(encode(r['state'])),
               'run_id':r['active_run_id'],'created_at':iso(now),'expires_at':iso(now+lifetime),'tasks':tasks,'task_order':list(task_ids)}
        r['leases'][lease['id']]=lease
        return r,deepcopy(lease)
    return transaction(path,update)

class MemoryStore:
    def __init__(self,state):self.state=deepcopy(state)
    def load(self,name,default):return deepcopy(self.state) if name==f'stores/{self.state["store"]}.json' else deepcopy(default)
    def save(self,name,state):self.state=deepcopy(state)

class StrictCollector(Collector):
    def process(self,task):
        before=deepcopy(self.state)
        try:
            super().process(task)
            if task['type']=='product':
                rows=self.state['journal'][len(before['journal']):]
                if len(rows)!=1 or not rows[0]['offer']['verified'] or rows[0]['offer'].get('issues'):
                    raise FetchError('receipt_unverified_product')
                if rows[0]['offer'].get('shipping_yen') is None:
                    raise FetchError('receipt_shipping_unverified')
        except BaseException:
            self.state.clear();self.state.update(before)
            raise

def collector(state,config,run_id,client):
    supported_runtime(state['store'],config)
    return StrictCollector(Path('.'),state['store'],config,run_id,client,disk=MemoryStore(state))

class Snapshot:
    def __init__(self,value):self.value=deepcopy(value)
    def snapshot(self):return deepcopy(self.value)

class RecordingClient:
    def __init__(self,client,bundle):
        self.client=client;self.bundle=Path(bundle);self.actions=[]
    def __getattr__(self,name):return getattr(self.client,name)
    def get(self,url,**kwargs):return self.call('get',url,kwargs)
    def rendered(self,url):return self.call('rendered',url,{})
    def call(self,method,url,kwargs):
        require(not kwargs,'unsupported_request_parameters');require(valid_url(url),'invalid_request_url')
        require(len(self.actions)<16,'request_limit')
        action={'method':method,'url':url,'at':iso(),'result':None};page=None
        try:
            page=getattr(self.client,method)(url);action['result']='response'
            return page
        except Exception as exc:
            reason=str(exc) if isinstance(exc,FetchError) else type(exc).__name__
            if reason not in ERRORS and not re.fullmatch(r'(access|transport)_retry_later:[A-Za-z0-9_]{1,40}',reason):reason='Error'
            action.update(result='error',reason=reason);page=getattr(exc,'page',None)
            raise FetchError(reason,page) from None
        finally:
            if page is not None:
                require(isinstance(page,Page) and len(page.body)<=2_000_000,'invalid_response')
                name=f'body-{len(self.actions):02d}.bin';(self.bundle/name).write_bytes(page.body)
                meta=asdict(page);meta.pop('body');meta.update(path=name,bytes=len(page.body),sha256=sha(page.body));action['page']=meta
            action['at']=iso();action['count']=self.client.count
            action['evidence']=self.client.evidence.snapshot()
            action['deadlines']={k:deepcopy(getattr(self.client,k,{})) for k in ['retry_after','transport_retry_after','access_retry_after']}
            self.actions.append(action)

def _checkpoint(root,lease,started,entries,actions,key):
    """An immutable directory is visible only after all signed bytes are saved."""
    number=len(entries);final=root/f'checkpoint-{number:04d}'
    temporary=root/('pending-'+uuid.uuid4().hex);temporary.mkdir()
    manifest={'schema_version':1,'scope':SCOPE,'lease':deepcopy(lease),'started_at':started,
              'ended_at':iso(),'entries':deepcopy(entries),'actions':deepcopy(actions)}
    raw=encode(manifest)
    try:
        for action in actions:
            if 'page' in action:
                name=action['page']['path'];data=(root/'working'/name).read_bytes()
                with (temporary/name).open('xb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        for name,data in [('manifest.json',raw),('signature.bin',key.sign(DOMAIN+raw))]:
            with (temporary/name).open('xb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        require(not final.exists(),'checkpoint_exists');os.rename(temporary,final)
    finally:
        # Only this call's known scratch directory, never another checkpoint.
        if temporary.exists():
            for child in temporary.iterdir():child.unlink()
            temporary.rmdir()
    return final,sha(raw)

def produce(bundle,lease,state,config,client,private_key,*,limit=None,on_checkpoint=None):
    bundle=Path(bundle);bundle.mkdir();(bundle/'working').mkdir()
    require(lease['base_state_sha256']==sha(encode(state)) and lease['config_sha256']==sha(encode(config)),'worker_input_mismatch')
    require(lease['runtime_sha256']==runtime_hash(),'source_runtime_mismatch')
    require(moment(lease['created_at'])<=utcnow()<=moment(lease['expires_at']),'lease_expired')
    require(client.count==0,'client_must_be_fresh')
    tape=RecordingClient(client,bundle/'working');c=collector(state,config,lease['run_id'],tape)
    entries=[];started=iso();latest=None;pin=None
    for key in lease['task_order'][:limit]:
        begin=len(tape.actions);c.attempt(key)
        entries.append({'task_id':key,'begin':begin,'end':len(tape.actions)})
        latest,pin=_checkpoint(bundle,lease,started,entries,tape.actions,private_key)
        if on_checkpoint:on_checkpoint(latest)
    return {'worker_state':deepcopy(c.state),'manifest_sha256':pin,'entries':entries,
            'requests':client.count,'checkpoint':str(latest) if latest else None}

class ReplayClient:
    def __init__(self,actions,bodies):
        self.actions=actions;self.bodies=bodies;self.index=0;self.end=0;self.count=0
        self.retry_after={};self.transport_retry_after={};self.access_retry_after={}
        self.evidence=None
    def get(self,url,**kwargs):
        if kwargs:raise InvalidTape('unsupported_request_parameters')
        return self.call('get',url)
    def rendered(self,url):return self.call('rendered',url)
    def call(self,method,url):
        if self.index>=self.end:raise InvalidTape('dependency_missing')
        a=self.actions[self.index]
        if a['method']!=method or a['url']!=url:raise InvalidTape('dependency_mismatch')
        self.index+=1;self.count=a['count'];self.evidence=Snapshot(a['evidence'])
        for k,v in a['deadlines'].items():setattr(self,k,deepcopy(v))
        page=None
        if 'page' in a:
            p=deepcopy(a['page']);body=self.bodies[p.pop('path')];p.pop('sha256');p.pop('bytes')
            page=Page(body=body,**p)
        if a['result']=='error':raise FetchError(a['reason'],page)
        return page

def read_bundle(bundle,r,now):
    bundle=Path(bundle);require(not bundle.is_symlink(),'artifact_path_escape')
    p=bundle/'manifest.json';require(not p.is_symlink() and p.stat().st_size<=500_000,'manifest_size_or_path')
    raw=p.read_bytes();m=json.loads(raw)
    require(set(m)=={'schema_version','scope','lease','started_at','ended_at','entries','actions'} and m['schema_version']==1 and m['scope']==SCOPE,'manifest_schema')
    lease=m['lease'];known=r['leases'].get(lease.get('id'));require(known==lease,'lease_mismatch')
    worker=r['workers'].get(lease['worker_id'])
    require(worker and worker.get('enabled') is True and lease['store'] in worker['stores'],'worker_not_authorized')
    sig=bundle/'signature.bin';require(not sig.is_symlink() and sig.stat().st_size==64,'signature_size_or_path')
    try:Ed25519PublicKey.from_public_bytes(bytes.fromhex(worker['public_key_hex'])).verify(sig.read_bytes(),DOMAIN+raw)
    except (ValueError,InvalidSignature):raise Rejected('signature_invalid') from None
    prior=r['receipts'].get(lease['id'])
    if prior:
        require(prior['manifest_sha256']==sha(raw),'lease_already_consumed')
        return None,{'status':'already_received','lease_id':lease['id'],'scheduled_credit':False}
    require(r['scope']==SCOPE and r['active_run_id']==lease['run_id'],'run_mismatch')
    require(r['source_sha']==lease['source_sha'] and r['data_sha']==lease['data_sha'],'source_data_mismatch')
    require(r['runtime_sha256']==lease['runtime_sha256']==runtime_hash(),'source_runtime_mismatch')
    require(sha(encode(r['config']))==lease['config_sha256'],'config_changed')
    require(sha(encode(r['state']))==lease['base_state_sha256'],'base_state_changed')
    require(moment(lease['created_at'])<=moment(m['started_at'])<=moment(m['ended_at'])<=now<=moment(lease['expires_at']),'lease_or_batch_time_invalid')
    actions=m['actions'];entries=m['entries'];require(isinstance(actions,list) and len(actions)<=16,'actions_invalid')
    require(isinstance(entries,list) and 1<=len(entries)<=len(lease['tasks']),'entries_invalid')
    ids=[e.get('task_id') for e in entries];require(ids==lease['task_order'][:len(entries)],'task_order_mismatch')
    bodies={};last_count=0;last_end=0
    for e in entries:
        require(set(e)=={'task_id','begin','end'} and type(e['begin']) is int and type(e['end']) is int and e['begin']==last_end and e['begin']<=e['end']<=len(actions),'entry_range_invalid');last_end=e['end']
    require(last_end==len(actions),'unused_actions')
    for n,a in enumerate(actions):
        required={'method','url','at','result','count','evidence','deadlines'}
        require(set(a) in [required|{'page'},required|{'reason'},required|{'reason','page'}] and a['method'] in {'get','rendered'} and valid_url(a['url']),'action_schema')
        require(moment(m['started_at'])<=moment(a['at'])<=moment(m['ended_at']),'action_time_invalid')
        require(type(a['count']) is int and last_count<=a['count']<=last_count+3,'request_count_invalid');last_count=a['count']
        h=a['evidence'];require(set(h)=={'scope','started_at','response_status_counts','exception_counts','failure_observations','failure_samples','omitted_failure_samples'},'http_evidence_schema')
        total=0
        for category in ['response_status_counts','exception_counts']:
            require(set(h[category])=={'http','browser'},'http_method_invalid')
            for values in h[category].values():
                require(all(type(v) is int and v>=0 for v in values.values()),'http_count_invalid');total+=sum(values.values())
        require(total==a['count'],'http_count_mismatch')
        require(set(a['deadlines'])=={'retry_after','transport_retry_after','access_retry_after'},'deadline_schema')
        require(a['result'] in {'response','error'},'action_result_invalid')
        if a['result']=='error':require(a.get('reason') in ERRORS or bool(re.fullmatch(r'(access|transport)_retry_later:[A-Za-z0-9_]{1,40}',a.get('reason',''))),'error_reason_invalid')
        else:require('page' in a and 'reason' not in a,'response_missing')
        if 'page' not in a:continue
        p=a['page'];require(set(p)=={'url','observed_at','method','content_type','status','path','bytes','sha256'},'page_schema')
        require(p['path']==f'body-{n:02d}.bin' and p['path'] not in bodies,'body_path_invalid')
        require(valid_url(p['url']) and urlsplit(p['url']).hostname==urlsplit(a['url']).hostname,'redirect_host_mismatch')
        require(p['method']==('http' if a['method']=='get' else 'browser') and type(p['status']) is int and p['status'] in {200,404},'page_transport_invalid')
        require(moment(m['started_at'])<=moment(p['observed_at'])<=moment(a['at']),'page_time_invalid')
        require(isinstance(p['content_type'],str) and len(p['content_type'])<=256 and '\n' not in p['content_type'] and '\r' not in p['content_type'],'content_type_invalid')
        f=bundle/p['path'];require(not f.is_symlink() and type(p['bytes']) is int and 0<=p['bytes']<=2_000_000 and f.stat().st_size==p['bytes'],'body_size_or_path')
        body=f.read_bytes();require(sha(body)==p['sha256'],'body_hash_mismatch');bodies[p['path']]=body
    require({p.name for p in bundle.iterdir()}==set(bodies)|{'manifest.json','signature.bin'},'unexpected_artifact_files')
    replay=ReplayClient(actions,bodies);c=collector(r['state'],r['config'],lease['run_id'],replay);results=[]
    for e in entries:
        replay.end=e['end']
        try:c.attempt(e['task_id'])
        except InvalidTape as exc:raise Rejected(str(exc)) from None
        require(replay.index==e['end'],'unused_dependency')
        task=c.state['queue'].get(e['task_id'])
        results.append({'task_id':e['task_id'],'result':'retry' if task else 'complete','reason':task.get('last_error') if task else None})
    c.state.update(status='isolated_native_receipt',pending_count=len(c.state['queue']),cycle_complete=not c.state['queue'])
    receipt={'status':'accepted','lease_id':lease['id'],'manifest_sha256':sha(raw),'worker_id':lease['worker_id'],
             'run_id':lease['run_id'],'received_at':iso(now),'requests':replay.count,'results':results,
             'successes':sum(x['result']=='complete' for x in results),'failures':sum(x['result']=='retry' for x in results),
             'unattempted':len(lease['tasks'])-len(entries),'scheduled_credit':False,'formal_audit_added':False}
    result=deepcopy(r);result['state']=c.state;result['receipts'][lease['id']]=receipt
    return result,receipt

def receive(bundle,path,*,now=None,hook=None):
    now=now or utcnow()
    return transaction(path,lambda r:read_bundle(bundle,r,now),hook)

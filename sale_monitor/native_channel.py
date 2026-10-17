"""Fixed-code, outbound-only native receipt channel; isolated validation only.

The public mailbox carries an encrypted, short-lived artifact credential, never
a repository write token. Local code/source approval is separate from mailbox
contents. DPAPI protects locally generated keys for the current Windows user.
"""
from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from cryptography.exceptions import InvalidTag
from . import receipts as R
from .http import Client
from .models import utcnow
from .runner import task_order

REPOSITORY = 'chijimi33/pc-sale-monitor'
MAILBOX_BRANCH = 'native-worker-mailbox'
MAILBOX_PATH = 'requests/windows-yahoo.json'
WORKER = 'windows-yahoo-v1'
INFO = b'pc-sale-monitor.native-artifact-channel.v1'
MAX_PACKET = 100_000
MAX_ZIP = 34_000_000


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise R.Rejected('unexpected_redirect')


def request(url, *, data=None, token=None, method=None, limit=MAX_PACKET):
    headers = {'User-Agent': 'pc-sale-monitor-native-validation', 'Accept': 'application/vnd.github+json',
               'Cache-Control': 'no-cache'}
    if token:
        R.require(urlsplit(url).hostname == 'api.github.com', 'credential_host')
        headers['Authorization'] = 'Bearer ' + token
    if data is not None:
        data = R.encode(data)
        headers['Content-Type'] = 'application/json'
    with build_opener(NoRedirect()).open(Request(url, data=data, headers=headers, method=method), timeout=30) as response:
        raw = response.read(limit + 1)
    R.require(len(raw) <= limit, 'download_too_large')
    return raw


def mailbox_packet():
    # Raw branch URLs can serve a previous lease for several minutes. Resolve
    # the authoritative ref first, then read the immutable object. This public
    # API needs no stored GitHub credential; callers must respect its quota.
    ref = json.loads(request(f'https://api.github.com/repos/{REPOSITORY}/git/ref/heads/{MAILBOX_BRANCH}'))
    commit = ref['object']['sha']
    R.require(ref['object']['type']=='commit' and re.fullmatch('[0-9a-f]{40}',commit), 'mailbox_ref')
    return json.loads(request(f'https://raw.githubusercontent.com/{REPOSITORY}/{commit}/{MAILBOX_PATH}'))


def dpapi(raw, *, decrypt=False):
    R.require(os.name == 'nt', 'windows_dpapi_required')
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    crypt.CryptProtectData.argtypes = [ctypes.POINTER(Blob),wintypes.LPCWSTR,ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    crypt.CryptProtectData.restype = wintypes.BOOL
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob),ctypes.POINTER(wintypes.LPWSTR),ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(source), 'PC sale monitor worker', None, None, None, 1, ctypes.byref(target))
    R.require(ok, 'dpapi_failed')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def generate_keys(path):
    path = Path(path)
    R.require(not path.exists(), 'key_already_exists')
    signing = Ed25519PrivateKey.generate()
    exchange = X25519PrivateKey.generate()
    private = {'signing': signing.private_bytes_raw().hex(), 'exchange': exchange.private_bytes_raw().hex()}
    with path.open('xb') as output:
        output.write(dpapi(R.encode(private)))
        output.flush()
        os.fsync(output.fileno())
    return {'worker_id': WORKER, 'enabled': True, 'stores': ['yahoo'],
            'public_key_hex': signing.public_key().public_bytes_raw().hex(),
            'exchange_key_hex': exchange.public_key().public_bytes_raw().hex()}


def load_keys(path):
    path = Path(path)
    R.require(not path.is_symlink() and path.stat().st_size <= 4096, 'key_path_or_size')
    value = json.loads(dpapi(path.read_bytes(), decrypt=True))
    return (Ed25519PrivateKey.from_private_bytes(bytes.fromhex(value['signing'])),
            X25519PrivateKey.from_private_bytes(bytes.fromhex(value['exchange'])))


def derive(private, public, context):
    shared = private.exchange(X25519PublicKey.from_public_bytes(bytes.fromhex(public)))
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=hashlib.sha256(context).digest(), info=INFO).derive(shared)


def seal(public, credentials, metadata):
    ephemeral = X25519PrivateKey.generate()
    context = R.encode(metadata)
    nonce = os.urandom(12)
    encrypted = AESGCM(derive(ephemeral, public, context)).encrypt(nonce, R.encode(credentials), context)
    return {'metadata': deepcopy(metadata), 'ephemeral': ephemeral.public_key().public_bytes_raw().hex(),
            'nonce': nonce.hex(), 'ciphertext': base64.b64encode(encrypted).decode()}


def validate_metadata(metadata, policy, *, now=None):
    now = now or utcnow()
    R.require(set(metadata) == {'version','repository','worker_id','source_sha','data_sha','runtime_sha256','lease'}, 'packet_schema')
    R.require(metadata['version'] == 1 and metadata['repository'] == REPOSITORY and metadata['worker_id'] == WORKER, 'packet_scope')
    R.require(policy['enabled'] is True and policy['source_sha'] == metadata['source_sha'], 'source_not_approved')
    R.require(re.fullmatch('[0-9a-f]{40}', metadata['source_sha']) and re.fullmatch('[0-9a-f]{40}', metadata['data_sha']), 'source_binding')
    R.require(metadata['runtime_sha256'] == policy['runtime_sha256'] == R.runtime_hash(), 'source_runtime_mismatch')
    lease = metadata['lease']
    R.require(lease['worker_id'] == WORKER and lease['store'] == 'yahoo' and lease['scope'] == R.SCOPE, 'lease_scope')
    for key in ['source_sha','data_sha','runtime_sha256']:
        R.require(lease[key] == metadata[key], 'lease_binding')
    R.require(re.fullmatch(r'isolated-native-github-[0-9]+-[0-9]+', lease['run_id']), 'isolated_github_run_required')
    R.require(re.fullmatch('[0-9a-f]{32}', lease['id']), 'lease_id')
    created, expires = R.moment(lease['created_at']), R.moment(lease['expires_at'])
    R.require(created <= now <= expires and 0 < (expires-created).total_seconds() <= 600, 'lease_expired')
    R.require(1 <= len(lease['task_order']) <= 4 and set(lease['task_order']) == set(lease['tasks']) and len(set(lease['task_order'])) == len(lease['task_order']), 'lease_tasks')
    R.require(all(R.supported_task('yahoo', task) and (not task.get('url') or R.valid_url(task['url'])) for task in lease['tasks'].values()), 'unsupported_task')


def open_packet(packet, private, policy, *, now=None):
    R.require(set(packet) == {'metadata','ephemeral','nonce','ciphertext'} and len(R.encode(packet)) <= MAX_PACKET, 'packet_schema')
    metadata = packet['metadata']
    validate_metadata(metadata, policy, now=now)
    try:
        nonce = bytes.fromhex(packet['nonce'])
        R.require(len(nonce) == 12, 'nonce_size')
        raw = AESGCM(derive(private, packet['ephemeral'], R.encode(metadata))).decrypt(
            nonce, base64.b64decode(packet['ciphertext'], validate=True), R.encode(metadata))
        credentials = json.loads(raw)
    except (InvalidTag, ValueError, KeyError):
        raise R.Rejected('encrypted_packet_invalid') from None
    R.require(set(credentials) == {'runtime_token','results_url'}, 'credential_schema')
    token = credentials['runtime_token']
    R.require(isinstance(token, str) and 1 <= len(token) <= 16_384 and not re.search(r'\s', token), 'credential_format')
    parsed = urlsplit(credentials['results_url'])
    R.require(parsed.scheme == 'https' and parsed.hostname and parsed.hostname.endswith('.actions.githubusercontent.com')
              and not parsed.username and not parsed.password and parsed.port in (None,443)
              and parsed.path in ('','/') and not parsed.query and not parsed.fragment, 'artifact_service_host')
    return metadata, credentials


def safe_extract(raw, target):
    """Validate every member before creating any files; no extractall."""
    R.require(len(raw) <= MAX_ZIP, 'archive_size')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        files = archive.infolist()
        names = [entry.filename for entry in files]
        R.require(2 <= len(files) <= 18 and len(set(names)) == len(names), 'archive_members')
        R.require({'manifest.json','signature.bin'} <= set(names), 'archive_missing_manifest')
        R.require(sum(entry.file_size for entry in files) <= 32_500_064, 'archive_expanded_size')
        for entry in files:
            name = entry.filename
            R.require(name in {'manifest.json','signature.bin'} or re.fullmatch(r'body-[0-9]{2}\.bin', name), 'archive_path')
            R.require(not entry.is_dir() and ((entry.external_attr >> 16) & 0o170000) in (0,0o100000), 'archive_file_type')
            maximum = 500_000 if name == 'manifest.json' else 64 if name == 'signature.bin' else 2_000_000
            R.require(entry.file_size <= maximum and not entry.flag_bits & 1, 'archive_entry_size')
        values = {entry.filename: archive.read(entry) for entry in files}
    target = Path(target)
    target.mkdir()
    for name, value in values.items():
        (target/name).write_bytes(value)


def publish_packet(packet):
    R.require(os.environ.get('GITHUB_REPOSITORY') == REPOSITORY, 'repository_scope')
    token = os.environ['GITHUB_TOKEN']
    url = f'https://api.github.com/repos/{REPOSITORY}/contents/{MAILBOX_PATH}'
    previous = None
    try:
        previous = json.loads(request(url+'?ref='+MAILBOX_BRANCH, token=token))['sha']
    except HTTPError as error:
        if error.code != 404:
            raise
    body = {'message':'Issue isolated native receipt lease', 'branch':MAILBOX_BRANCH,
            'content':base64.b64encode(R.encode(packet)+b'\n').decode()}
    if previous:
        body['sha'] = previous
    result = json.loads(request(url, token=token, data=body, method='PUT'))
    return result['commit']['sha']


def prepare(args):
    root = args.output
    root.mkdir()
    config = json.loads(Path('config/sources.json').read_bytes())
    state = json.loads((args.state/'state/stores/yahoo.json').read_bytes())
    worker = json.loads(Path('config/native-worker.json').read_bytes())
    source = os.environ['SOURCE_COMMIT']
    data_sha = subprocess.check_output(['git','rev-parse','HEAD'], cwd=args.state, text=True).strip()
    run_id = f'isolated-native-github-{os.environ["GITHUB_RUN_ID"]}-{os.environ["GITHUB_RUN_ATTEMPT"]}'
    value = R.receiver(state, config, source, data_sha, run_id, {WORKER:worker})
    path = root/'receiver.json'
    path.write_bytes(R.encode(value))
    c = R.collector(state, config, run_id, Client())
    ready, _ = R.runnable(c.state['queue'], c.attempted, c.cfg, c.client, {}, utcnow().timestamp())
    # A two-lane integration sample, not the production scheduler. No cursor
    # movement or scheduled credit is claimed by this isolated workflow.
    selected = []
    for comparison in (True,False):
        choices = [(key,task) for key,task in ready if task['type']=='yahoo' and task.get('start',1)==1
                   and (task.get('kind')=='comparison') == comparison]
        if choices:
            selected.append(min(choices,key=lambda pair:task_order(pair[1]))[0])
    R.require(bool(selected), 'no_probe_tasks')
    lease = R.issue(path, WORKER, selected)
    metadata = {'version':1,'repository':REPOSITORY,'worker_id':WORKER,'source_sha':source,
                'data_sha':data_sha,'runtime_sha256':R.runtime_hash(),'lease':lease}
    credentials = json.load(sys.stdin)
    packet = seal(worker['exchange_key_hex'], credentials, metadata)
    (root/'request.json').write_bytes(R.encode(packet)+b'\n')
    commit = publish_packet(packet)
    result = {'mailbox_commit':commit,'lease_id':lease['id'],'expires_at':lease['expires_at'],
              'artifact_name':'native-receipt-'+lease['id'],'source_sha':source,'data_sha':data_sha,
              'production_state_writes':0,'scheduled_credit':False}
    (root/'issued.json').write_bytes(R.encode(result)+b'\n')
    print(json.dumps(result))


def work(args):
    policy = json.loads(args.policy.read_bytes())
    signing, exchange = load_keys(args.key)
    R.require(signing.public_key().public_bytes_raw().hex()==policy['public_key_hex'] and
              exchange.public_key().public_bytes_raw().hex()==policy['exchange_key_hex'], 'registered_key_mismatch')
    packet = mailbox_packet()
    metadata, credentials = open_packet(packet, exchange, policy)
    lease = metadata['lease']
    config = json.loads(Path('config/sources.json').read_bytes())
    state = json.loads(request(f'https://raw.githubusercontent.com/{REPOSITORY}/{metadata["data_sha"]}/state/stores/yahoo.json', limit=12_000_000))
    R.require(lease['config_sha256']==R.sha(R.encode(config)) and lease['base_state_sha256']==R.sha(R.encode(state)), 'worker_input_mismatch')
    R.require(all(state['queue'].get(key)==task for key,task in lease['tasks'].items()), 'lease_task_mismatch')
    args.output.mkdir()
    (args.output/'request.json').write_bytes(R.encode(packet)+b'\n')
    result = R.produce(args.output/'bundle', lease, state, config, Client(), signing)
    R.require(utcnow() < R.moment(lease['expires_at']), 'lease_expired_before_upload')
    child_env = {key:value for key,value in os.environ.items() if key.upper() in {'SYSTEMROOT','PATH','TEMP','TMP','HOME','USERPROFILE'}}
    child_env.update(ACTIONS_RUNTIME_TOKEN=credentials['runtime_token'], ACTIONS_RESULTS_URL=credentials['results_url'],
                     GITHUB_WORKSPACE=str(Path(result['checkpoint']).resolve()), GITHUB_SERVER_URL='https://github.com',
                     ACTIONS_ARTIFACT_UPLOAD_CONCURRENCY='1')
    process = subprocess.run([args.node, str(args.uploader), 'upload', result['checkpoint'], 'native-receipt-'+lease['id']],
                             env=child_env, capture_output=True, text=True, encoding='utf-8', timeout=180)
    # SDK diagnostics may contain temporary signed URLs. Persist only our own
    # small result object, never stdout/stderr or the child environment.
    R.require(process.returncode == 0, 'artifact_upload_failed')
    lines = [line.removeprefix('NATIVE_ARTIFACT_RESULT=') for line in process.stdout.splitlines() if line.startswith('NATIVE_ARTIFACT_RESULT=')]
    R.require(len(lines)==1, 'artifact_upload_result')
    upload = json.loads(lines[0])
    R.require(set(upload)=={'id','size','digest'} and type(upload['id']) is int, 'artifact_upload_result')
    summary = {'lease_id':lease['id'],'requests':result['requests'],'manifest_sha256':result['manifest_sha256'],
               'upload':upload,'scheduled_credit':False,'production_state_writes':0}
    (args.output/'worker-result.json').write_bytes(R.encode(summary)+b'\n')
    print(json.dumps(summary))


def receive(args):
    paths = list((args.output/'download').iterdir())
    R.require(len(paths)==1 and paths[0].is_file() and not paths[0].is_symlink() and paths[0].stat().st_size<=MAX_ZIP, 'archive_download')
    safe_extract(paths[0].read_bytes(), args.output/'verified-bundle')
    result = R.receive(args.output/'verified-bundle', args.output/'receiver.json')
    result.update(production_state_writes=0, formal_audit_added=False)
    (args.output/'received.json').write_bytes(R.encode(result)+b'\n')
    print(json.dumps(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['generate','prepare','work','receive'])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--key', type=Path)
    parser.add_argument('--policy', type=Path)
    parser.add_argument('--state', type=Path, default=Path('state-repo'))
    parser.add_argument('--node', default='node')
    parser.add_argument('--uploader', type=Path, default=Path('tools/native-channel/artifact.mjs'))
    args = parser.parse_args()
    if args.command=='generate':
        args.output.write_bytes(R.encode(generate_keys(args.key))+b'\n')
    else:
        try:
            {'prepare':prepare,'work':work,'receive':receive}[args.command](args)
        except R.Rejected as error:
            print('native_channel_rejected:'+str(error), file=sys.stderr)
            raise SystemExit(1)
        except Exception as error:
            # No exception text: HTTP/SDK errors can include credential URLs.
            print('native_channel_failed:'+type(error).__name__, file=sys.stderr)
            raise SystemExit(1)


if __name__ == '__main__':
    main()

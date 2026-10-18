import {DefaultArtifactClient} from '@actions/artifact';
import fs from 'node:fs/promises';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const artifact = new DefaultArtifactClient();
function python(args, input) {
  const result = spawnSync(process.env.NATIVE_PYTHON || 'python', ['-B','-m','sale_monitor.native_operations',...args],
    {input:input ? JSON.stringify(input) : undefined, encoding:'utf8', maxBuffer:1000000});
  if (result.status !== 0) throw new Error('native_session_command_failed');
  return JSON.parse(result.stdout.trim());
}

export async function findOne(client, name) {
  const result = await client.listArtifacts({latest:false});
  const matches = result.artifacts.filter(item => item.name === name);
  if (matches.length > 1) throw new Error('duplicate_artifact_name');
  return matches[0];
}

export async function download(client, item, directory, maximum) {
  if (!Number.isSafeInteger(item.id) || !Number.isSafeInteger(item.size) || item.size < 0 || item.size > maximum ||
      !/^(sha256:)?[a-f0-9]{64}$/.test(item.digest || '')) throw new Error('artifact_size_or_digest');
  const expectedHash = item.digest.startsWith('sha256:') ? item.digest : 'sha256:'+item.digest;
  const result = await client.downloadArtifact(item.id, {path:directory,skipDecompress:true,expectedHash});
  if (result.digestMismatch) throw new Error('artifact_digest_mismatch');
  return {id:item.id,size:item.size,digest:expectedHash};
}

export async function controller() {
  const root = path.resolve('native-session');
  const key = path.join(process.env.RUNNER_TEMP,'native-controller-'+process.env.GITHUB_RUN_ID+'-'+process.env.GITHUB_RUN_ATTEMPT+'.key');
  const common = ['--root',root,'--controller-key',key];
  const isolated = process.env.NATIVE_SESSION_MODE === 'isolated';
  const start = python(['prepare',...common,...(isolated ? ['--isolated'] : []),
    '--seconds',process.env.NATIVE_SESSION_SECONDS || '2100','--batches',process.env.NATIVE_SESSION_BATCHES || '200'],
    {runtime_token:process.env.ACTIONS_RUNTIME_TOKEN,results_url:process.env.ACTIONS_RESULTS_URL});
  console.log(JSON.stringify(start));
  let received = 0;
  try {
    while (Date.now()+60000 < Date.parse(start.collection_deadline)) {
      const next = python(['next',...common]);
      if (next.status === 'finished') {console.log(JSON.stringify(next));break;}
      if (next.status === 'waiting') {await new Promise(resolve => setTimeout(resolve,next.seconds*1000));continue;}
      if (next.status !== 'issued') throw new Error('unexpected_batch_status');
      await artifact.uploadArtifact(next.input_name,
        ['request.json','signature.bin'].map(name => path.join(next.directory,name)),next.directory,
        {retentionDays:7,compressionLevel:6});
      console.log(JSON.stringify({status:'input_uploaded',sequence:next.sequence,lease_id:next.lease_id}));
      let found;
      const until = Math.min(Date.parse(next.expires_at)-15000,Date.parse(start.collection_deadline));
      while (Date.now() < until) {
        found = await findOne(artifact,next.receipt_name);
        if (found) break;
        await new Promise(resolve => setTimeout(resolve,8000));
      }
      if (!found) throw new Error('native_receipt_timeout');
      const directory = path.join(root,'download-'+next.lease_id);
      const proof = await download(artifact,found,directory,34000000);
      const result = python(['receive',...common,'--download',directory]);
      await fs.writeFile(path.join(root,'receipts',next.lease_id+'-artifact.json'),JSON.stringify(proof));
      received++;
      console.log(JSON.stringify({sequence:next.sequence,...result}));
    }
    console.log(JSON.stringify({status:'session_complete',accepted_batches:received}));
  } finally {
    // Also used on transfer failure; this does not trust worker-produced state.
    const ended=python(['finish',...common]);
    console.log(JSON.stringify(ended));
    await artifact.uploadArtifact(ended.name,['request.json','signature.bin'].map(name=>path.join(ended.directory,name)),
      ended.directory,{retentionDays:7,compressionLevel:6});
  }
}

export async function input(client, request) {
  if (!/^native-input-[a-f0-9]{32}-[0-9]{4}$/.test(request.name)) throw new Error('input_name');
  if (request.end_name) {
    if (request.end_name!=='native-end-'+request.name.slice(13,45)) throw new Error('completion_name');
    const ended=await findOne(client,request.end_name);
    if (ended) return {found:true,finished:true,...await download(client,ended,request.directory,100000)};
  }
  const found = await findOne(client,request.name);
  if (!found) return {found:false};
  return {found:true,...await download(client,found,request.directory,20100000)};
}

export async function receipt(client, request, compare=python) {
  if (!/^native-receipt-[a-f0-9]{32}$/.test(request.name)) throw new Error('receipt_name');
  const root = await fs.realpath(request.directory);
  const entries = await fs.readdir(root,{withFileTypes:true});
  if (entries.length < 2 || entries.length > 18 || entries.some(entry => !entry.isFile() ||
      !/^(manifest\.json|signature\.bin|body-[0-9]{2}\.bin)$/.test(entry.name))) throw new Error('receipt_files');
  async function recover(found) {
    const directory = request.verification_root+'-download';
    const result = await download(client,found,directory,34000000);
    const files = await fs.readdir(directory);
    if (files.length !== 1) throw new Error('receipt_archive_files');
    compare(['compare-upload'],{directory:root,archive:path.join(directory,files[0]),verification_root:request.verification_root});
    return {...result,recovered:true};
  }
  const prior = await findOne(client,request.name);
  if (prior) return recover(prior);
  try {
    const result = await client.uploadArtifact(request.name,entries.map(entry=>path.join(root,entry.name)),root,
      {retentionDays:7,compressionLevel:6});
    return {id:result.id,size:result.size,digest:result.digest,recovered:false};
  } catch {
    // A lost reply can mean upload succeeded. Do not delete or overwrite it.
    const found = await findOne(client,request.name);
    if (!found) throw new Error('receipt_upload_failed');
    return recover(found);
  }
}

if (process.argv[1] && path.resolve(process.argv[1])===fileURLToPath(import.meta.url)) {
  try {
    let text='';for await (const chunk of process.stdin) text+=chunk;
    const request=JSON.parse(text);
    const result = process.argv[2]==='input' ? await input(artifact,request) :
      process.argv[2]==='receipt' ? await receipt(artifact,request) : (()=>{throw new Error('command');})();
    console.log('NATIVE_SESSION_RESULT='+JSON.stringify(result));
  } catch {
    console.error('native_session_transfer_failed');process.exitCode=1;
  }
}

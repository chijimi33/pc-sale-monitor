import {DefaultArtifactClient} from '@actions/artifact';
import fs from 'node:fs/promises';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const artifact = new DefaultArtifactClient();
function python(args, input) {
  const result = spawnSync('python', ['-B', '-m', 'sale_monitor.native_channel', ...args],
    {input: input ? JSON.stringify(input) : undefined, encoding:'utf8', maxBuffer:200000});
  if (result.status !== 0) throw new Error('native_controller_failed');
  return JSON.parse(result.stdout.trim());
}

export async function controller() {
  if (!process.env.ACTIONS_RUNTIME_TOKEN || !process.env.ACTIONS_RESULTS_URL) throw new Error('runtime_credentials_missing');
  const output = path.resolve('native-validation');
  const issued = python(['prepare', '--output', output], {
    runtime_token:process.env.ACTIONS_RUNTIME_TOKEN, results_url:process.env.ACTIONS_RESULTS_URL});
  console.log(JSON.stringify(issued));
  const end = Date.parse(issued.expires_at) - 30000;
  let found;
  while (Date.now() < end) {
    const list = await artifact.listArtifacts({latest:true});
    found = list.artifacts.find(item => item.name === issued.artifact_name);
    if (found) break;
    await new Promise(resolve => setTimeout(resolve, 15000));
  }
  if (!found) throw new Error('native_receipt_timeout');
  if (found.size > 34000000 || !found.digest) throw new Error('artifact_size_or_digest');
  const expectedHash = found.digest.startsWith('sha256:') ? found.digest : 'sha256:'+found.digest;
  const download = await artifact.downloadArtifact(found.id, {
    path:path.join(output,'download'), skipDecompress:true, expectedHash});
  if (download.digestMismatch) throw new Error('artifact_digest_mismatch');
  await fs.writeFile(path.join(output,'artifact.json'), JSON.stringify({id:found.id,size:found.size,digest:expectedHash}));
  console.log(JSON.stringify(python(['receive','--output',output])));
}

async function upload(directory, name) {
  if (!/^native-receipt-[a-f0-9]{32}$/.test(name)) throw new Error('artifact_name');
  const root = await fs.realpath(directory);
  const entries = await fs.readdir(root, {withFileTypes:true});
  if (entries.length < 2 || entries.length > 18 || entries.some(entry => !entry.isFile() ||
    !/^(manifest\.json|signature\.bin|body-[0-9]{2}\.bin)$/.test(entry.name))) throw new Error('artifact_files');
  const result = await artifact.uploadArtifact(name, entries.map(entry => path.join(root, entry.name)), root,
    {retentionDays:7, compressionLevel:6});
  console.log('NATIVE_ARTIFACT_RESULT='+JSON.stringify({id:result.id,size:result.size,digest:result.digest}));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    if (process.argv[2] !== 'upload') throw new Error('command');
    await upload(process.argv[3], process.argv[4]);
  } catch {
    console.error('native_artifact_upload_failed');
    process.exitCode = 1;
  }
}

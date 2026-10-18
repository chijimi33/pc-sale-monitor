import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {input,receipt,findOne,download} from './session.mjs';

const name='native-receipt-'+'a'.repeat(32);
const artifact={id:42,name,size:123,digest:'b'.repeat(64)};
async function fixture(t) {
  const parent=await fs.realpath(os.tmpdir());
  const root=await fs.mkdtemp(path.join(parent,'native-sdk-'));
  t.after(async()=>{
    assert.equal(path.dirname(root),parent);assert.ok(path.basename(root).startsWith('native-sdk-'));
    await fs.rm(root,{recursive:true});
  });
  const directory=path.join(root,'receipt');await fs.mkdir(directory);
  await fs.writeFile(path.join(directory,'manifest.json'),'{}');
  await fs.writeFile(path.join(directory,'signature.bin'),Buffer.alloc(64));
  return {name,directory,verification_root:path.join(root,'verification')};
}
function client(prior=[], options={}) {
  let artifacts=[...prior],uploads=0,downloads=0;
  return {
    async listArtifacts(){return {artifacts};},
    async uploadArtifact(){uploads++;artifacts.push(artifact);if(options.lostReply)throw new Error('lost');return artifact;},
    async downloadArtifact(id,args){
      downloads++;assert.equal(id,42);assert.equal(args.skipDecompress,true);assert.equal(args.expectedHash,'sha256:'+'b'.repeat(64));
      await fs.mkdir(args.path,{recursive:true});await fs.writeFile(path.join(args.path,'artifact.zip'),'synthetic');
      return {digestMismatch:options.badDigest||false};
    },
    counts(){return {uploads,downloads};}
  };
}
test('lost upload response recovers only after byte comparison',async t=>{
  const args=await fixture(t),sdk=client([],{lostReply:true});let compared=0;
  const result=await receipt(sdk,args,(command,value)=>{
    assert.deepEqual(command,['compare-upload']);assert.equal(value.directory,args.directory);compared++;
  });
  assert.equal(result.recovered,true);assert.equal(compared,1);assert.deepEqual(sdk.counts(),{uploads:1,downloads:1});
});
test('existing identical result is downloaded and verified without uploading',async t=>{
  const args=await fixture(t),sdk=client([artifact]);let compared=0;
  const result=await receipt(sdk,args,()=>{compared++;});
  assert.equal(result.id,42);assert.equal(compared,1);assert.deepEqual(sdk.counts(),{uploads:0,downloads:1});
});
test('same name with different contents is never overwritten',async t=>{
  const args=await fixture(t),sdk=client([artifact]);
  await assert.rejects(()=>receipt(sdk,args,()=>{throw new Error('existing_artifact_conflict');}),/existing_artifact_conflict/);
  assert.deepEqual(sdk.counts(),{uploads:0,downloads:1});
});
test('duplicate artifact names are rejected rather than selecting the newest',async()=>{
  await assert.rejects(()=>findOne(client([artifact,{...artifact,id:43}]),name),/duplicate_artifact_name/);
});
test('archive digest mismatch prevents comparison and adoption',async t=>{
  const args=await fixture(t),sdk=client([artifact],{badDigest:true});
  await assert.rejects(()=>receipt(sdk,args,()=>{throw new Error('must_not_compare');}),/artifact_digest_mismatch/);
});
test('oversized artifact is rejected before download',async t=>{
  const args=await fixture(t),sdk=client();
  await assert.rejects(()=>download(sdk,{...artifact,size:34000001},args.directory,34000000),/artifact_size_or_digest/);
  assert.equal(sdk.counts().downloads,0);
});
test('input is selected by exact session and sequence',async t=>{
  const args=await fixture(t),sdk=client([artifact]);
  const result=await input(sdk,{name:'native-input-'+'a'.repeat(32)+'-0001',directory:args.directory});
  assert.deepEqual(result,{found:false});assert.equal(sdk.counts().downloads,0);
});
test('completion from a different session cannot stop a worker',async t=>{
  const args=await fixture(t),sdk=client();
  await assert.rejects(()=>input(sdk,{name:'native-input-'+'a'.repeat(32)+'-0001',end_name:'native-end-'+'b'.repeat(32),directory:args.directory}),/completion_name/);
});

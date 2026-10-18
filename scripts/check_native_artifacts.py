"""Synthetic GitHub matrix/output integration check; no retailer or state writes."""
import argparse
import json
import os
from pathlib import Path

from sale_monitor.models import STORES
from native_artifact_ids import selected


def expected(store):
    return {'store':store,'synthetic':True,'source_sha':os.environ['SOURCE_COMMIT'],
            'run_id':os.environ['GITHUB_RUN_ID']+'-'+os.environ['GITHUB_RUN_ATTEMPT']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['fixture','verify']);args=parser.parse_args()
    if os.environ['GITHUB_EVENT_NAME']!='pull_request':raise ValueError('isolated_pr_required')
    if args.command=='fixture':
        store=os.environ['STORE']
        if store not in STORES:raise ValueError('unknown_store')
        root=Path('fixture/stores');root.mkdir(parents=True)
        (root/(store+'.json')).write_text(json.dumps(expected(store))+'\n',encoding='utf-8')
        return
    regular=json.loads(os.environ['COLLECTION_OUTPUTS']);yahoo=json.loads(os.environ['YAHOO_OUTPUTS'])
    ids=selected(regular,yahoo)
    if set(regular)!=set(STORES)-{'yahoo'} or set(yahoo)!={'yahoo'} or len(ids)!=len(STORES):
        raise ValueError('matrix_outputs_incomplete')
    paths=list(Path('incoming').rglob('*'))
    actual={p.relative_to('incoming').as_posix() for p in paths if p.is_file()}
    if actual!={'stores/'+store+'.json' for store in STORES}:raise ValueError('artifact_selection_scope')
    for store in STORES:
        if json.loads(Path('incoming/stores',store+'.json').read_bytes())!=expected(store):
            raise ValueError('artifact_source_or_run_mismatch')
    proof={'status':'verified','stores':list(STORES),'artifact_ids':ids,
           'matrix_outputs_complete':True,'untrusted_artifact_excluded':True,
           'source_sha':os.environ['SOURCE_COMMIT'],'run_id':expected('yahoo')['run_id'],
           'synthetic':True,'retailer_requests':0,'production_state_writes':0,'scheduled_credit':False}
    Path('artifact-selection-proof.json').write_text(json.dumps(proof,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(proof))


if __name__=='__main__':main()

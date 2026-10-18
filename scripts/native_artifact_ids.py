"""Bind publisher inputs to trusted workflow job outputs, never a name glob."""
import argparse
import json
import os
from pathlib import Path
import re
from sale_monitor.models import STORES


def selected(regular, yahoo):
    if not isinstance(regular,dict) or not isinstance(yahoo,dict):raise ValueError('artifact_outputs_invalid')
    if set(regular)-set(STORES)|({'yahoo'}&set(regular)) or set(yahoo)-{'yahoo'}:raise ValueError('artifact_store_scope')
    values={**regular,**yahoo};ids=[]
    for store in STORES:
        value=values.get(store,'')
        if value=='':continue
        if not isinstance(value,str) or not re.fullmatch(r'[1-9][0-9]{0,19}',value):raise ValueError('artifact_id_invalid')
        if value in ids:raise ValueError('artifact_id_reused')
        ids.append(value)
    return ids


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=['record','select','mode']);args=parser.parse_args()
    if args.command=='record':
        store=os.environ['STORE'];value=os.environ['ARTIFACT_ID']
        if store not in STORES or store=='yahoo' or not re.fullmatch(r'[1-9][0-9]{0,19}',value):raise ValueError('artifact_binding_invalid')
        output=f'{store}={value}'
    elif args.command=='mode':
        config=json.loads(Path('config/sources.json').read_bytes())
        output='native='+str(config['stores']['yahoo'].get('native_collection') is True).lower()
    else:
        ids=selected(json.loads(os.environ.get('COLLECTION_OUTPUTS','{}')),json.loads(os.environ.get('YAHOO_OUTPUTS','{}')))
        output='ids='+','.join(ids)
    with open(os.environ['GITHUB_OUTPUT'],'a',encoding='utf-8') as target:target.write(output+'\n')


if __name__=='__main__':main()

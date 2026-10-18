"""Exercise publisher on a disposable local copy; never write monitor-data."""
import json
import os
from pathlib import Path
from sale_monitor import receipts as R
from sale_monitor.reporting import checkpoint

root=Path('native-session')
control=json.loads((root/'control.json').read_bytes())
receiver=json.loads((root/'receiver.json').read_bytes())
trust=json.loads(Path('native-trust.json').read_bytes())
session=control['session']
R.require(os.environ.get('GITHUB_EVENT_NAME')=='pull_request','isolated_pr_required')
R.require(len(receiver['receipts'])>=2,'multiple_batches_required')
args=dict(native_required=True,native_trust=trust,source_sha=os.environ['SOURCE_COMMIT'],data_sha=session['data_sha'])
first=checkpoint(Path('publication-check'),Path('outgoing'),os.environ['RUN_ID'],**args)
R.require(first['accepted_stores']==['yahoo'] and not first['rejected_stores'],'publisher_rejected')
saved=Path('publication-check/stores/yahoo.json').read_bytes()
second=checkpoint(Path('publication-check'),Path('outgoing'),os.environ['RUN_ID'],**args)
R.require(second==first and Path('publication-check/stores/yahoo.json').read_bytes()==saved,'publisher_duplicate_changed')
result={'status':'verified','accepted_batches':len(receiver['receipts']),'publisher':first,
        'duplicate_byte_stable':True,'production_state_writes':0,'scheduled_credit':False,'formal_audit_added':False}
(root/'publication-check.json').write_bytes(R.encode(result)+b'\n')
print(json.dumps(result))

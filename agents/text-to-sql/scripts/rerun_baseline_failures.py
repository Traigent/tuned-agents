"""Regenerate the baseline's SQL for every question it got wrong on the held-out set.
The archived held-out run stored correctness per question, not the SQL, so this makes a fresh
(paid, ~$0.005) Vertex AI call per failed question and prints question / gold / prediction.
Run from the repo root after scripts/get_spider.sh. Needs Vertex credentials (see README).
usage: TRAIGENT_COST_APPROVED=true python scripts/rerun_baseline_failures.py > failures.json
"""
import json,sys,os
sys.path.insert(0,'agent')
import paid_guard
try:
    paid_guard.arm_paid()          # approval + cap + spend tracking, before any provider call
except paid_guard.Refused as exc:
    sys.exit(f'REFUSING to run: {exc}')
import traigent
from traigent.config.context import set_config
import agent as A
from sqlutil import exec_match, run_sql
cfg=json.load(open('results/candidates.json'))['baseline']
set_config(dict(cfg))
H={e['example_id']:e for e in json.load(open('results/heldout_baseline.json'))['per_example']}
rows={json.loads(l)['example_id']:json.loads(l) for l in open('data/heldout.jsonl')}
bad=[i for i,e in H.items() if e['acc']!=1.0]
others={l:{e['example_id']:e['acc'] for e in json.load(open(f'results/heldout_{l}.json'))['per_example']} for l in ('best_other','cheap_strong','next_strongest')}
out=[]
for i in bad:
    r=rows[i]; A.CALL.reset()
    try:
        pred=A.generate_sql(r['input'],r['db_id'])
    except Exception as exc:
        sys.exit(f'ABORT: {type(exc).__name__}: {exc}. {paid_guard.report()}')
    acc,st=exec_match(pred,r['output'],r['db_id'])
    out.append(dict(id=i,db=r['db_id'],q=r['input'],gold=r['output'],pred=pred,rerun_acc=acc,rerun_status=st,archived_status=H[i]['status'],others_ok=[l for l,d in others.items() if d[i]==1.0]))
json.dump(out,sys.stdout,indent=1)
print('[guard] '+paid_guard.report(),file=sys.stderr)
print(f'spend recorded by the guard: ${paid_guard.SPEND["total"]:.6f}',file=sys.stderr)
print(f'{len(out)} archived failures, {sum(o["rerun_acc"]!=1 for o in out)} fail again on re-run',file=sys.stderr)

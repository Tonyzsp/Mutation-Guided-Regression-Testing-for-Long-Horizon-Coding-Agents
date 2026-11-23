"""Merge, review, validate, and freeze Vanilla v2c recovery candidates."""
from __future__ import annotations
import argparse, hashlib, json, shutil, subprocess, sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/'artifacts/results/rq1_vanilla_recovery_v2c'
OUT=ROOT/'artifacts/results/rq1_vanilla_recovery_v2c_merged'
PKG=ROOT/'data/checkpoints/checkpoint_packages_rq1_full_36x1'
BASE=ROOT/'artifacts/results/rq1_full_36x1/c1_base_audit_current_sanitized_20261116/c1_base_suite_audit.json'
BUD=ROOT/'config/generation_budget_rq1_recovery_v2c_composite.json'
STUDY=ROOT/'config/rq1_recovery_v2c_composite_study_budget.json'
TASKS=("cfgpipe","circuit_eval","dag_execution","database_migration","datagate","dynamic_buffer","dynamic_config_service_api","env_manager","eve_industry","eve_market_tools","eve_route_planner","execution_server","file_merger","forge","l2m","layered_config_synthesizer","log_query","meshctl","metric_transform_lang","migrate_configs","mocked_http","mvvault","pwd_manager","recli","sith","test_translator","textdrop","trajectory_api","xjq")
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def read(p): return json.loads(p.read_text(encoding='utf-8'))
def merge(task):
 d=OUT/task/'C1'/'vanilla_llm'; d.mkdir(parents=True,exist_ok=True)
 if (d/'generation_run.json').exists(): return d
 slots=[SRC/task/'C1'/'vanilla_llm'/f'call_{i:02d}' for i in (1,2)]
 runs=[read(s/'generation_run.json') for s in slots]; ins=[read(s/'input_manifest.json') for s in slots]
 tests=[]; seen={}; provenance=[]
 for i,(s,r) in enumerate(zip(slots,runs),1):
  for c in read(s/'candidate_response.json').get('tests',[]):
   c=dict(c); name=c['relative_path']; content=c['content']; key=(name,hashlib.sha256(content.encode()).hexdigest())
   if name in seen and seen[name]!=content:
    c['relative_path']=f'test_call{i}_{Path(name).stem[5:]}.py'
   elif key in {(x['relative_path'],hashlib.sha256(x['content'].encode()).hexdigest()) for x in tests}: continue
   seen[c['relative_path']]=content; tests.append(c); provenance.append({'slot':i,'original_path':name,'merged_path':c['relative_path'],'content_sha256':hashlib.sha256(content.encode()).hexdigest(),'generation_run_sha256':sha(s/'generation_run.json')})
 assert len(tests)<=8
 inp=dict(ins[0]); inp['budget_config']={'path':'config/generation_budget_rq1_recovery_v2c_composite.json','sha256':sha(BUD)}; inp['budget_id']=read(BUD)['budget_id']; inp['study_budget_config']={'path':'config/rq1_recovery_v2c_composite_study_budget.json','sha256':sha(STUDY)}; inp['slot_provenance']=provenance
 (d/'input_manifest.json').write_text(json.dumps(inp,indent=2)+'\n',encoding='utf-8')
 (d/'candidate_response.json').write_text(json.dumps({'tests':tests,'assessment':'Merged independent recovery-v2c slot outputs; review and C1 validation pending.'},indent=2)+'\n',encoding='utf-8')
 run={'format':'rq1-generation-run/v1','task_id':task,'trajectory_id':runs[0]['trajectory_id'],'checkpoint_id':'C1','method':'vanilla_llm','model_config_id':read(BUD)['model']['model_config_id'],'budget_id':read(BUD)['budget_id'],'study_budget_id':read(STUDY)['budget_id'],'budget':read(BUD)['budget'],'actual_usage':{'calls':2,'input_tokens':sum(x['actual_usage']['input_tokens'] or 0 for x in runs),'output_tokens':sum(x['actual_usage']['output_tokens'] or 0 for x in runs),'token_usage_available':True},'candidate_count':len(tests),'retained_count':0,'codex_exit_code':0,'output_budget_exceeded':False,'validation_log_path':'validation.jsonl','input_manifest_path':'input_manifest.json'}
 (d/'generation_run.json').write_text(json.dumps(run,indent=2)+'\n',encoding='utf-8'); return d
def main():
 p=argparse.ArgumentParser(); p.add_argument('--task',action='append'); a=p.parse_args(); tasks=[x for x in TASKS if not a.task or x in a.task]
 for t in tasks:
  d=merge(t); review=d/'correctness_review.json'
  if not review.exists(): subprocess.run([sys.executable,'scripts/review_generated_candidates.py','--package',str(PKG/t/'C1'),'--generation-dir',str(d),'--model','gpt-6-luna','--reasoning-effort','medium'],cwd=ROOT,check=True)
  if not (d/'frozen_test_suite.json').exists():
   r=subprocess.run([sys.executable,'scripts/prepare_checkpoint_runtime.py','--package',str(PKG/t/'C1'),'--problems-root',str(ROOT/'external/scb-problems')],cwd=ROOT,capture_output=True,text=True,check=True); runtime=next(x.split(': ',1)[1] for x in r.stdout.splitlines() if x.startswith('Prepared isolated checkpoint context: '))
   subprocess.run([sys.executable,'scripts/validate_and_freeze_tests.py','--generation-dir',str(d),'--runtime-dir',runtime,'--correctness-review',str(review),'--repeats','3','--baseline-audit',str(BASE)],cwd=ROOT,check=True)
  print(t,flush=True)
if __name__=='__main__': main()

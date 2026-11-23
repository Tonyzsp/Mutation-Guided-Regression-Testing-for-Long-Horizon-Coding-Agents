"""Launch the locked paired-budget Vanilla recovery v2b generation slots."""
from __future__ import annotations

import json
import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = ("cfgpipe","circuit_eval","dag_execution","database_migration","datagate","dynamic_buffer","dynamic_config_service_api","env_manager","eve_industry","eve_market_tools","eve_route_planner","execution_server","file_merger","forge","l2m","layered_config_synthesizer","log_query","meshctl","metric_transform_lang","migrate_configs","mocked_http","mvvault","pwd_manager","recli","sith","test_translator","textdrop","trajectory_api","xjq")
PACKAGES = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
RESULTS = ROOT / "artifacts" / "results" / "rq1_vanilla_recovery_v2b"
GENERATOR = ROOT / "scripts" / "run_vanilla_generation.py"
BUDGET = ROOT / "config" / "generation_budget_rq1_recovery_v2b_call.json"
STUDY = ROOT / "config" / "rq1_recovery_v2b_vanilla_study_budget.json"
PROMPT = ROOT / "prompts" / "vanilla_gap_v4.md"

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-root", type=Path, default=RESULTS)
    args = parser.parse_args()
    results = args.result_root.resolve()
    results.mkdir(parents=True, exist_ok=True)
    log = results / "generation_cohort.jsonl"
    prior = {json.loads(line)["slot"] for line in log.read_text(encoding="utf-8").splitlines() if line.strip()} if log.exists() else set()
    for task in TASKS:
        brief = ROOT / "artifacts" / "results" / "rq1_full_36x1" / task / "C1" / "gap_opportunities_isolation_revalidated_v3.md"
        for number in (1, 2):
            slot = f"{task}/call_{number:02d}"
            out = results / task / "C1" / "vanilla_llm" / f"call_{number:02d}"
            if slot in prior or (out / "generation_run.json").is_file():
                continue
            command = [sys.executable, str(GENERATOR), "--package", str(PACKAGES / task / "C1"), "--output-dir", str(out), "--budget-config", str(BUDGET), "--study-budget-config", str(STUDY), "--prompt-template", str(PROMPT), "--opportunity-brief", str(brief)]
            result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
            record = {"slot": slot, "task_id": task, "status": "complete" if result.returncode == 0 else "failed", "exit_code": result.returncode, "finished_at_utc": datetime.now(timezone.utc).isoformat(), "output_dir": str(out), "stdout": result.stdout, "stderr": result.stderr}
            with log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(f"{slot}: {record['status']}", flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

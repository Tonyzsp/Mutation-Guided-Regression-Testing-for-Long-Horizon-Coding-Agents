"""Run the paired-budget Vanilla generator once for every sanitized C1 unit."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
UNITS_PATH = REPO_ROOT / "config" / "rq1_full_36x1_candidate_units.json"
PACKAGE_ROOT = REPO_ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
BUDGET_PATH = REPO_ROOT / "config" / "generation_budget_rq1_c1_cohort_formal.json"
STUDY_BUDGET_PATH = REPO_ROOT / "config" / "rq1_full_study_budget_v1.json"
PROMPT_PATH = REPO_ROOT / "prompts" / "vanilla_gap_v4.md"
GENERATOR = REPO_ROOT / "scripts" / "run_vanilla_generation.py"
RESULTS_ROOT = REPO_ROOT / "artifacts" / "results" / "rq1_full_36x1"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--units", type=Path, default=UNITS_PATH)
    parser.add_argument("--budget-config", type=Path, default=BUDGET_PATH)
    parser.add_argument("--study-budget-config", type=Path, default=STUDY_BUDGET_PATH)
    parser.add_argument("--prompt-template", type=Path, default=PROMPT_PATH)
    parser.add_argument("--output-suffix", default="vanilla_formal_v1_20261117")
    parser.add_argument("--task", action="append", help="Limit this run to task IDs; may be repeated")
    args = parser.parse_args()

    all_units = read_json(args.units.resolve(strict=True))["units"]
    known_tasks = {unit["task_id"] for unit in all_units}
    unknown_tasks = sorted(set(args.task or []) - known_tasks)
    if unknown_tasks:
        raise ValueError(f"unknown task IDs: {unknown_tasks}")
    units = [unit for unit in all_units if not args.task or unit["task_id"] in args.task]
    if not units:
        raise ValueError("no C1 units selected")
    budget_id = read_json(args.budget_config.resolve(strict=True))["budget_id"]
    study_budget_sha = hashlib.sha256(args.study_budget_config.resolve(strict=True).read_bytes()).hexdigest()
    batch_dir = RESULTS_ROOT / f"cohort_logs_{args.output_suffix}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    batch_log = batch_dir / "cohort_generation.jsonl"
    prior: dict[str, list[dict[str, Any]]] = {}
    if batch_log.exists():
        for line in batch_log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                prior.setdefault(record["task_id"], []).append(record)

    failures = 0
    for index, unit in enumerate(units, start=1):
        task_id = unit["task_id"]
        package = PACKAGE_ROOT / task_id / "C1"
        generation_dir = RESULTS_ROOT / task_id / "C1" / args.output_suffix
        brief = RESULTS_ROOT / task_id / "C1" / "formal_v1_inputs_20261117" / "gap_opportunities.md"
        run_path = generation_dir / "generation_run.json"
        prior_records = prior.get(task_id, [])
        current_generation_dir = generation_dir.resolve()
        prior_success = any(
            record.get("status") in {"completed", "already_complete"}
            and record.get("generation_dir")
            and Path(record["generation_dir"]).resolve() == current_generation_dir
            for record in prior_records
        )
        existing = read_json(run_path) if run_path.is_file() else None
        existing_complete = (
            existing is not None
            and existing.get("codex_exit_code") == 0
            and existing.get("prompt_template_version") == "c1_shared_gap_v4_cohort_v1"
            and existing.get("study_budget_sha256") == study_budget_sha
            and (generation_dir / "candidate_response.json").is_file()
            and (generation_dir / "input_manifest.json").is_file()
            and (generation_dir / "prompt_archive.md").is_file()
        )
        if prior_success and not run_path.is_file():
            status = "missing_run_record"
            return_code = 2
            output = "prior cohort log reports success but generation_run.json is missing"
            error = "refusing to treat an incomplete artifact as complete"
        elif existing is not None and existing.get("budget_id") != budget_id:
            status = "budget_mismatch"
            return_code = 0 if status == "already_complete" else 2
            output = "existing generation run uses a different budget"
            error = f"existing budget {existing.get('budget_id')!r} != {budget_id!r}"
        elif existing_complete:
            status = "already_complete"
            return_code = 0
            output = "existing successful generation run"
            error = ""
        else:
            command = [
                sys.executable,
                str(GENERATOR),
                "--package", str(package),
                "--output-dir", str(generation_dir),
                "--budget-config", str(args.budget_config.resolve(strict=True)),
                "--study-budget-config", str(args.study_budget_config.resolve(strict=True)),
                "--prompt-template", str(args.prompt_template.resolve(strict=True)),
                "--opportunity-brief", str(brief),
            ]
            result = subprocess.run(
                command,
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            return_code = result.returncode
            output = result.stdout.strip()
            error = result.stderr.strip()
            status = "completed" if return_code == 0 else "failed"
            if run_path.is_file():
                generated = read_json(run_path)
                if generated.get("budget_id") != budget_id:
                    status = "budget_mismatch"
                    return_code = 2
                    error = f"generation run used unexpected budget {generated.get('budget_id')!r}"

        record = {
            "unit_index": index,
            "unit_count": len(units),
            "task_id": task_id,
            "status": status,
            "exit_code": return_code,
            "generation_dir": str(generation_dir),
            "budget_id": budget_id,
            "study_budget_sha256": study_budget_sha,
            "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            "stdout": output,
            "stderr": error,
        }
        with batch_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"[{index}/{len(units)}] {task_id}: {status}", flush=True)
        if return_code != 0:
            failures += 1
            if error:
                print(error[-2000:], file=sys.stderr, flush=True)

    print(f"Cohort generation log: {batch_log}")
    print(f"Failed units: {failures}/{len(units)}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

"""Run exactly one method-blind semantic review for each nonempty C1 suite."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "artifacts" / "results" / "rq1_full_36x1"
UNITS = ROOT / "config" / "rq1_full_36x1_candidate_units.json"
REVIEW_SCRIPT = ROOT / "scripts" / "review_generated_candidates.py"
DEFAULT_PREPARED_INPUTS = RESULTS / "candidate_review_inputs_vanilla_formal_v1_20261117"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation-suffix", default="vanilla_formal_v1_20261117")
    parser.add_argument("--task", action="append", help="Limit to task IDs; may be repeated")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--prepared-input-dir", type=Path, default=DEFAULT_PREPARED_INPUTS)
    args = parser.parse_args()
    if Path(args.generation_suffix).name != args.generation_suffix or args.generation_suffix in {".", ".."}:
        raise ValueError("generation suffix must be one path component")

    all_units = read_json(UNITS)["units"]
    known = {row["task_id"] for row in all_units}
    unknown = sorted(set(args.task or []) - known)
    if unknown:
        raise ValueError(f"unknown task IDs: {unknown}")
    units = [row for row in all_units if not args.task or row["task_id"] in args.task]
    if not units:
        raise ValueError("no candidate-review tasks selected")
    prepared_root = args.prepared_input_dir.resolve(strict=True)
    if not prepared_root.is_relative_to(RESULTS.resolve(strict=True)):
        raise ValueError("prepared candidate-review inputs must remain under artifacts/results")

    batch_dir = RESULTS / f"cohort_logs_review_{args.generation_suffix}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    log_path = batch_dir / "candidate_review.jsonl"
    previous: dict[str, dict[str, Any]] = {}
    if log_path.is_file():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                previous[record["task_id"]] = record

    records: list[dict[str, Any]] = []
    for index, unit in enumerate(units, 1):
        task = unit["task_id"]
        generation_dir = RESULTS / task / "C1" / args.generation_suffix
        run_path = generation_dir / "correctness_review_run.json"
        response_path = generation_dir / "candidate_response.json"
        attempt_artifacts = [
            generation_dir / name for name in (
                "correctness_review_prompt.md",
                "correctness_review_events.jsonl",
                "correctness_review_stderr.log",
            )
        ]
        existing = previous.get(task)
        if not response_path.is_file():
            triage_path = RESULTS / "mutant_family_triage_formal_v1_sanitized_20261117" / task / "mutant_triage.json"
            if (
                args.generation_suffix.startswith("mutation_guided")
                and triage_path.is_file()
                and read_json(triage_path).get("selected_for_generation_count", 0) == 0
            ):
                if existing is not None:
                    records.append(existing)
                    print(f"[{index}/{len(units)}] {task}: {existing.get('status')} candidates=0 calls=0 (already recorded)", flush=True)
                    continue
                record = {
                    "task_id": task,
                    "status": "not_needed_no_generated_tests",
                    "exit_code": 0,
                    "candidate_count": 0,
                    "call_count": 0,
                    "generation_directory": str(generation_dir.relative_to(ROOT)),
                    "review_run_sha256": None,
                    "detail": "No Guided generation was launched because this task had no eligible mutant hints.",
                    "checked_at_utc": datetime.now(timezone.utc).isoformat(),
                }
                records.append(record)
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                print(f"[{index}/{len(units)}] {task}: {record['status']} candidates=0 calls=0", flush=True)
                continue
            raise FileNotFoundError(f"{task}: C1 candidate response is missing")
        response = read_json(response_path)
        candidate_count = len(response.get("tests", []))
        if existing is not None:
            records.append(existing)
            print(f"[{index}/{len(units)}] {task}: {existing.get('status')} candidates={candidate_count} calls={existing.get('call_count', 0)} (already recorded)", flush=True)
            continue
        elif run_path.is_file():
            run = read_json(run_path)
            status = run.get("status", "unknown")
            detail = "review result already exists; no review call was repeated"
            exit_code = run.get("exit_code", 0 if status in {"complete", "not_needed_no_candidates"} else 2)
        elif any(path.exists() for path in attempt_artifacts):
            status = "attempted_incomplete_no_retry"
            detail = "a review prompt/attempt artifact exists without a completed run record"
            exit_code = 2
        elif candidate_count == 0:
            command = [
                sys.executable, str(REVIEW_SCRIPT),
                "--package", str(ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1" / task / "C1"),
                "--generation-dir", str(generation_dir),
                "--timeout-seconds", str(args.timeout_seconds),
            ]
            result = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            exit_code = result.returncode
            detail = (result.stderr.strip() or result.stdout.strip())[-1800:]
            status = read_json(run_path).get("status", "unknown") if run_path.is_file() else "failed_before_review_run_record"
        else:
            prepared_prompt = prepared_root / task / "review_prompt.md"
            prepared_manifest_path = prepared_root / task / "input_manifest.json"
            if not prepared_prompt.is_file() or not prepared_manifest_path.is_file():
                raise FileNotFoundError(f"{task}: prepared review prompt or manifest is missing")
            prepared_manifest = read_json(prepared_manifest_path)
            if (
                prepared_manifest.get("task_id") != task
                or prepared_manifest.get("candidate_count") != candidate_count
                or prepared_manifest.get("source_candidate_response_sha256") != hashlib.sha256(response_path.read_bytes()).hexdigest()
                or prepared_manifest.get("prompt_sha256") != hashlib.sha256(prepared_prompt.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
                or prepared_manifest.get("llm_calls_made") != 0
                or prepared_manifest.get("status") != "prepared_locally_not_sent"
            ):
                raise ValueError(f"{task}: prepared review prompt provenance differs from current candidates")
            command = [
                sys.executable, str(REVIEW_SCRIPT),
                "--package", str(ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1" / task / "C1"),
                "--generation-dir", str(generation_dir),
                "--timeout-seconds", str(args.timeout_seconds),
                "--prepared-prompt", str(prepared_prompt),
            ]
            result = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            exit_code = result.returncode
            detail = (result.stderr.strip() or result.stdout.strip())[-1800:]
            if run_path.is_file():
                status = read_json(run_path).get("status", "unknown")
            else:
                status = "failed_before_review_run_record"

        record = {
            "task_id": task,
            "status": status,
            "exit_code": exit_code,
            "candidate_count": candidate_count,
            "call_count": 0 if candidate_count == 0 else (1 if status == "complete" else 0),
            "generation_directory": str(generation_dir.relative_to(ROOT)),
            "review_run_sha256": None,
            "detail": detail,
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        if run_path.is_file():
            record["review_run_sha256"] = hashlib.sha256(run_path.read_bytes()).hexdigest()
            run = read_json(run_path)
            record["call_count"] = int(run.get("call_count", 0))
        if record["call_count"] > 1:
            raise ValueError(f"{task}: candidate review exceeded one call")
        records.append(record)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"[{index}/{len(units)}] {task}: {status} candidates={candidate_count} calls={record['call_count']}", flush=True)

    total_calls = sum(row["call_count"] for row in records)
    report = {
        "format": "rq1-candidate-review-cohort/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "generation_suffix": args.generation_suffix,
        "task_count": len(records),
        "candidate_count": sum(row["candidate_count"] for row in records),
        "actual_review_calls": total_calls,
        "per_task_call_cap": 1,
        "study_call_cap": 72,
        "prepared_input_directory": str(prepared_root.relative_to(ROOT)),
        "status_counts": dict(sorted(Counter(row["status"] for row in records).items())),
        "log_path": str(log_path.relative_to(ROOT)),
        "records": records,
    }
    report_path = RESULTS / f"candidate_review_cohort_{args.generation_suffix}.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"task_count": len(records), "candidate_count": report["candidate_count"], "actual_review_calls": total_calls, "status_counts": report["status_counts"], "report": str(report_path)}, ensure_ascii=False))
    return 0 if total_calls <= 72 and all(row["status"] in {"complete", "not_needed_no_candidates", "not_needed_no_generated_tests"} for row in records) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

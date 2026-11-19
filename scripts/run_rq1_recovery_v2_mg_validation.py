"""Validate and freeze recovery-v2 MG candidates without launching generation calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "artifacts" / "results"
V2_ROOT = RESULTS / "rq1_recovery_v2"
V1_ROOT = RESULTS / "rq1_full_36x1"
PACKAGE_ROOT = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
BASELINE = V1_ROOT / "c1_base_audit_current_sanitized_20261116" / "c1_base_suite_audit.json"
SUMMARY = V2_ROOT / "generation_call_summary.json"
AMENDMENT = ROOT / "config" / "rq1_recovery_v2_mg_only_amendment.json"
AMENDMENT_LOCK = ROOT / "config" / "rq1_recovery_v2_mg_only_amendment.lock.json"
V1_MG_SUFFIX = "mutation_guided_formal_v1_baseline_linked_20261117"
VALIDATOR = ROOT / "scripts" / "validate_and_freeze_tests.py"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def locate_call(task: str, slot: int) -> Path:
    logical = V2_ROOT / task / "C1" / "mutation_guided" / f"call_{slot:02d}"
    for path in (logical, logical / "model_invocation_after_preflight_fix"):
        if (path / "generation_run.json").is_file() and (path / "candidate_response.json").is_file():
            return path
    raise FileNotFoundError(f"{task}/call_{slot:02d}: complete generation artifacts are missing")


def validation_unit(row: dict[str, Any], timeout: int, mutmut_timeout: int) -> dict[str, Any]:
    task = row["task_id"]
    slot = int(row["slot"])
    generation_dir = locate_call(task, slot)
    frozen_path = generation_dir / "frozen_test_suite.json"
    record_path = generation_dir / "recovery_v2_validation_record.json"
    if frozen_path.is_file():
        frozen = read_json(frozen_path)
        return {
            "task_id": task,
            "call_slot": f"call_{slot:02d}",
            "status": "already_frozen",
            "candidate_count": int(row["candidate_count"]),
            "retained_count": frozen.get("metadata", {}).get("candidate_files_retained", 0),
            "suite_sha256": frozen.get("suite_sha256"),
            "generation_directory": str(generation_dir.relative_to(ROOT).as_posix()),
        }
    if record_path.exists():
        prior = read_json(record_path)
        return {**prior, "status": "existing_validation_attempt_not_repeated"}

    review_path = generation_dir / "correctness_review.json"
    if not review_path.is_file():
        raise FileNotFoundError(f"{task}/call_{slot:02d}: per-slot mapped correctness review is missing")
    v1_manifest = read_json(V1_ROOT / task / "C1" / V1_MG_SUFFIX / "frozen_test_suite.json")
    runtime_manifest = Path(v1_manifest["metadata"]["runtime_manifest_path"]).resolve(strict=True)
    package = PACKAGE_ROOT / task / "C1"
    command = [
        sys.executable, str(VALIDATOR),
        "--generation-dir", str(generation_dir),
        "--runtime-dir", str(runtime_manifest.parent),
        "--correctness-review", str(review_path),
        "--repeats", "3",
        "--target-repeat-runs", "3",
        "--timeout-seconds", str(timeout),
        "--mutmut-timeout-seconds", str(mutmut_timeout),
        "--baseline-audit", str(BASELINE),
    ]
    started = datetime.now(timezone.utc).isoformat()
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    (generation_dir / "recovery_v2_validation.stdout.log").write_text(result.stdout, encoding="utf-8")
    (generation_dir / "recovery_v2_validation.stderr.log").write_text(result.stderr, encoding="utf-8")
    frozen = read_json(frozen_path) if frozen_path.is_file() else None
    record = {
        "format": "rq1-recovery-v2-mg-validation-slot/v1",
        "task_id": task,
        "call_slot": f"call_{slot:02d}",
        "status": "complete" if result.returncode == 0 and frozen else "failed",
        "exit_code": result.returncode,
        "candidate_count": int(row["candidate_count"]),
        "retained_count": frozen.get("metadata", {}).get("candidate_files_retained", 0) if frozen else None,
        "suite_sha256": frozen.get("suite_sha256") if frozen else None,
        "correctness_review_sha256": sha256_file(review_path),
        "runtime_manifest_path": str(runtime_manifest.relative_to(ROOT).as_posix()),
        "baseline_audit_sha256": sha256_file(BASELINE),
        "repeats": 3,
        "target_mutant_repeat_runs": 3,
        "started_at_utc": started,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "generation_directory": str(generation_dir.relative_to(ROOT).as_posix()),
        "stdout_path": "recovery_v2_validation.stdout.log",
        "stderr_path": "recovery_v2_validation.stderr.log",
        "stdout_tail": result.stdout[-2500:],
        "stderr_tail": result.stderr[-2500:],
    }
    write_json(record_path, record)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--mutmut-timeout-seconds", type=int, default=900)
    args = parser.parse_args()
    if not 1 <= args.workers <= 4:
        raise ValueError("workers must be between 1 and 4")

    amendment = read_json(AMENDMENT)
    if read_json(AMENDMENT_LOCK).get("sha256") != sha256_file(AMENDMENT):
        raise ValueError("MG-only amendment differs from its lock")
    if amendment.get("mg_generation", {}).get("methods") != ["mutation_guided"]:
        raise ValueError("validation scope must be MG-only")
    summary = read_json(SUMMARY)
    if summary.get("methods") != ["mutation_guided"]:
        raise ValueError("generation summary contains a method outside MG")
    units = [row for row in summary["records"] if int(row.get("candidate_count", 0)) > 0]
    if any(row.get("status") != "already_complete" for row in units):
        raise ValueError("a candidate-bearing slot is not a completed generation slot")

    summary_path = V2_ROOT / "mg_validation_summary.json"
    records_by_key: dict[str, dict[str, Any]] = {}
    if summary_path.is_file():
        previous = read_json(summary_path)
        if previous.get("scope_amendment_sha256") != read_json(V2_ROOT / "generation_call_summary.json").get("scope_amendment_sha256"):
            raise ValueError("existing MG validation summary belongs to a different cohort")
        records_by_key = {
            f"{row['task_id']}|{row['call_slot']}": row
            for row in previous.get("records", [])
        }

    pending: list[dict[str, Any]] = []
    for row in units:
        key = f"{row['task_id']}|call_{int(row['slot']):02d}"
        if key in records_by_key and records_by_key[key].get("status") in {"complete", "already_frozen", "existing_validation_attempt_not_repeated"}:
            continue
        pending.append(row)

    def persist() -> None:
        rows = sorted(records_by_key.values(), key=lambda item: (item["task_id"], item["call_slot"]))
        counts = Counter(row.get("status", "unknown") for row in rows)
        output = {
            "format": "rq1-recovery-v2-mg-validation-summary/v1",
            "scope_amendment_sha256": summary.get("scope_amendment_sha256"),
            "task_count": len(amendment["mg_generation"]["task_ids"]),
            "candidate_bearing_slots": len(units),
            "slots_completed_or_recorded": len(rows),
            "candidate_count": sum(int(row.get("candidate_count", 0)) for row in rows),
            "retained_count": sum(int(row.get("retained_count") or 0) for row in rows),
            "status_counts": dict(sorted(counts.items())),
            "validation_repeats": 3,
            "target_mutant_repeat_runs": 3,
            "records": rows,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        write_json(summary_path, output)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(validation_unit, row, args.timeout_seconds, args.mutmut_timeout_seconds): row
            for row in pending
        }
        for future in as_completed(futures):
            row = futures[future]
            try:
                record = future.result()
            except Exception as error:
                record = {
                    "task_id": row["task_id"],
                    "call_slot": f"call_{int(row['slot']):02d}",
                    "status": "coordinator_error",
                    "candidate_count": int(row["candidate_count"]),
                    "error": f"{type(error).__name__}: {error}",
                }
            records_by_key[f"{record['task_id']}|{record['call_slot']}"] = record
            persist()
            print(json.dumps({
                "task_id": record["task_id"],
                "call_slot": record["call_slot"],
                "status": record["status"],
                "candidate_count": record.get("candidate_count"),
                "retained_count": record.get("retained_count"),
                "error": record.get("error"),
            }, ensure_ascii=False), flush=True)

    persist()
    final = read_json(summary_path)
    print(json.dumps({key: final[key] for key in (
        "task_count", "candidate_bearing_slots", "slots_completed_or_recorded",
        "candidate_count", "retained_count", "status_counts",
    )}, ensure_ascii=False))
    return 0 if not final["status_counts"].get("coordinator_error") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(2) from error

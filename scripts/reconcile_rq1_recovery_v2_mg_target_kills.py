"""Reconcile recovery-v2 MG target checks with the locked at-least-one kill rule."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "artifacts" / "results"
V2 = RESULTS / "rq1_recovery_v2"
V1 = RESULTS / "rq1_full_36x1"
PACKAGES = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
SUMMARY = V2 / "generation_call_summary.json"
VALIDATION_SUMMARY = V2 / "mg_validation_summary.json"
V1_MG_SUFFIX = "mutation_guided_formal_v1_baseline_linked_20261117"
OUT = V2 / "mg_target_kill_reconciliation_summary.json"
sys.path.insert(0, str(ROOT / "scripts"))
from validate_and_freeze_tests import (  # noqa: E402
    apply_frozen_mutation_diff,
    copy_runtime_stage,
    read_json as read_validator_json,
    read_pytest_outcomes,
    run_in_docker,
    sha256_file as validator_sha256_file,
    write_text_file,
)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def locate_call(task: str, slot: int) -> Path:
    root = V2 / task / "C1" / "mutation_guided" / f"call_{slot:02d}"
    for path in (root, root / "model_invocation_after_preflight_fix"):
        if (path / "validation.jsonl").is_file():
            return path
    raise FileNotFoundError(f"{task}/call_{slot:02d}: validation.jsonl is missing")


def c1_passed(record: dict[str, Any]) -> bool:
    runs = record.get("runs", [])
    return (
        bool(record.get("collection_ids"))
        and len(runs) == 3
        and all(run.get("passed") is True for run in runs)
    )


def direct_check_target(
    task: str,
    slot: int,
    candidate_index: int,
    mutant: dict[str, Any],
    candidate: dict[str, Any],
    record: dict[str, Any],
    runtime_dir: Path,
    runtime: dict[str, Any],
    package: dict[str, Any],
    out_dir: Path,
    timeout: int,
) -> dict[str, Any]:
    mutant_id = str(mutant["mutant_id"])
    source_rel = str(mutant.get("location", {}).get("path", ""))
    source_scope = set(runtime.get("task_runtime_profile", {}).get("mutmut", {}).get("source_paths", []))
    candidate_ids = [str(value) for value in record.get("collection_ids", [])]
    if source_rel not in source_scope:
        return {"mutant_id": mutant_id, "status": "error", "killed": False, "error": "source outside frozen mutation scope", "runs": []}
    if not candidate_ids:
        return {"mutant_id": mutant_id, "status": "error", "killed": False, "error": "candidate has no collected node IDs", "runs": []}

    runs: list[dict[str, Any]] = []
    for repetition in range(1, 4):
        prefix = f"target_{candidate_index:02d}_{repetition:02d}_{hashlib.sha256(mutant_id.encode('utf-8')).hexdigest()[:8]}"
        with tempfile.TemporaryDirectory(
            prefix=f"rq1-v2-direct-{task}-{slot:02d}-{candidate_index:02d}-{repetition:02d}-",
            dir=ROOT / "tmp" / "validation",
            ignore_cleanup_errors=True,
        ) as temp:
            stage = Path(temp) / "workspace"
            copy_runtime_stage(runtime_dir, stage)
            test_path = stage / package["base_tests_path"] / candidate["relative_path"]
            if test_path.exists():
                return {"mutant_id": mutant_id, "status": "error", "killed": False, "error": "candidate path collides with base tests", "runs": runs}
            test_path.parent.mkdir(parents=True, exist_ok=True)
            test_path.write_text(candidate["content"], encoding="utf-8")

            source_path = stage / source_rel
            before = sha256_file(source_path)
            try:
                after = apply_frozen_mutation_diff(
                    source_path, str(mutant["diff"]), source_rel.replace("\\", "/")
                )
            except (OSError, UnicodeError, ValueError) as error:
                runs.append({"repetition": repetition, "status": "error", "killed": False, "error": f"{type(error).__name__}: {error}"})
                continue
            changed = after != before
            report_path = stage / "rq1_validation_report.json"
            report_path.unlink(missing_ok=True)
            command = list(runtime["test_command_argv"])
            command[-1:] = candidate_ids
            command.extend(["--json-report", "--json-report-file=/workspace/rq1_validation_report.json"])
            try:
                result = run_in_docker(stage, runtime, command, timeout=timeout)
            except subprocess.TimeoutExpired:
                runs.append({"repetition": repetition, "status": "timeout", "killed": False, "source_changed": changed})
                continue

            log_stem = prefix
            write_text_file(out_dir / f"{log_stem}.stdout.log", result.stdout)
            write_text_file(out_dir / f"{log_stem}.stderr.log", result.stderr)
            try:
                outcomes = read_pytest_outcomes(report_path)
                report_error = None
            except (OSError, json.JSONDecodeError, RuntimeError) as error:
                outcomes = {}
                report_error = str(error)
            candidate_outcomes = {nodeid: outcomes.get(nodeid) for nodeid in candidate_ids}
            failed = any(value in {"failed", "error"} for value in candidate_outcomes.values())
            passed = all(value == "passed" for value in candidate_outcomes.values())
            if changed and failed:
                status = "killed"
            elif changed and result.returncode == 0 and passed:
                status = "survived"
            else:
                status = "error"
            runs.append({
                "repetition": repetition,
                "status": status,
                "killed": status == "killed",
                "exit_code": result.returncode,
                "source_changed": changed,
                "candidate_outcomes": candidate_outcomes,
                "report_error": report_error,
                "stdout_path": f"{log_stem}.stdout.log",
                "stderr_path": f"{log_stem}.stderr.log",
            })

    statuses = [item["status"] for item in runs]
    if len(statuses) == 3 and all(value == "killed" for value in statuses):
        status = "killed"
    elif len(statuses) == 3 and all(value == "survived" for value in statuses):
        status = "survived"
    elif statuses and all(value in {"error", "timeout"} for value in statuses):
        status = "error"
    else:
        status = "unstable"
    return {
        "mutant_id": mutant_id,
        "status": status,
        "killed": status == "killed",
        "verification": "exact hash-linked survivor diff on fresh C1 runtime; candidate-only pytest run",
        "diff_sha256": hashlib.sha256(str(mutant["diff"]).encode("utf-8")).hexdigest(),
        "repeat_runs": 3,
        "runs": runs,
    }


def reconcile_call(unit: dict[str, Any], timeout: int) -> dict[str, Any]:
    task, slot = unit["task_id"], int(unit["slot"])
    generation_dir = locate_call(task, slot)
    validation_path = generation_dir / "validation.jsonl"
    records = [json.loads(line) for line in validation_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    response = read_json(generation_dir / "candidate_response.json")
    review = read_json(generation_dir / "correctness_review.json")
    review_by_path = {row["candidate_path"]: row for row in review["reviews"]}
    eligible_path = generation_dir / "eligible_mutants.json"
    input_manifest = read_json(generation_dir / "input_manifest.json")
    if sha256_file(eligible_path) != input_manifest["eligible_mutants_sha256"]:
        raise ValueError(f"{task}/call_{slot:02d}: eligible mutant data hash changed")
    eligible = read_json(eligible_path)
    eligible_by_id = {row["mutant_id"]: row for row in eligible}
    if len(records) != len(response["tests"]):
        raise ValueError(f"{task}/call_{slot:02d}: candidate and validation record counts differ")

    v1 = read_json(V1 / task / "C1" / V1_MG_SUFFIX / "frozen_test_suite.json")
    runtime_path = Path(v1["metadata"]["runtime_manifest_path"]).resolve(strict=True)
    runtime_dir = runtime_path.parent
    runtime = read_validator_json(runtime_path)
    package_root = PACKAGES / task / "C1"
    package = read_json(package_root / "package.json")

    output_records: list[dict[str, Any]] = []
    for index, (candidate, record) in enumerate(zip(response["tests"], records, strict=True), 1):
        name = candidate["relative_path"]
        content_sha = hashlib.sha256(candidate["content"].encode("utf-8")).hexdigest()
        review_row = review_by_path[name]
        if record.get("status") == "retained":
            output_records.append({
                "candidate_path": name,
                "candidate_content_sha256": content_sha,
                "decision": "retained_strict_validation",
                "target_checks": record.get("target_mutant_results", []),
            })
            continue

        if review_row.get("verdict") != "correct" or not c1_passed(record):
            output_records.append({
                "candidate_path": name,
                "candidate_content_sha256": content_sha,
                "decision": "rejected_unchanged",
                "reason": record.get("rejection_reason") or "semantic review or source-C1 checks failed",
            })
            continue

        target_checks = record.get("target_mutant_results", [])
        stable_kills = [
            item for item in target_checks
            if item.get("killed") is True
            and item.get("repeat_runs") == 3
            and len(item.get("repetition_results", [])) == 3
            and all(run.get("status") == "killed" for run in item["repetition_results"])
        ]
        if stable_kills:
            output_records.append({
                "candidate_path": name,
                "candidate_content_sha256": content_sha,
                "decision": "retained_at_least_one_target_killed",
                "rule": "one declared surviving mutant killed in all three fresh runs",
                "target_checks": stable_kills,
            })
            continue

        reason = str(record.get("rejection_reason") or "")
        if not (
            "targeted mutmut runner failed with exit code" in reason
            or "targeted mutmut run timed out" in reason
        ):
            output_records.append({
                "candidate_path": name,
                "candidate_content_sha256": content_sha,
                "decision": "rejected_unchanged",
                "reason": reason or "no protocol basis for diff reconciliation",
            })
            continue

        target_ids = [str(value) for value in candidate.get("target_mutant_ids", [])]
        if not target_ids or not set(target_ids).issubset(eligible_by_id):
            raise ValueError(f"{task}/call_{slot:02d}/{name}: target IDs do not match frozen eligible survivors")
        candidate_index = index
        # Keep persistent reconciliation evidence under a shallow v2 path.
        # Some Windows hosts still enforce MAX_PATH even when Python handles
        # the candidate payload in memory; nesting beneath call_NN and the
        # model-invocation directory can exceed that limit.
        out_dir = (
            V2 / "diff_reconciliation" / task
            / f"call_{slot:02d}" / f"candidate_{index:02d}"
        )
        manifest = {
            "format": "rq1-recovery-v2-mg-target-diff-input/v1",
            "task_id": task,
            "call_slot": f"call_{slot:02d}",
            "candidate_path": name,
            "candidate_content_sha256": content_sha,
            "eligible_mutants_sha256": sha256_file(eligible_path),
            "candidate_review_sha256": sha256_file(generation_dir / "correctness_review.json"),
            "validation_record_sha256": hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
            "target_mutant_ids": target_ids,
            "trigger": reason,
            "rule": "retain only if at least one declared target mutant is killed in three exact-diff fresh runs",
        }
        manifest_path = out_dir / "reconciliation_input_manifest.json"
        if manifest_path.is_file():
            if read_json(manifest_path) != manifest:
                raise FileExistsError(f"{manifest_path} has different inputs")
            prior_path = out_dir / "target_diff_result.json"
            if prior_path.is_file():
                prior = read_json(prior_path)
                output_records.append({
                    "candidate_path": name,
                    "candidate_content_sha256": content_sha,
                    "decision": prior["decision"],
                    "target_checks": prior["target_checks"],
                    "direct_diff_record_path": str(prior_path.relative_to(ROOT).as_posix()),
                })
                continue
        else:
            out_dir.mkdir(parents=True)
            write_json(manifest_path, manifest)

        target_results = []
        for target_index, mutant_id in enumerate(target_ids, 1):
            target_results.append(direct_check_target(
                task, slot, candidate_index, eligible_by_id[mutant_id], candidate, record,
                runtime_dir, runtime, package, out_dir, timeout,
            ))
            write_json(out_dir / "target_diff_result.partial.json", {
                "format": "rq1-recovery-v2-mg-target-diff-result/v1",
                "target_checks": target_results,
                "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            })
        killed = [item["mutant_id"] for item in target_results if item.get("killed")]
        result = {
            "format": "rq1-recovery-v2-mg-target-diff-result/v1",
            "task_id": task,
            "call_slot": f"call_{slot:02d}",
            "candidate_path": name,
            "candidate_content_sha256": content_sha,
            "input_manifest_sha256": sha256_file(manifest_path),
            "target_checks": target_results,
            "killed_target_mutant_ids": killed,
            "decision": "retained_direct_diff_fallback" if killed else "rejected_direct_diff_fallback",
            "decision_rule": "one declared mutant killed in all three fresh runs",
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        write_json(out_dir / "target_diff_result.json", result)
        output_records.append({
            "candidate_path": name,
            "candidate_content_sha256": content_sha,
            "decision": result["decision"],
            "killed_target_mutant_ids": killed,
            "target_checks": target_results,
            "direct_diff_record_path": str((out_dir / "target_diff_result.json").relative_to(ROOT).as_posix()),
        })

    retained = [item for item in output_records if item["decision"].startswith("retained")]
    result = {
        "format": "rq1-recovery-v2-mg-validation-reconciliation-slot/v1",
        "task_id": task,
        "call_slot": f"call_{slot:02d}",
        "candidate_count": len(output_records),
        "retained_count": len(retained),
        "records": output_records,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json(generation_dir / "target_kill_reconciliation.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()
    if not 1 <= args.workers <= 4 or args.timeout_seconds < 1:
        raise ValueError("workers must be 1-4 and timeout must be positive")
    generation = read_json(SUMMARY)
    validation = read_json(VALIDATION_SUMMARY)
    if validation.get("candidate_bearing_slots") != 31 or validation.get("slots_completed_or_recorded") != 31:
        raise ValueError("the initial 31-slot MG validation batch is not complete")

    units = [row for row in generation["records"] if int(row.get("candidate_count", 0)) > 0]
    completed: dict[str, dict[str, Any]] = {}
    if OUT.is_file():
        old = read_json(OUT)
        if old.get("scope_amendment_sha256") != generation.get("scope_amendment_sha256"):
            raise ValueError("existing reconciliation belongs to another MG cohort")
        completed = {f"{row['task_id']}|{row['call_slot']}": row for row in old.get("records", [])}
    pending = [
        row for row in units
        if (
            f"{row['task_id']}|call_{int(row['slot']):02d}" not in completed
            or completed[f"{row['task_id']}|call_{int(row['slot']):02d}"].get("status") == "coordinator_error"
        )
    ]

    def persist() -> None:
        records = sorted(completed.values(), key=lambda row: (row["task_id"], row["call_slot"]))
        decisions = Counter(entry["decision"] for row in records for entry in row.get("records", []))
        write_json(OUT, {
            "format": "rq1-recovery-v2-mg-validation-reconciliation/v1",
            "scope_amendment_sha256": generation.get("scope_amendment_sha256"),
            "candidate_bearing_slots": len(units),
            "slots_reconciled": len(records),
            "candidate_count": sum(row["candidate_count"] for row in records),
            "retained_count": sum(row["retained_count"] for row in records),
            "decision_counts": dict(sorted(decisions.items())),
            "direct_diff_timeout_seconds": args.timeout_seconds,
            "direct_diff_repeat_runs": 3,
            "records": records,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        })

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(reconcile_call, unit, args.timeout_seconds): unit for unit in pending}
        for future in as_completed(futures):
            unit = futures[future]
            try:
                result = future.result()
            except Exception as error:
                result = {
                    "task_id": unit["task_id"],
                    "call_slot": f"call_{int(unit['slot']):02d}",
                    "candidate_count": int(unit["candidate_count"]),
                    "retained_count": 0,
                    "records": [],
                    "status": "coordinator_error",
                    "error": f"{type(error).__name__}: {error}",
                }
            completed[f"{result['task_id']}|{result['call_slot']}"] = result
            persist()
            print(json.dumps({
                "task_id": result["task_id"],
                "call_slot": result["call_slot"],
                "status": result.get("status", "complete"),
                "candidate_count": result["candidate_count"],
                "retained_count": result["retained_count"],
                "error": result.get("error"),
            }, ensure_ascii=False), flush=True)
    persist()
    final = read_json(OUT)
    print(json.dumps({key: final[key] for key in (
        "candidate_bearing_slots", "slots_reconciled", "candidate_count", "retained_count", "decision_counts",
    )}, ensure_ascii=False))
    return 0 if not final["decision_counts"].get("coordinator_error") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(2) from error

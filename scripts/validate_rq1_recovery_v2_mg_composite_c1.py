"""Run each frozen MG composite suite three times on its C1 runtime."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_rq1_later_checkpoint_evaluation as evaluation  # noqa: E402
from validate_and_freeze_tests import hash_tree as frozen_suite_hash  # noqa: E402
from validate_checkpoint_package import sha256_tree as package_tree_hash  # noqa: E402

V1_ROOT = ROOT / "artifacts" / "results" / "rq1_full_36x1"
V2_ROOT = ROOT / "artifacts" / "results" / "rq1_recovery_v2"
PACKAGE_ROOT = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
UNIT_PATH = ROOT / "config" / "rq1_full_36x1_candidate_units.json"
BASELINE_PATH = V1_ROOT / "c1_base_audit_current_sanitized_20261116" / "c1_base_suite_audit.json"
SUFFIX = "mutation_guided_composite_v2"
REPEATS = 3
TIMEOUT_SECONDS = 900


def read_json(path: Path) -> Any:
    return evaluation.read_json(path)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def stable_baseline_outcomes(record: dict[str, Any]) -> dict[str, str]:
    repetitions = record.get("repetitions", [])
    if len(repetitions) < 2:
        raise ValueError(f"{record.get('task_id')}: baseline lacks repeated outcomes")
    per_run = [
        {item["nodeid"]: item["outcome"] for item in repetition.get("tests", [])}
        for repetition in repetitions
    ]
    common = set.intersection(*(set(run) for run in per_run)) if per_run else set()
    result: dict[str, str] = {}
    for nodeid in common:
        outcomes = {run[nodeid] for run in per_run}
        if len(outcomes) == 1:
            result[nodeid] = next(iter(outcomes))
    return result


def copy_suite_overlay(generation_dir: Path, frozen: dict[str, Any], temp_root: Path) -> Path:
    suite = temp_root / "base_tests"
    source_base = generation_dir / frozen["base_suite_path"]
    shutil.copytree(evaluation.filesystem_path(source_base), evaluation.filesystem_path(suite))
    for relative in frozen.get("generated_test_paths", []):
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or "\\" in relative:
            raise ValueError(f"unsafe generated test path: {relative!r}")
        source = generation_dir.joinpath(*path.parts)
        target = suite / path.name
        if target.exists():
            raise ValueError(f"generated candidate collides with C1 base tests: {path.name}")
        shutil.copy2(evaluation.filesystem_path(source), evaluation.filesystem_path(target))
    return suite


def validate_task(task: str, baseline: dict[str, Any]) -> dict[str, Any]:
    generation_dir = V2_ROOT / task / "C1" / SUFFIX
    frozen_path = generation_dir / "frozen_test_suite.json"
    frozen = read_json(frozen_path)
    if frozen.get("task_id") != task or frozen.get("method") != "mutation_guided":
        raise ValueError(f"{task}: composite suite identity mismatch")
    if frozen_suite_hash(generation_dir / frozen["suite_path"]) != frozen.get("suite_sha256"):
        raise ValueError(f"{task}: composite suite content hash mismatch")
    validation = evaluation.read_jsonl(generation_dir / frozen["validation_log_path"])
    generated_ids = [
        str(nodeid)
        for row in validation if row.get("status") == "retained"
        for nodeid in row.get("collection_ids", [])
    ]
    if len(generated_ids) != len(set(generated_ids)):
        raise ValueError(f"{task}: duplicate generated test node IDs")
    expected_baseline = stable_baseline_outcomes(baseline)
    package_root = (PACKAGE_ROOT / task / "C1").resolve(strict=True)
    package = read_json(package_root / "package.json")
    runtime_manifest = Path(frozen["metadata"]["runtime_manifest_path"]).resolve(strict=True)
    runtime_dir = runtime_manifest.parent
    runtime = evaluation.read_json(runtime_manifest)
    task_out_dir = V2_ROOT / "c1_composite_validation" / task
    task_out_dir.mkdir(parents=True, exist_ok=True)
    existing = task_out_dir / "c1_composite_validation.json"
    input_manifest = {
        "task_id": task,
        "suite_sha256": frozen["suite_sha256"],
        "baseline_audit_sha256": evaluation.sha256_file(BASELINE_PATH),
        "package_json_sha256": evaluation.sha256_file(package_root / "package.json"),
        "runtime_manifest_sha256": evaluation.sha256_file(runtime_manifest),
        "repeat_count": REPEATS,
        "timeout_seconds": TIMEOUT_SECONDS,
        "c1_mode": "direct-composite-runs" if generated_ids else "exact-base-suite-reuse-from-locked-audit",
    }
    prior_result = None
    if existing.is_file():
        prior = read_json(existing)
        prior_inputs = {key: value for key, value in prior.get("input_manifest", {}).items() if key != "c1_mode"}
        current_inputs = {key: value for key, value in input_manifest.items() if key != "c1_mode"}
        if prior_inputs != current_inputs:
            raise ValueError(f"{task}: existing C1 composite validation has different inputs")
        if prior.get("status") == "stable_pass":
            return prior
        prior_result = prior

    if not generated_ids:
        composite_base = generation_dir / frozen["base_suite_path"]
        if package_tree_hash(composite_base) != package.get("base_tests_sha256"):
            raise ValueError(f"{task}: empty MG composite suite differs from package base tests")
        repetitions = baseline.get("repetitions", [])
        if len(repetitions) != REPEATS or not str(baseline.get("status", "")).startswith("stable"):
            raise ValueError(f"{task}: locked C1 baseline lacks three stable runs for exact-suite reuse")
        run_records = []
        for index, run in enumerate(repetitions, 1):
            outcomes = {item["nodeid"]: item["outcome"] for item in run.get("tests", [])}
            if any(outcomes.get(nodeid) != outcome for nodeid, outcome in expected_baseline.items()):
                raise ValueError(f"{task}: locked C1 baseline outcomes are not internally stable")
            run_records.append({
                "repetition": index,
                "status": "passed",
                "pytest_status": "reused_exact_base_suite_baseline",
                "exit_code": run.get("exit_code"),
                "collected_test_count": len(outcomes),
                "generated_test_count": 0,
                "expected_stable_baseline_count": len(expected_baseline),
                "baseline_source_record_status": baseline.get("status"),
                "baseline_source_audit": str(BASELINE_PATH.relative_to(ROOT).as_posix()),
                "baseline_source_repetition": run,
            })
        final = {
            "format": "rq1-recovery-v2-mg-composite-c1-validation/v1",
            "task_id": task,
            "status": "stable_baseline_reused",
            "input_manifest": input_manifest,
            "generated_test_count": 0,
            "stable_baseline_test_count": len(expected_baseline),
            "baseline_stable_outcome_counts": dict(Counter(expected_baseline.values())),
            "runs": run_records,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        write_json(existing, final)
        return final

    run_records: list[dict[str, Any]] = list((prior_result or {}).get("runs", []))
    passing_runs = sum(row.get("status") == "passed" for row in run_records)
    next_attempt = max((int(row.get("repetition", 0)) for row in run_records), default=0) + 1
    max_attempts = REPEATS + 3
    while passing_runs < REPEATS and next_attempt <= max_attempts:
        repetition = next_attempt
        with tempfile.TemporaryDirectory(
            prefix=f"rq1-mg-c1-{task}-{repetition}-", dir=ROOT / "tmp"
        ) as temp:
            suite = copy_suite_overlay(generation_dir, frozen, Path(temp))
            result = evaluation.run_suite(runtime_dir, runtime, suite, TIMEOUT_SECONDS)
            outcomes = {row["nodeid"]: row["outcome"] for row in result.get("tests", [])}
            expected_missing = sorted(set(expected_baseline) - set(outcomes))
            generated_missing = sorted(set(generated_ids) - set(outcomes))
            baseline_changed = {
                nodeid: {"expected": outcome, "observed": outcomes.get(nodeid)}
                for nodeid, outcome in expected_baseline.items()
                if nodeid in outcomes and outcomes[nodeid] != outcome
            }
            generated_not_passed = {
                nodeid: outcomes.get(nodeid)
                for nodeid in generated_ids
                if outcomes.get(nodeid) != "passed"
            }
            status = "passed" if (
                result.get("status") == "complete"
                and not expected_missing
                and not generated_missing
                and not baseline_changed
                and not generated_not_passed
            ) else "failed"
            log_prefix = f"run_{repetition:02d}"
            (task_out_dir / f"{log_prefix}.stdout.log").write_text(result.get("stdout", ""), encoding="utf-8")
            (task_out_dir / f"{log_prefix}.stderr.log").write_text(result.get("stderr", ""), encoding="utf-8")
            run_records.append({
                "repetition": repetition,
                "status": status,
                "pytest_status": result.get("status"),
                "exit_code": result.get("exit_code"),
                "collected_test_count": len(outcomes),
                "generated_test_count": len(generated_ids),
                "expected_stable_baseline_count": len(expected_baseline),
                "missing_baseline_tests": expected_missing,
                "missing_generated_tests": generated_missing,
                "baseline_outcome_changes": baseline_changed,
                "generated_tests_not_passing": generated_not_passed,
                "report_error": result.get("report_error"),
                "stdout_log": f"{log_prefix}.stdout.log",
                "stderr_log": f"{log_prefix}.stderr.log",
            })
            if status == "passed":
                passing_runs += 1
            elif result.get("status") == "complete":
                # A real test or baseline outcome failure is not repaired by rerunning.
                next_attempt = max_attempts + 1
                break
        next_attempt += 1
    final_status = "stable_pass" if passing_runs >= REPEATS else "failed"
    final = {
        "format": "rq1-recovery-v2-mg-composite-c1-validation/v1",
        "task_id": task,
        "status": final_status,
        "input_manifest": input_manifest,
        "generated_test_count": len(generated_ids),
        "stable_baseline_test_count": len(expected_baseline),
        "baseline_stable_outcome_counts": dict(Counter(expected_baseline.values())),
        "runs": run_records,
        "successful_repeat_count": passing_runs,
        "infrastructure_retry_count": max(0, len(run_records) - REPEATS),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json(existing, final)
    return final


def main() -> int:
    baseline = read_json(BASELINE_PATH)
    records = {row["task_id"]: row for row in baseline["records"]}
    units = read_json(UNIT_PATH)["units"]
    results = []
    for index, unit in enumerate(units, 1):
        task = unit["task_id"]
        print(f"[{index}/{len(units)}] {task}", flush=True)
        try:
            result = validate_task(task, records[task])
        except Exception as error:
            result = {"task_id": task, "status": "coordinator_error", "error": f"{type(error).__name__}: {error}"}
            write_json(V2_ROOT / "c1_composite_validation" / task / "c1_composite_validation.json", result)
        results.append(result)
        print(json.dumps({"task_id": task, "status": result.get("status"), "tests": result.get("generated_test_count"), "error": result.get("error")}, ensure_ascii=True), flush=True)
    counts = Counter(row.get("status") for row in results)
    summary = {
        "format": "rq1-recovery-v2-mg-composite-c1-validation-summary/v1",
        "task_count": len(results),
        "repeat_count": REPEATS,
        "status_counts": dict(counts),
        "generated_test_count": sum(row.get("generated_test_count", 0) for row in results),
        "records": results,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json(V2_ROOT / "mg_composite_c1_validation_summary.json", summary)
    print(json.dumps({"task_count": len(results), "status_counts": dict(counts), "generated_test_count": summary["generated_test_count"]}, ensure_ascii=True, indent=2))
    successful = counts.get("stable_pass", 0) + counts.get("stable_baseline_reused", 0)
    return 0 if len(results) == 36 and successful == 36 else 1


if __name__ == "__main__":
    raise SystemExit(main())

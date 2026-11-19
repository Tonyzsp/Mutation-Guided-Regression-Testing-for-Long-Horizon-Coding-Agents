"""Measure the C1 coverage increment from the frozen Mutation-Guided suite."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from validate_and_freeze_tests import filesystem_path, hash_tree
from validate_checkpoint_package import sha256_tree as package_sha256_tree


ROOT = Path(__file__).resolve().parents[1]
RESULT_ROOT = ROOT / "artifacts" / "results" / "rq1_full_36x1"
RECOVERY_ROOT = ROOT / "artifacts" / "results" / "rq1_recovery_v2"
PACKAGE_ROOT = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
BASELINE_AUDIT = RESULT_ROOT / "c1_coverage_integrity_current_sanitized_20261116.json"
GUIDED_SUFFIX = "mutation_guided_formal_v1_baseline_linked_20261117"
PREPARE_RUNTIME_SCRIPT = ROOT / "scripts" / "prepare_checkpoint_runtime.py"
COVERAGE_RUNNER_SCRIPT = ROOT / "scripts" / "run_checkpoint_coverage.py"
DEFAULT_OUTPUT = RESULT_ROOT / "rq2_mg_coverage_v1_20261118"
DEFAULT_RUNTIME_ROOT = ROOT / "tmp" / "rq2-mg-coverage-v1"
DEFAULT_COMPOSITE_OUTPUT = RECOVERY_ROOT / "rq2_mg_composite_coverage_v2_20261118"
DEFAULT_COMPOSITE_RUNTIME_ROOT = ROOT / "tmp" / "rq2-mg-composite-coverage-v2"


def read_json(path: Path) -> Any:
    with open(filesystem_path(path), "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(filesystem_path(path), "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(filesystem_path(path), "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_relative(value: str, field: str) -> PurePosixPath:
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or "\\" in value:
        raise ValueError(f"unsafe {field}: {value!r}")
    return relative


def input_bundle(task: str, suite_cohort: str = "v1") -> dict[str, Any]:
    package_dir = PACKAGE_ROOT / task / "C1"
    package = read_json(package_dir / "package.json")
    if suite_cohort == "v1":
        generation_dir = RESULT_ROOT / task / "C1" / GUIDED_SUFFIX
    elif suite_cohort == "composite-v2":
        generation_dir = RECOVERY_ROOT / task / "C1" / "mutation_guided_composite_v2"
    else:
        raise ValueError(f"unsupported MG suite cohort: {suite_cohort}")
    frozen = read_json(generation_dir / "frozen_test_suite.json")

    if frozen.get("task_id") != task or frozen.get("method") != "mutation_guided" or frozen.get("source_checkpoint") != "C1":
        raise ValueError(f"{task}: frozen suite identity mismatch")
    metadata = frozen.get("metadata", {})
    if metadata.get("base_tests_sha256") != package.get("base_tests_sha256"):
        raise ValueError(f"{task}: frozen suite base-test hash differs from the C1 package")
    if suite_cohort == "v1":
        generation = read_json(generation_dir / "generation_run.json")
        input_manifest = read_json(generation_dir / "input_manifest.json")
        if generation.get("task_id") != task or generation.get("method") != "mutation_guided":
            raise ValueError(f"{task}: generation record identity mismatch")
        if frozen.get("budget_id") != generation.get("budget_id"):
            raise ValueError(f"{task}: frozen suite and generation budget differ")
        if input_manifest.get("base_tests_sha256") != package.get("base_tests_sha256"):
            raise ValueError(f"{task}: generation input base-test hash differs from the C1 package")
        expected_test_count = generation.get("retained_count")
    else:
        if metadata.get("recovery_v2_composite") is not True:
            raise ValueError(f"{task}: suite is not marked as a recovery-v2 composite")
        v1_frozen_path = RESULT_ROOT / task / "C1" / GUIDED_SUFFIX / "frozen_test_suite.json"
        v1_frozen = read_json(v1_frozen_path)
        if metadata.get("v1_frozen_suite_sha256") != v1_frozen.get("suite_sha256"):
            raise ValueError(f"{task}: composite suite does not link to the frozen v1 suite")
        v1_input_manifest = read_json(v1_frozen_path.parent / "input_manifest.json")
        if metadata.get("source_package_sha256") != v1_input_manifest.get("input_package_sha256"):
            raise ValueError(f"{task}: composite source-package hash differs from its frozen v1 input")
        expected_test_count = metadata.get("composite_test_count")
    if expected_test_count != len(frozen.get("generated_test_paths", [])):
        raise ValueError(f"{task}: retained-test count disagrees with the frozen suite")

    suite_relative = safe_relative(str(frozen["suite_path"]), "suite_path")
    base_relative = safe_relative(str(frozen["base_suite_path"]), "base_suite_path")
    if base_relative.parts[: len(suite_relative.parts)] != suite_relative.parts:
        raise ValueError(f"{task}: frozen base suite is outside its suite root")
    suite_root = generation_dir.joinpath(*suite_relative.parts)
    base_suite = generation_dir.joinpath(*base_relative.parts)
    if hash_tree(suite_root) != frozen.get("suite_sha256"):
        raise ValueError(f"{task}: frozen suite content hash mismatch")
    if package_sha256_tree(base_suite) != package.get("base_tests_sha256"):
        raise ValueError(f"{task}: frozen base-suite files do not match the C1 package hash")

    generated_sources: list[tuple[str, Path, str]] = []
    for relative_text in frozen.get("generated_test_paths", []):
        relative = safe_relative(str(relative_text), "generated_test_path")
        if relative.parts[: len(suite_relative.parts)] != suite_relative.parts:
            raise ValueError(f"{task}: generated test escaped frozen suite root")
        source = generation_dir.joinpath(*relative.parts)
        generated_sources.append((str(relative), source, sha256_file(source)))

    runtime_manifest_text = frozen.get("metadata", {}).get("runtime_manifest_path")
    if not runtime_manifest_text:
        raise ValueError(f"{task}: frozen suite has no staged C1 runtime manifest")
    source_runtime_manifest = Path(runtime_manifest_text)
    runtime = read_json(source_runtime_manifest)
    expected_runtime_fields = {
        "task_id": task,
        "checkpoint_id": "C1",
        "workspace_revision": package.get("workspace_revision"),
        "base_tests_sha256": package.get("base_tests_sha256"),
    }
    for key, expected in expected_runtime_fields.items():
        if runtime.get(key) != expected:
            raise ValueError(f"{task}: staged runtime {key} does not match current C1 package")
    runtime_source_package = Path(str(runtime.get("source_package", "")))
    if os.path.normcase(os.path.abspath(runtime_source_package)) != os.path.normcase(os.path.abspath(package_dir)):
        raise ValueError(f"{task}: runtime manifest points to a different C1 package")
    runtime_root = source_runtime_manifest.parent.resolve(strict=True)
    allowed_runtime_root = (ROOT / "tmp" / "checkpoint-runtime").resolve(strict=True)
    if not runtime_root.is_relative_to(allowed_runtime_root):
        raise ValueError(f"{task}: source runtime is outside the prepared runtime directory")

    test_dir_relative = safe_relative(
        str(runtime.get("task_runtime_profile", {}).get("test_directory", "")),
        "runtime test directory",
    )
    if test_dir_relative.as_posix() != str(package.get("base_tests_path", "")):
        raise ValueError(f"{task}: staged runtime test path differs from C1 package")

    record = {
        "task_id": task,
        "source_checkpoint": "C1",
        "method": "mutation_guided",
        "suite_cohort": suite_cohort,
        "suite_sha256": frozen["suite_sha256"],
        "base_tests_sha256": package["base_tests_sha256"],
        "workspace_revision": package["workspace_revision"],
        "package_json_sha256": sha256_file(package_dir / "package.json"),
        "generation_budget_id": frozen.get("budget_id"),
        "runtime_manifest_path": str(source_runtime_manifest),
        "docker_image": runtime.get("docker_image"),
        "generated_test_count": len(generated_sources),
        "generated_tests": [{"path": path, "sha256": digest} for path, _, digest in generated_sources],
    }
    record["input_sha256"] = hashlib.sha256(
        json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {
        "record": record,
        "package": package,
        "generation_dir": generation_dir,
        "frozen": frozen,
        "generated_sources": generated_sources,
        "source_runtime": runtime_root,
        "test_dir_relative": test_dir_relative,
    }


def coverage_attempt_paths(output_root: Path, runtime_root: Path, task: str, input_sha: str) -> tuple[int, Path, Path]:
    task_output = output_root / task
    task_runtime = runtime_root / task
    task_output.mkdir(parents=True, exist_ok=True)
    task_runtime.mkdir(parents=True, exist_ok=True)
    existing = sorted(
        (int(path.name.split("-")[-1]), path)
        for path in task_output.glob("attempt-*")
        if path.is_dir() and path.name.split("-")[-1].isdigit()
    )
    for attempt, attempt_dir in existing:
        record_path = attempt_dir / "rq2_mg_coverage_record.json"
        if not record_path.is_file():
            continue
        record = read_json(record_path)
        coverage_dir = attempt_dir / "coverage"
        base_coverage_dir = attempt_dir / "base_coverage"
        coverage_manifest = coverage_dir / "coverage_run_manifest.json"
        base_coverage_manifest = base_coverage_dir / "coverage_run_manifest.json"
        if record.get("input_sha256") == input_sha and coverage_manifest.is_file() and base_coverage_manifest.is_file():
            coverage = read_json(coverage_manifest)
            base_coverage = read_json(base_coverage_dir / "coverage.json") if (base_coverage_dir / "coverage.json").is_file() else {}
            data_path = coverage_dir / "coverage.json"
            if (
                data_path.is_file()
                and read_json(data_path).get("totals", {}).get("num_statements") is not None
                and base_coverage.get("totals", {}).get("num_statements") is not None
            ):
                return attempt, attempt_dir, task_runtime / f"attempt-{attempt:02d}"
    next_attempt = max((number for number, _ in existing), default=0) + 1
    return next_attempt, task_output / f"attempt-{next_attempt:02d}", task_runtime / f"attempt-{next_attempt:02d}"


def load_baseline_rows() -> dict[str, dict[str, Any]]:
    audit = read_json(BASELINE_AUDIT)
    if audit.get("status") != "complete" or audit.get("task_count") != 36:
        raise ValueError("current sanitized 36-task base-coverage audit is incomplete")
    rows = {row["task_id"]: row for row in audit.get("records", []) if row.get("status") == "verified"}
    if len(rows) != 36:
        raise ValueError("base-coverage audit does not contain 36 verified task records")
    return rows


def materialize_and_run(task: str, bundle: dict[str, Any], output_root: Path, runtime_root: Path, timeout_seconds: int) -> dict[str, Any]:
    record = bundle["record"]
    attempt, attempt_dir, runtime_attempt_dir = coverage_attempt_paths(output_root, runtime_root, task, record["input_sha256"])
    coverage_dir = attempt_dir / "coverage"
    base_coverage_dir = attempt_dir / "base_coverage"
    existing_record = attempt_dir / "rq2_mg_coverage_record.json"
    if existing_record.is_file():
        previous = read_json(existing_record)
        if (
            previous.get("input_sha256") == record["input_sha256"]
            and previous.get("coverage_status") == "complete"
            and (base_coverage_dir / "coverage_run_manifest.json").is_file()
        ):
            guided_totals = previous.get("coverage_totals", {})
            base_totals = previous.get("base_coverage_totals", {})
            previous["line_coverage_percent"] = guided_totals.get("percent_statements_covered")
            previous["branch_coverage_percent"] = guided_totals.get("percent_branches_covered")
            previous["base_line_coverage_percent"] = base_totals.get("percent_statements_covered")
            previous["base_branch_coverage_percent"] = base_totals.get("percent_branches_covered")
            write_json(existing_record, previous)
            return previous

    base_runtime_copy = runtime_attempt_dir / "runtime_base"
    guided_runtime_copy = runtime_attempt_dir / "runtime_guided"
    if not base_runtime_copy.exists():
        shutil.copytree(filesystem_path(bundle["source_runtime"]), filesystem_path(base_runtime_copy))
    if not guided_runtime_copy.exists():
        shutil.copytree(filesystem_path(bundle["source_runtime"]), filesystem_path(guided_runtime_copy))
    test_destination = guided_runtime_copy.joinpath(*bundle["test_dir_relative"].parts)
    for relative, source, _digest in bundle["generated_sources"]:
        destination = test_destination / PurePosixPath(relative).name
        if os.path.exists(filesystem_path(destination)):
            raise ValueError(f"{task}: generated test filename collides with a base test: {destination.name}")
        shutil.copy2(filesystem_path(source), filesystem_path(destination))

    def run_one(label: str, runtime_dir: Path, result_dir: Path) -> dict[str, Any]:
        command = [
            sys.executable,
            str(COVERAGE_RUNNER_SCRIPT),
            "--runtime-dir",
            str(runtime_dir),
            "--output-dir",
            str(result_dir),
            "--timeout-seconds",
            str(timeout_seconds),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds + 120,
                check=False,
            )
            (attempt_dir / f"{label}_runner_stdout.log").write_text(completed.stdout, encoding="utf-8")
            (attempt_dir / f"{label}_runner_stderr.log").write_text(completed.stderr, encoding="utf-8")
            runner_exit_code: int | None = completed.returncode
            timeout_hit = False
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
            stderr = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
            (attempt_dir / f"{label}_runner_stdout.log").write_text(stdout, encoding="utf-8")
            (attempt_dir / f"{label}_runner_stderr.log").write_text(stderr, encoding="utf-8")
            runner_exit_code = None
            timeout_hit = True
        manifest_path = result_dir / "coverage_run_manifest.json"
        data_path = result_dir / "coverage.json"
        manifest = read_json(manifest_path) if manifest_path.is_file() else None
        data = read_json(data_path) if data_path.is_file() else None
        totals = data.get("totals", {}) if data else {}
        valid = bool(
            manifest
            and data
            and totals.get("num_statements") is not None
            and totals.get("percent_statements_covered") is not None
        )
        return {
            "runner_exit_code": runner_exit_code,
            "timeout": timeout_hit,
            "manifest": manifest,
            "totals": totals,
            "valid": valid,
            "result_dir": result_dir,
        }

    base_run = run_one("base", base_runtime_copy, base_coverage_dir)
    guided_run = run_one("guided", guided_runtime_copy, coverage_dir)
    base_manifest = base_run["manifest"]
    guided_manifest = guided_run["manifest"]
    base_totals = base_run["totals"]
    totals = guided_run["totals"]
    paired_config_match = bool(
        base_manifest and guided_manifest
        and base_manifest.get("coverage_config_sha256") == guided_manifest.get("coverage_config_sha256")
    )
    paired_source_match = bool(
        base_manifest and guided_manifest
        and base_manifest.get("source_sha256") == guided_manifest.get("source_sha256")
    )
    paired_image_match = bool(
        base_manifest and guided_manifest
        and base_manifest.get("docker_image") == guided_manifest.get("docker_image")
    )
    coverage_valid = bool(base_run["valid"] and guided_run["valid"] and paired_config_match and paired_source_match and paired_image_match)
    timeout_hit = bool(base_run["timeout"] or guided_run["timeout"])
    runner_exit_code = guided_run["runner_exit_code"]
    result = {
        **record,
        "attempt": attempt,
        "coverage_status": "complete" if coverage_valid else ("timeout" if timeout_hit else "coverage_failed"),
        "coverage_runner_exit_code": runner_exit_code,
        "container_exit_code": guided_manifest.get("container_exit_code") if guided_manifest else None,
        "base_container_exit_code": base_manifest.get("container_exit_code") if base_manifest else None,
        "source_sha256": guided_manifest.get("source_sha256") if guided_manifest else None,
        "base_source_sha256": base_manifest.get("source_sha256") if base_manifest else None,
        "coverage_config_sha256": guided_manifest.get("coverage_config_sha256") if guided_manifest else None,
        "base_coverage_config_sha256": base_manifest.get("coverage_config_sha256") if base_manifest else None,
        "docker_image": guided_manifest.get("docker_image") if guided_manifest else record.get("docker_image"),
        "paired_config_match": paired_config_match,
        "paired_source_match": paired_source_match,
        "paired_image_match": paired_image_match,
        "line_coverage_percent": totals.get("percent_statements_covered"),
        "branch_coverage_percent": totals.get("percent_branches_covered"),
        "coverage_totals": totals,
        "base_coverage_totals": base_totals,
        "base_line_coverage_percent": base_totals.get("percent_statements_covered"),
        "base_branch_coverage_percent": base_totals.get("percent_branches_covered"),
        "coverage_dir": str(coverage_dir.relative_to(ROOT)).replace("\\", "/"),
        "coverage_manifest_path": str((coverage_dir / "coverage_run_manifest.json").relative_to(ROOT)).replace("\\", "/") if guided_manifest else None,
        "base_coverage_dir": str(base_coverage_dir.relative_to(ROOT)).replace("\\", "/"),
        "base_coverage_manifest_path": str((base_coverage_dir / "coverage_run_manifest.json").relative_to(ROOT)).replace("\\", "/") if base_manifest else None,
        "test_exit_nonzero_but_coverage_collected": bool(guided_run["valid"] and guided_manifest and guided_manifest.get("container_exit_code") != 0),
        "base_test_exit_nonzero_but_coverage_collected": bool(base_run["valid"] and base_manifest and base_manifest.get("container_exit_code") != 0),
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json(existing_record, result)
    return result


def percent(numerator: int, denominator: int) -> float | None:
    return (numerator / denominator * 100.0) if denominator else None


def aggregate(task_rows: list[dict[str, Any]], baseline_rows: dict[str, dict[str, Any]], expected_task_count: int, output_root: Path) -> dict[str, Any]:
    joined: list[dict[str, Any]] = []
    for row in task_rows:
        if row.get("coverage_status") != "complete":
            joined.append({**row, "base_line_coverage_percent": None, "base_branch_coverage_percent": None, "line_delta_percentage_points": None, "branch_delta_percentage_points": None})
            continue
        baseline = baseline_rows[row["task_id"]]
        base_totals = row.get("base_coverage_totals", {})
        mg_totals = row["coverage_totals"]
        source_hashes_match = baseline.get("source_sha256") == row.get("source_sha256")
        base_tests_match = baseline.get("base_tests_sha256") == row.get("base_tests_sha256")
        paired_source_hashes_match = row.get("base_source_sha256") == row.get("source_sha256")
        paired_config_match = row.get("paired_config_match") is True
        paired_image_match = row.get("paired_image_match") is True
        if not source_hashes_match or not base_tests_match or not paired_source_hashes_match or not paired_config_match or not paired_image_match:
            raise ValueError(f"{row['task_id']}: paired coverage source/config/image or baseline source/test hashes do not match")
        joined.append({
            **row,
            "line_coverage_percent": mg_totals.get("percent_statements_covered"),
            "branch_coverage_percent": mg_totals.get("percent_branches_covered"),
            "base_line_coverage_percent": base_totals.get("percent_statements_covered"),
            "base_branch_coverage_percent": base_totals.get("percent_branches_covered"),
            "line_delta_percentage_points": round(
                float(mg_totals["percent_statements_covered"]) - float(base_totals["percent_statements_covered"]), 6
            ),
            "branch_delta_percentage_points": (
                round(float(mg_totals["percent_branches_covered"]) - float(base_totals["percent_branches_covered"]), 6)
                if mg_totals.get("percent_branches_covered") is not None and base_totals.get("percent_branches_covered") is not None
                else None
            ),
            "base_coverage_totals": base_totals,
            "source_hashes_match": source_hashes_match,
            "base_tests_hashes_match": base_tests_match,
            "paired_source_hashes_match": paired_source_hashes_match,
        })

    complete = [row for row in joined if row.get("coverage_status") == "complete"]

    def summarize_group(rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {"task_count": 0}
        mg_lines = sum(int(row["coverage_totals"].get("covered_lines", 0)) for row in rows)
        mg_statements = sum(int(row["coverage_totals"].get("num_statements", 0)) for row in rows)
        base_lines = sum(int(row["base_coverage_totals"].get("covered_lines", 0)) for row in rows)
        base_statements = sum(int(row["base_coverage_totals"].get("num_statements", 0)) for row in rows)
        mg_branches = sum(int(row["coverage_totals"].get("covered_branches", 0)) for row in rows)
        mg_branch_total = sum(int(row["coverage_totals"].get("num_branches", 0)) for row in rows)
        base_branches = sum(int(row["base_coverage_totals"].get("covered_branches", 0)) for row in rows)
        base_branch_total = sum(int(row["base_coverage_totals"].get("num_branches", 0)) for row in rows)
        line_deltas = [float(row["line_delta_percentage_points"]) for row in rows]
        branch_deltas = [float(row["branch_delta_percentage_points"]) for row in rows if row.get("branch_delta_percentage_points") is not None]
        return {
            "task_count": len(rows),
            "base_weighted_line_coverage_percent": percent(base_lines, base_statements),
            "mutation_guided_weighted_line_coverage_percent": percent(mg_lines, mg_statements),
            "weighted_line_delta_percentage_points": percent(mg_lines, mg_statements) - percent(base_lines, base_statements),
            "base_weighted_branch_coverage_percent": percent(base_branches, base_branch_total),
            "mutation_guided_weighted_branch_coverage_percent": percent(mg_branches, mg_branch_total),
            "weighted_branch_delta_percentage_points": (
                percent(mg_branches, mg_branch_total) - percent(base_branches, base_branch_total)
                if base_branch_total and mg_branch_total else None
            ),
            "macro_mean_line_delta_percentage_points": statistics.mean(line_deltas),
            "macro_median_line_delta_percentage_points": statistics.median(line_deltas),
            "macro_mean_branch_delta_percentage_points": statistics.mean(branch_deltas) if branch_deltas else None,
            "macro_median_branch_delta_percentage_points": statistics.median(branch_deltas) if branch_deltas else None,
            "tasks_with_line_coverage_increase": sum(delta > 0 for delta in line_deltas),
            "tasks_with_no_line_coverage_change": sum(delta == 0 for delta in line_deltas),
            "tasks_with_line_coverage_decrease": sum(delta < 0 for delta in line_deltas),
            "tasks_with_branch_coverage_increase": sum(delta > 0 for delta in branch_deltas),
            "tasks_with_no_branch_coverage_change": sum(delta == 0 for delta in branch_deltas),
            "tasks_with_branch_coverage_decrease": sum(delta < 0 for delta in branch_deltas),
        }

    added = [row for row in complete if row.get("generated_test_count", 0) > 0]
    no_added = [row for row in complete if row.get("generated_test_count", 0) == 0]
    failures = [row for row in joined if row.get("coverage_status") != "complete"]
    base_nonzero_tasks = {row["task_id"] for row in complete if row.get("base_test_exit_nonzero_but_coverage_collected")}
    guided_nonzero_tasks = {row["task_id"] for row in complete if row.get("test_exit_nonzero_but_coverage_collected")}
    summary = {
        "format": "rq2-mutation-guided-c1-coverage/v1",
        "status": "complete" if len(complete) == expected_task_count else "partial",
        "suite_cohort": next((row.get("suite_cohort", "v1") for row in joined), "v1"),
        "scope": "C1 line and branch coverage of legal base tests versus the combined frozen Mutation-Guided suite; descriptive coverage increment only",
        "coverage_metric_definitions": {
            "line": "coverage.py percent_statements_covered = covered_lines / num_statements * 100",
            "branch": "coverage.py percent_branches_covered = covered_branches / num_branches * 100",
            "delta": "Mutation-Guided percentage minus base-suite percentage, in percentage points",
        },
        "expected_task_count": expected_task_count,
        "completed_task_count": len(complete),
        "failed_or_missing_task_count": len(failures),
        "generated_test_count": sum(int(row.get("generated_test_count", 0)) for row in joined),
        "generated_test_task_count": sum(int(row.get("generated_test_count", 0)) > 0 for row in joined),
        "all_tasks": summarize_group(complete),
        "tasks_with_guided_tests": summarize_group(added),
        "tasks_without_guided_tests": summarize_group(no_added),
        "test_runner_nonzero_with_coverage_available": sum(bool(row.get("test_exit_nonzero_but_coverage_collected")) for row in complete),
        "base_test_runner_nonzero_with_coverage_available": sum(bool(row.get("base_test_exit_nonzero_but_coverage_collected")) for row in complete),
        "nonzero_exit_status_overlap_task_count": len(base_nonzero_tasks & guided_nonzero_tasks),
        "base_only_nonzero_exit_task_count": len(base_nonzero_tasks - guided_nonzero_tasks),
        "guided_only_nonzero_exit_task_count": len(guided_nonzero_tasks - base_nonzero_tasks),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "task_rows": joined,
    }
    write_json(output_root / "rq2_mg_coverage_summary.json", summary)

    def f(value: Any) -> str:
        return "n/a" if value is None else f"{float(value):.2f}%"

    def pp(value: Any) -> str:
        return "n/a" if value is None else f"{float(value):.2f} pp"

    table_rows = []
    for row in joined:
        table_rows.append([
            row["task_id"], row.get("generated_test_count", 0),
            f(row.get("base_line_coverage_percent")), f(row.get("line_coverage_percent")),
            f"{float(row['line_delta_percentage_points']):.2f}" if row.get("line_delta_percentage_points") is not None else "n/a",
            f(row.get("base_branch_coverage_percent")), f(row.get("branch_coverage_percent")),
            f"{float(row['branch_delta_percentage_points']):.2f}" if row.get("branch_delta_percentage_points") is not None else "n/a", row.get("coverage_status"),
        ])

    all_summary = summary["all_tasks"]
    added_summary = summary["tasks_with_guided_tests"]
    markdown = [
        "# Mutation-Guided C1 coverage evaluation",
        "",
        "**Scope:** paired coverage comparison between each task's legal C1 base suite and its frozen combined Mutation-Guided suite. Both suites are rerun on the unchanged C1 source with the same subprocess-aware branch coverage runner, image, and per-task coverage configuration.",
        "",
        "Line coverage uses `percent_statements_covered` (`covered_lines / num_statements`); branch coverage uses `percent_branches_covered` (`covered_branches / num_branches`). Deltas are absolute percentage-point differences.",
        "",
        f"**Status:** {summary['completed_task_count']}/{expected_task_count} tasks complete; {summary['generated_test_count']} retained Guided tests across {summary['generated_test_task_count']} tasks.",
        "",
        "## Cohort summary",
        "",
        f"- All evaluated tasks: weighted line coverage **{f(all_summary.get('base_weighted_line_coverage_percent'))} → {f(all_summary.get('mutation_guided_weighted_line_coverage_percent'))}** (Δ {pp(all_summary.get('weighted_line_delta_percentage_points'))}); weighted branch coverage **{f(all_summary.get('base_weighted_branch_coverage_percent'))} → {f(all_summary.get('mutation_guided_weighted_branch_coverage_percent'))}** (Δ {pp(all_summary.get('weighted_branch_delta_percentage_points'))}).",
        f"- Tasks with at least one retained Guided test ({added_summary.get('task_count', 0)}): weighted line coverage **{f(added_summary.get('base_weighted_line_coverage_percent'))} → {f(added_summary.get('mutation_guided_weighted_line_coverage_percent'))}** (Δ {pp(added_summary.get('weighted_line_delta_percentage_points'))}); weighted branch coverage **{f(added_summary.get('base_weighted_branch_coverage_percent'))} → {f(added_summary.get('mutation_guided_weighted_branch_coverage_percent'))}** (Δ {pp(added_summary.get('weighted_branch_delta_percentage_points'))}).",
        f"- Across tasks, line coverage increased in {all_summary.get('tasks_with_line_coverage_increase', 0)}, was unchanged in {all_summary.get('tasks_with_no_line_coverage_change', 0)}, and decreased in {all_summary.get('tasks_with_line_coverage_decrease', 0)}; branch coverage increased in {all_summary.get('tasks_with_branch_coverage_increase', 0)}, was unchanged in {all_summary.get('tasks_with_no_branch_coverage_change', 0)}, and decreased in {all_summary.get('tasks_with_branch_coverage_decrease', 0)}.",
        f"- Base suite runs had a nonzero exit with usable coverage in {summary['base_test_runner_nonzero_with_coverage_available']} tasks; combined MG suite runs did so in {summary['test_runner_nonzero_with_coverage_available']} tasks. The same {summary['nonzero_exit_status_overlap_task_count']} tasks were nonzero in both conditions; base-only {summary['base_only_nonzero_exit_task_count']}, MG-only {summary['guided_only_nonzero_exit_task_count']}. Logs retain test details.",
        "",
        "## Per-task results",
        "",
        "| Task | Added MG tests | Base line | MG line | Δ pp | Base branch | MG branch | Δ pp | Status |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        *["| " + " | ".join(str(value) for value in row) + " |" for row in table_rows],
        "",
        "## Interpretation and boundary",
        "",
        "Positive deltas show additional C1 source lines or branches executed by the combined Mutation-Guided suite over the shared base suite. This is a descriptive coverage contribution, not a causal estimate of later-regression detection. A complete coverage-control comparison requires Original and Vanilla suites run with the same paired procedure, then task-level association/adjustment against the RQ1 event counts.",
        "",
        "Task-level machine-readable totals and hashes are in `rq2_mg_coverage_summary.json`; raw coverage JSON and runner logs are under each task's `attempt-*` directory.",
        "",
    ]
    (output_root / "rq2_mg_coverage_report.md").write_text("\n".join(markdown), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", nargs="*", help="Task IDs to run; omit for all 36 tasks")
    parser.add_argument("--suite-cohort", choices=("v1", "composite-v2"), default="v1", help="Frozen MG suite cohort to measure")
    parser.add_argument("--preflight-only", action="store_true", help="Validate inputs and hashes without running Docker")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    args = parser.parse_args()

    default_output = DEFAULT_OUTPUT if args.suite_cohort == "v1" else DEFAULT_COMPOSITE_OUTPUT
    default_runtime = DEFAULT_RUNTIME_ROOT if args.suite_cohort == "v1" else DEFAULT_COMPOSITE_RUNTIME_ROOT
    output_root = (args.output_root or default_output).resolve()
    runtime_root = (args.runtime_root or default_runtime).resolve()
    allowed_results = (ROOT / "artifacts" / "results").resolve(strict=True)
    allowed_tmp = (ROOT / "tmp").resolve(strict=True)
    if not output_root.is_relative_to(allowed_results):
        raise ValueError("output root must remain under artifacts/results")
    if not runtime_root.is_relative_to(allowed_tmp):
        raise ValueError("runtime root must remain under repository tmp")
    output_root.mkdir(parents=True, exist_ok=True)
    runtime_root.mkdir(parents=True, exist_ok=True)

    units = read_json(ROOT / "config" / "rq1_full_36x1_candidate_units.json")["units"]
    all_tasks = [row["task_id"] for row in units]
    selected_tasks = args.tasks or all_tasks
    unknown = sorted(set(selected_tasks) - set(all_tasks))
    if unknown:
        raise ValueError(f"unknown task IDs: {unknown}")
    baseline_rows = load_baseline_rows()
    prepared: list[dict[str, Any]] = []
    for task in selected_tasks:
        bundle = input_bundle(task, args.suite_cohort)
        baseline = baseline_rows[task]
        if baseline.get("base_tests_sha256") != bundle["record"]["base_tests_sha256"]:
            raise ValueError(f"{task}: base coverage and MG frozen suite use different base-test hashes")
        prepared.append(bundle)
    preflight = {
        "format": f"rq2-mg-coverage-preflight/{args.suite_cohort}",
        "status": "complete",
        "suite_cohort": args.suite_cohort,
        "selected_tasks": selected_tasks,
        "task_count": len(selected_tasks),
        "generated_test_count": sum(bundle["record"]["generated_test_count"] for bundle in prepared),
        "generated_test_task_count": sum(bundle["record"]["generated_test_count"] > 0 for bundle in prepared),
        "all_inputs_match_c1_and_baseline_hashes": True,
        "baseline_coverage_task_count": len(baseline_rows),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "task_inputs": [bundle["record"] for bundle in prepared],
    }
    write_json(output_root / "rq2_mg_coverage_preflight.json", preflight)
    print(json.dumps({key: preflight[key] for key in ("status", "task_count", "generated_test_count", "generated_test_task_count", "all_inputs_match_c1_and_baseline_hashes")}, ensure_ascii=False), flush=True)
    if args.preflight_only:
        return 0

    run_rows: list[dict[str, Any]] = []
    for index, bundle in enumerate(prepared, 1):
        task = bundle["record"]["task_id"]
        row = materialize_and_run(task, bundle, output_root, runtime_root, args.timeout_seconds)
        run_rows.append(row)
        print(
            f"[{index}/{len(prepared)}] {task}: {row['coverage_status']} "
            f"line={row.get('line_coverage_percent')} branch={row.get('branch_coverage_percent')} "
            f"container_exit={row.get('container_exit_code')}",
            flush=True,
        )
        aggregate(run_rows, baseline_rows, len(all_tasks) if selected_tasks == all_tasks else len(selected_tasks), output_root)

    summary = aggregate(run_rows, baseline_rows, len(all_tasks) if selected_tasks == all_tasks else len(selected_tasks), output_root)
    print(json.dumps({
        "status": summary["status"],
        "completed_tasks": summary["completed_task_count"],
        "expected_tasks": summary["expected_task_count"],
        "all_tasks_weighted_line_delta_pp": summary["all_tasks"].get("weighted_line_delta_percentage_points"),
        "all_tasks_weighted_branch_delta_pp": summary["all_tasks"].get("weighted_branch_delta_percentage_points"),
        "generated_task_weighted_line_delta_pp": summary["tasks_with_guided_tests"].get("weighted_line_delta_percentage_points"),
        "generated_task_weighted_branch_delta_pp": summary["tasks_with_guided_tests"].get("weighted_branch_delta_percentage_points"),
        "report": str(output_root / "rq2_mg_coverage_report.md"),
    }, indent=2, ensure_ascii=False), flush=True)
    return 0 if summary["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())

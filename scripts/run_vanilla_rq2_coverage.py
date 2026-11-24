"""Measure the C1 coverage increment from the audited frozen Vanilla v5 suite.

The runner deliberately performs a paired measurement for every selected task: it
copies the same prepared C1 runtime twice, runs the legal base suite in one copy,
and overlays the immutable Vanilla-generated tests in the other.  It never calls
an LLM and it never modifies a frozen suite.
"""

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
PACKAGE_ROOT = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
BASELINE_AUDIT = RESULT_ROOT / "c1_coverage_integrity_current_sanitized_20261116.json"
VANILLA_AUDIT = RESULT_ROOT / "c1_vanilla_cohort_audit_formal_v1_20261117.json"
VANILLA_SUFFIX = "vanilla_formal_v5_20261116"
COVERAGE_RUNNER_SCRIPT = ROOT / "scripts" / "run_checkpoint_coverage.py"
DEFAULT_OUTPUT = RESULT_ROOT / "rq2_vanilla_coverage_v5_20261123"
DEFAULT_RUNTIME_ROOT = ROOT / "tmp" / "rq2-vanilla-coverage-v5"


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


def load_baseline_rows() -> dict[str, dict[str, Any]]:
    audit = read_json(BASELINE_AUDIT)
    if audit.get("status") != "complete" or audit.get("task_count") != 36:
        raise ValueError("current sanitized 36-task base-coverage audit is incomplete")
    rows = {row["task_id"]: row for row in audit.get("records", []) if row.get("status") == "verified"}
    if len(rows) != 36:
        raise ValueError("base-coverage audit does not contain 36 verified task records")
    return rows


def load_vanilla_audit_rows() -> dict[str, dict[str, Any]]:
    audit = read_json(VANILLA_AUDIT)
    required = {
        "format": "rq1-c1-vanilla-cohort-audit/v1",
        "status": "complete",
        "task_count": 36,
        "expected_task_count": 36,
        "candidate_count": 89,
        "retained_count": 65,
    }
    for key, expected in required.items():
        if audit.get(key) != expected:
            raise ValueError(f"Vanilla cohort audit {key} is {audit.get(key)!r}, expected {expected!r}")
    rows = {row["task_id"]: row for row in audit.get("records", [])}
    if len(rows) != 36 or set(rows) != {row["task_id"] for row in read_json(ROOT / "config" / "rq1_full_36x1_candidate_units.json")["units"]}:
        raise ValueError("Vanilla cohort audit does not contain exactly the locked 36 tasks")
    if any(row.get("cohort_directory") != VANILLA_SUFFIX for row in rows.values()):
        raise ValueError("Vanilla cohort audit points to a different frozen suite directory")
    if sum(int(row.get("retained_count", -1)) for row in rows.values()) != 65:
        raise ValueError("Vanilla cohort audit record counts do not sum to 65")
    return rows


def input_bundle(task: str, audit_record: dict[str, Any]) -> dict[str, Any]:
    package_dir = PACKAGE_ROOT / task / "C1"
    package = read_json(package_dir / "package.json")
    generation_dir = RESULT_ROOT / task / "C1" / VANILLA_SUFFIX
    frozen = read_json(generation_dir / "frozen_test_suite.json")
    generation = read_json(generation_dir / "generation_run.json")
    input_manifest = read_json(generation_dir / "input_manifest.json")

    if frozen.get("task_id") != task or frozen.get("method") != "vanilla_llm" or frozen.get("source_checkpoint") != "C1":
        raise ValueError(f"{task}: frozen Vanilla suite identity mismatch")
    if generation.get("task_id") != task or generation.get("method") != "vanilla_llm":
        raise ValueError(f"{task}: Vanilla generation record identity mismatch")
    if frozen.get("budget_id") != generation.get("budget_id"):
        raise ValueError(f"{task}: frozen Vanilla suite and generation budget differ")
    metadata = frozen.get("metadata", {})
    for source_name, source_hash in (
        ("frozen suite", metadata.get("base_tests_sha256")),
        ("generation input", input_manifest.get("base_tests_sha256")),
        ("cohort audit", audit_record.get("base_tests_sha256")),
    ):
        if source_hash != package.get("base_tests_sha256"):
            raise ValueError(f"{task}: {source_name} base-test hash differs from the C1 package")

    generated_paths = frozen.get("generated_test_paths", [])
    if generation.get("retained_count") != len(generated_paths):
        raise ValueError(f"{task}: generated-test count disagrees with generation record")
    if audit_record.get("retained_count") != len(generated_paths):
        raise ValueError(f"{task}: generated-test count disagrees with cohort audit")
    if audit_record.get("candidate_count") != generation.get("candidate_count"):
        raise ValueError(f"{task}: candidate count disagrees with cohort audit")

    suite_relative = safe_relative(str(frozen["suite_path"]), "suite_path")
    base_relative = safe_relative(str(frozen["base_suite_path"]), "base_suite_path")
    if base_relative.parts[: len(suite_relative.parts)] != suite_relative.parts:
        raise ValueError(f"{task}: frozen base suite is outside its suite root")
    suite_root = generation_dir.joinpath(*suite_relative.parts)
    base_suite = generation_dir.joinpath(*base_relative.parts)
    if hash_tree(suite_root) != frozen.get("suite_sha256"):
        raise ValueError(f"{task}: frozen Vanilla suite content hash mismatch")
    if package_sha256_tree(base_suite) != package.get("base_tests_sha256"):
        raise ValueError(f"{task}: frozen Vanilla base suite does not match the C1 package")

    generated_sources: list[tuple[str, Path, str]] = []
    for relative_text in generated_paths:
        relative = safe_relative(str(relative_text), "generated_test_path")
        if relative.parts[: len(suite_relative.parts)] != suite_relative.parts:
            raise ValueError(f"{task}: generated test escaped frozen suite root")
        source = generation_dir.joinpath(*relative.parts)
        if not source.is_file():
            raise ValueError(f"{task}: missing frozen Vanilla test {relative}")
        generated_sources.append((str(relative), source, sha256_file(source)))

    runtime_manifest_text = metadata.get("runtime_manifest_path")
    if not runtime_manifest_text:
        raise ValueError(f"{task}: frozen Vanilla suite has no staged C1 runtime manifest")
    source_runtime_manifest = Path(str(runtime_manifest_text))
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
        "method": "vanilla_llm",
        "suite_cohort": VANILLA_SUFFIX,
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
        record_path = attempt_dir / "rq2_vanilla_coverage_record.json"
        coverage_dir = attempt_dir / "coverage"
        base_coverage_dir = attempt_dir / "base_coverage"
        if not record_path.is_file() or not (coverage_dir / "coverage_run_manifest.json").is_file() or not (base_coverage_dir / "coverage_run_manifest.json").is_file():
            continue
        record = read_json(record_path)
        base_data = base_coverage_dir / "coverage.json"
        vanilla_data = coverage_dir / "coverage.json"
        if record.get("input_sha256") == input_sha and base_data.is_file() and vanilla_data.is_file():
            if read_json(base_data).get("totals", {}).get("num_statements") is not None and read_json(vanilla_data).get("totals", {}).get("num_statements") is not None:
                return attempt, attempt_dir, task_runtime / f"attempt-{attempt:02d}"
    next_attempt = max((number for number, _ in existing), default=0) + 1
    return next_attempt, task_output / f"attempt-{next_attempt:02d}", task_runtime / f"attempt-{next_attempt:02d}"


def run_one(attempt_dir: Path, label: str, runtime_dir: Path, result_dir: Path, timeout_seconds: int) -> dict[str, Any]:
    command = [
        sys.executable,
        str(COVERAGE_RUNNER_SCRIPT),
        "--runtime-dir", str(runtime_dir),
        "--output-dir", str(result_dir),
        "--timeout-seconds", str(timeout_seconds),
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
    valid = bool(manifest and data and totals.get("num_statements") is not None and totals.get("percent_statements_covered") is not None)
    return {
        "runner_exit_code": runner_exit_code,
        "timeout": timeout_hit,
        "manifest": manifest,
        "totals": totals,
        "valid": valid,
    }


def materialize_and_run(task: str, bundle: dict[str, Any], output_root: Path, runtime_root: Path, timeout_seconds: int) -> dict[str, Any]:
    record = bundle["record"]
    attempt, attempt_dir, runtime_attempt_dir = coverage_attempt_paths(output_root, runtime_root, task, record["input_sha256"])
    coverage_dir = attempt_dir / "coverage"
    base_coverage_dir = attempt_dir / "base_coverage"
    existing_record = attempt_dir / "rq2_vanilla_coverage_record.json"
    if existing_record.is_file():
        previous = read_json(existing_record)
        if previous.get("input_sha256") == record["input_sha256"] and previous.get("coverage_status") == "complete":
            return previous

    base_runtime_copy = runtime_attempt_dir / "runtime_base"
    vanilla_runtime_copy = runtime_attempt_dir / "runtime_vanilla"
    if not base_runtime_copy.exists():
        shutil.copytree(filesystem_path(bundle["source_runtime"]), filesystem_path(base_runtime_copy))
    if not vanilla_runtime_copy.exists():
        shutil.copytree(filesystem_path(bundle["source_runtime"]), filesystem_path(vanilla_runtime_copy))
    test_destination = vanilla_runtime_copy.joinpath(*bundle["test_dir_relative"].parts)
    for relative, source, _digest in bundle["generated_sources"]:
        destination = test_destination / PurePosixPath(relative).name
        if destination.exists():
            raise ValueError(f"{task}: generated Vanilla test filename collides with a base test: {destination.name}")
        shutil.copy2(filesystem_path(source), filesystem_path(destination))

    base_run = run_one(attempt_dir, "base", base_runtime_copy, base_coverage_dir, timeout_seconds)
    vanilla_run = run_one(attempt_dir, "vanilla", vanilla_runtime_copy, coverage_dir, timeout_seconds)
    base_manifest = base_run["manifest"]
    vanilla_manifest = vanilla_run["manifest"]
    base_totals = base_run["totals"]
    totals = vanilla_run["totals"]
    paired_config_match = bool(base_manifest and vanilla_manifest and base_manifest.get("coverage_config_sha256") == vanilla_manifest.get("coverage_config_sha256"))
    paired_source_match = bool(base_manifest and vanilla_manifest and base_manifest.get("source_sha256") == vanilla_manifest.get("source_sha256"))
    paired_image_match = bool(base_manifest and vanilla_manifest and base_manifest.get("docker_image") == vanilla_manifest.get("docker_image"))
    coverage_valid = bool(base_run["valid"] and vanilla_run["valid"] and paired_config_match and paired_source_match and paired_image_match)
    result = {
        **record,
        "attempt": attempt,
        "coverage_status": "complete" if coverage_valid else ("timeout" if base_run["timeout"] or vanilla_run["timeout"] else "coverage_failed"),
        "coverage_runner_exit_code": vanilla_run["runner_exit_code"],
        "container_exit_code": vanilla_manifest.get("container_exit_code") if vanilla_manifest else None,
        "base_container_exit_code": base_manifest.get("container_exit_code") if base_manifest else None,
        "source_sha256": vanilla_manifest.get("source_sha256") if vanilla_manifest else None,
        "base_source_sha256": base_manifest.get("source_sha256") if base_manifest else None,
        "coverage_config_sha256": vanilla_manifest.get("coverage_config_sha256") if vanilla_manifest else None,
        "base_coverage_config_sha256": base_manifest.get("coverage_config_sha256") if base_manifest else None,
        "docker_image": vanilla_manifest.get("docker_image") if vanilla_manifest else record.get("docker_image"),
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
        "coverage_manifest_path": str((coverage_dir / "coverage_run_manifest.json").relative_to(ROOT)).replace("\\", "/") if vanilla_manifest else None,
        "base_coverage_dir": str(base_coverage_dir.relative_to(ROOT)).replace("\\", "/"),
        "base_coverage_manifest_path": str((base_coverage_dir / "coverage_run_manifest.json").relative_to(ROOT)).replace("\\", "/") if base_manifest else None,
        "test_exit_nonzero_but_coverage_collected": bool(vanilla_run["valid"] and vanilla_manifest and vanilla_manifest.get("container_exit_code") != 0),
        "base_test_exit_nonzero_but_coverage_collected": bool(base_run["valid"] and base_manifest and base_manifest.get("container_exit_code") != 0),
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json(existing_record, result)
    return result


def percent(numerator: int, denominator: int) -> float | None:
    return numerator / denominator * 100.0 if denominator else None


def summarize_group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"task_count": 0}
    vanilla_lines = sum(int(row["coverage_totals"].get("covered_lines", 0)) for row in rows)
    vanilla_statements = sum(int(row["coverage_totals"].get("num_statements", 0)) for row in rows)
    base_lines = sum(int(row["base_coverage_totals"].get("covered_lines", 0)) for row in rows)
    base_statements = sum(int(row["base_coverage_totals"].get("num_statements", 0)) for row in rows)
    vanilla_branches = sum(int(row["coverage_totals"].get("covered_branches", 0)) for row in rows)
    vanilla_branch_total = sum(int(row["coverage_totals"].get("num_branches", 0)) for row in rows)
    base_branches = sum(int(row["base_coverage_totals"].get("covered_branches", 0)) for row in rows)
    base_branch_total = sum(int(row["base_coverage_totals"].get("num_branches", 0)) for row in rows)
    line_deltas = [float(row["line_delta_percentage_points"]) for row in rows]
    branch_deltas = [float(row["branch_delta_percentage_points"]) for row in rows if row.get("branch_delta_percentage_points") is not None]
    return {
        "task_count": len(rows),
        "base_weighted_line_coverage_percent": percent(base_lines, base_statements),
        "vanilla_weighted_line_coverage_percent": percent(vanilla_lines, vanilla_statements),
        "weighted_line_delta_percentage_points": percent(vanilla_lines, vanilla_statements) - percent(base_lines, base_statements),
        "base_weighted_branch_coverage_percent": percent(base_branches, base_branch_total),
        "vanilla_weighted_branch_coverage_percent": percent(vanilla_branches, vanilla_branch_total),
        "weighted_branch_delta_percentage_points": percent(vanilla_branches, vanilla_branch_total) - percent(base_branches, base_branch_total) if base_branch_total and vanilla_branch_total else None,
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


def aggregate(task_rows: list[dict[str, Any]], baseline_rows: dict[str, dict[str, Any]], expected_task_count: int, output_root: Path) -> dict[str, Any]:
    joined: list[dict[str, Any]] = []
    for row in task_rows:
        if row.get("coverage_status") != "complete":
            joined.append({**row, "line_delta_percentage_points": None, "branch_delta_percentage_points": None})
            continue
        baseline = baseline_rows[row["task_id"]]
        source_hashes_match = baseline.get("source_sha256") == row.get("source_sha256")
        base_tests_match = baseline.get("base_tests_sha256") == row.get("base_tests_sha256")
        paired_source_hashes_match = row.get("base_source_sha256") == row.get("source_sha256")
        if not all((source_hashes_match, base_tests_match, paired_source_hashes_match, row.get("paired_config_match") is True, row.get("paired_image_match") is True)):
            raise ValueError(f"{row['task_id']}: paired coverage or baseline source/test hashes do not match")
        totals = row["coverage_totals"]
        base_totals = row["base_coverage_totals"]
        joined.append({
            **row,
            "line_delta_percentage_points": round(float(totals["percent_statements_covered"]) - float(base_totals["percent_statements_covered"]), 6),
            "branch_delta_percentage_points": round(float(totals["percent_branches_covered"]) - float(base_totals["percent_branches_covered"]), 6) if totals.get("percent_branches_covered") is not None and base_totals.get("percent_branches_covered") is not None else None,
            "source_hashes_match": source_hashes_match,
            "base_tests_hashes_match": base_tests_match,
            "paired_source_hashes_match": paired_source_hashes_match,
        })
    complete = [row for row in joined if row.get("coverage_status") == "complete"]
    added = [row for row in complete if row.get("generated_test_count", 0) > 0]
    no_added = [row for row in complete if row.get("generated_test_count", 0) == 0]
    summary = {
        "format": "rq2-vanilla-c1-coverage/v1",
        "status": "complete" if len(complete) == expected_task_count else "partial",
        "suite_cohort": VANILLA_SUFFIX,
        "scope": "C1 line and branch coverage of legal base tests versus the audited frozen Vanilla v5 suite; descriptive coverage increment only",
        "coverage_metric_definitions": {
            "line": "coverage.py percent_statements_covered = covered_lines / num_statements * 100",
            "branch": "coverage.py percent_branches_covered = covered_branches / num_branches * 100",
            "delta": "Vanilla percentage minus base-suite percentage, in percentage points",
        },
        "expected_task_count": expected_task_count,
        "completed_task_count": len(complete),
        "failed_or_missing_task_count": len(joined) - len(complete),
        "generated_test_count": sum(int(row.get("generated_test_count", 0)) for row in joined),
        "generated_test_task_count": sum(int(row.get("generated_test_count", 0)) > 0 for row in joined),
        "all_tasks": summarize_group(complete),
        "tasks_with_vanilla_tests": summarize_group(added),
        "tasks_without_vanilla_tests": summarize_group(no_added),
        "task_rows": joined,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json(output_root / "rq2_vanilla_coverage_summary.json", summary)

    def f(value: Any) -> str:
        return "n/a" if value is None else f"{float(value):.2f}%"

    def pp(value: Any) -> str:
        return "n/a" if value is None else f"{float(value):.2f} pp"

    overall = summary["all_tasks"]
    added_summary = summary["tasks_with_vanilla_tests"]
    report = [
        "# Vanilla C1 coverage evaluation",
        "",
        "**Scope:** paired coverage comparison between each task's legal C1 base suite and its audited frozen Vanilla v5 suite. Both suites are rerun on the unchanged C1 source with the same branch-coverage runner, image, and per-task configuration.",
        "",
        f"**Status:** {summary['completed_task_count']}/{expected_task_count} tasks complete; {summary['generated_test_count']} retained Vanilla tests across {summary['generated_test_task_count']} tasks.",
        "",
        "## Cohort summary",
        "",
        f"- All evaluated tasks: weighted line coverage **{f(overall.get('base_weighted_line_coverage_percent'))} → {f(overall.get('vanilla_weighted_line_coverage_percent'))}** (Δ {pp(overall.get('weighted_line_delta_percentage_points'))}); weighted branch coverage **{f(overall.get('base_weighted_branch_coverage_percent'))} → {f(overall.get('vanilla_weighted_branch_coverage_percent'))}** (Δ {pp(overall.get('weighted_branch_delta_percentage_points'))}).",
        f"- Tasks with retained Vanilla tests ({added_summary.get('task_count', 0)}): weighted line coverage **{f(added_summary.get('base_weighted_line_coverage_percent'))} → {f(added_summary.get('vanilla_weighted_line_coverage_percent'))}** (Δ {pp(added_summary.get('weighted_line_delta_percentage_points'))}); weighted branch coverage **{f(added_summary.get('base_weighted_branch_coverage_percent'))} → {f(added_summary.get('vanilla_weighted_branch_coverage_percent'))}** (Δ {pp(added_summary.get('weighted_branch_delta_percentage_points'))}).",
        "",
        "## Interpretation boundary",
        "",
        "Positive deltas show additional C1 source lines or branches executed by the frozen Vanilla suite over the shared base suite. This is a descriptive coverage contribution, not an estimate of later-regression detection or a controlled comparison of test-generation budgets.",
        "",
        "The machine-readable per-task totals, hashes, coverage JSON, and runner logs are retained alongside this report.",
        "",
    ]
    (output_root / "rq2_vanilla_coverage_report.md").write_text("\n".join(report), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", nargs="*", help="Task IDs to run; omit for all 36 tasks")
    parser.add_argument("--preflight-only", action="store_true", help="Validate inputs and hashes without running Docker")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    args = parser.parse_args()
    output_root = (args.output_root or DEFAULT_OUTPUT).resolve()
    runtime_root = (args.runtime_root or DEFAULT_RUNTIME_ROOT).resolve()
    allowed_results = (ROOT / "artifacts" / "results").resolve(strict=True)
    allowed_tmp = (ROOT / "tmp").resolve(strict=True)
    if not output_root.is_relative_to(allowed_results):
        raise ValueError("output root must remain under artifacts/results")
    if not runtime_root.is_relative_to(allowed_tmp):
        raise ValueError("runtime root must remain under repository tmp")
    output_root.mkdir(parents=True, exist_ok=True)
    runtime_root.mkdir(parents=True, exist_ok=True)

    all_tasks = [row["task_id"] for row in read_json(ROOT / "config" / "rq1_full_36x1_candidate_units.json")["units"]]
    selected_tasks = args.tasks or all_tasks
    unknown = sorted(set(selected_tasks) - set(all_tasks))
    if unknown:
        raise ValueError(f"unknown task IDs: {unknown}")
    baseline_rows = load_baseline_rows()
    vanilla_rows = load_vanilla_audit_rows()
    prepared: list[dict[str, Any]] = []
    for task in selected_tasks:
        bundle = input_bundle(task, vanilla_rows[task])
        if baseline_rows[task].get("base_tests_sha256") != bundle["record"]["base_tests_sha256"]:
            raise ValueError(f"{task}: base coverage and Vanilla frozen suite use different base-test hashes")
        prepared.append(bundle)
    preflight = {
        "format": "rq2-vanilla-coverage-preflight/v1",
        "status": "complete",
        "suite_cohort": VANILLA_SUFFIX,
        "selected_tasks": selected_tasks,
        "task_count": len(selected_tasks),
        "generated_test_count": sum(bundle["record"]["generated_test_count"] for bundle in prepared),
        "generated_test_task_count": sum(bundle["record"]["generated_test_count"] > 0 for bundle in prepared),
        "all_inputs_match_c1_and_baseline_hashes": True,
        "baseline_coverage_task_count": len(baseline_rows),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "task_inputs": [bundle["record"] for bundle in prepared],
    }
    write_json(output_root / "rq2_vanilla_coverage_preflight.json", preflight)
    print(json.dumps({key: preflight[key] for key in ("status", "task_count", "generated_test_count", "generated_test_task_count", "all_inputs_match_c1_and_baseline_hashes")}, ensure_ascii=False), flush=True)
    if args.preflight_only:
        return 0

    run_rows: list[dict[str, Any]] = []
    for index, bundle in enumerate(prepared, 1):
        task = bundle["record"]["task_id"]
        row = materialize_and_run(task, bundle, output_root, runtime_root, args.timeout_seconds)
        run_rows.append(row)
        print(f"[{index}/{len(prepared)}] {task}: {row['coverage_status']} line={row.get('line_coverage_percent')} branch={row.get('branch_coverage_percent')} container_exit={row.get('container_exit_code')}", flush=True)
        aggregate(run_rows, baseline_rows, len(selected_tasks), output_root)
    summary = aggregate(run_rows, baseline_rows, len(selected_tasks), output_root)
    print(json.dumps({
        "status": summary["status"],
        "completed_tasks": summary["completed_task_count"],
        "expected_tasks": summary["expected_task_count"],
        "all_tasks_weighted_line_delta_pp": summary["all_tasks"].get("weighted_line_delta_percentage_points"),
        "all_tasks_weighted_branch_delta_pp": summary["all_tasks"].get("weighted_branch_delta_percentage_points"),
        "report": str(output_root / "rq2_vanilla_coverage_report.md"),
    }, indent=2, ensure_ascii=False), flush=True)
    return 0 if summary["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())

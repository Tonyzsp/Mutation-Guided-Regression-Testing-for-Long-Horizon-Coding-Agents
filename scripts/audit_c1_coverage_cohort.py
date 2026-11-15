"""Verify hash-linked coverage reports for every sanitized RQ1 C1 package."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RESULT_ROOT = ROOT / "artifacts" / "results" / "rq1_full_36x1"
PACKAGE_ROOT = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
COHORT = RESULT_ROOT / "c1_coverage_cohort_20261019.json"
BASELINE = RESULT_ROOT / "c1_base_audit_isolation_revalidated_20261019" / "c1_base_suite_audit.json"
OUTPUT = RESULT_ROOT / "c1_coverage_integrity_audit_20261019.json"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def package_source_hashes(task: str, source_paths: list[str], workspace_path: str) -> dict[str, str]:
    workspace = PACKAGE_ROOT / task / "C1" / workspace_path
    return {
        relative: sha256_file(workspace / Path(*relative.split("/")))
        for relative in source_paths
    }


def main() -> int:
    cohort = read_json(COHORT)
    baseline = read_json(BASELINE)
    baseline_by_task = {row["task_id"]: row for row in baseline["records"]}
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in cohort.get("records", []):
        task = entry.get("task_id")
        try:
            if not isinstance(task, str) or task in seen:
                raise ValueError(f"missing or duplicate task ID: {task!r}")
            seen.add(task)
            if task not in baseline_by_task:
                raise ValueError("task is not present in the final C1 baseline audit")
            bundle = (ROOT / entry["coverage_bundle"]).resolve(strict=True)
            manifest = read_json(bundle / "coverage_run_manifest.json")
            package = read_json(PACKAGE_ROOT / task / "C1" / "package.json")
            baseline_row = baseline_by_task[task]
            expected_sources = package_source_hashes(task, manifest["source_paths"], package["workspace_path"])
            if manifest.get("format") != "rq1-checkpoint-coverage/v1":
                raise ValueError("unexpected coverage manifest format")
            if manifest.get("task_id") != task or manifest.get("checkpoint_id") != "C1":
                raise ValueError("coverage manifest identifies a different checkpoint")
            if manifest.get("base_tests_sha256") != baseline_row.get("base_tests_sha256"):
                raise ValueError("coverage report base-tests hash differs from final C1 baseline")
            if manifest.get("source_sha256") != expected_sources:
                raise ValueError("coverage report source hash differs from sanitized C1 package")
            if manifest.get("total_line_coverage_percent") is None or not (bundle / "coverage.json").is_file():
                raise ValueError("coverage data or line coverage summary is missing")
            if not (bundle / "coverage-report.txt").is_file():
                raise ValueError("coverage text report is missing")
            records.append({
                "task_id": task,
                "checkpoint_id": "C1",
                "status": "verified",
                "coverage_bundle": str(bundle.relative_to(ROOT)),
                "line_coverage_percent": manifest["total_line_coverage_percent"],
                "branch_coverage_percent": manifest.get("total_branch_coverage_percent"),
                "container_exit_code": manifest.get("container_exit_code"),
                "base_tests_sha256": manifest["base_tests_sha256"],
                "source_sha256": manifest["source_sha256"],
            })
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            errors.append({"task_id": str(task), "error": str(error)})

    missing = sorted(set(baseline_by_task) - seen)
    for task in missing:
        errors.append({"task_id": task, "error": "coverage cohort manifest has no record for this C1 task"})
    report = {
        "format": "rq1-c1-coverage-integrity-audit/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "task_count": len(records),
        "expected_task_count": len(baseline_by_task),
        "baseline_audit_sha256": sha256_file(BASELINE),
        "coverage_cohort_manifest_sha256": sha256_file(COHORT),
        "source_linkage_basis": "coverage report base-tests and source hashes match sanitized C1 packages; coverage uses the full official C1 base suite",
        "status": "complete" if len(records) == len(baseline_by_task) and not errors else "incomplete",
        "records": records,
        "errors": errors,
    }
    OUTPUT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "task_count", "expected_task_count", "errors")}, ensure_ascii=False))
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Prove each direct Mutmut bundle uses the current C1 source, base tests, and stable test IDs."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESULT_ROOT = ROOT / "artifacts" / "results" / "rq1_full_36x1"
DEFAULT_CURRENT_BASELINE = RESULT_ROOT / "c1_base_audit_current_sanitized_20261116" / "c1_base_suite_audit.json"
DEFAULT_MUTANT_AUDIT = RESULT_ROOT / "mutmut_cohort_audit_sanitized_linked_20261117.json"
DEFAULT_SURVIVOR_MANIFEST = RESULT_ROOT / "survivor_only_input_v1" / "manifest.json"
DEFAULT_OUTPUT = RESULT_ROOT / "guided_mutation_baseline_linkage_formal_v1_20261117.json"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_list(values: list[str]) -> str:
    payload = (json.dumps(sorted(values), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-baseline", type=Path, default=DEFAULT_CURRENT_BASELINE)
    parser.add_argument("--mutant-audit", type=Path, default=DEFAULT_MUTANT_AUDIT)
    parser.add_argument("--survivor-manifest", type=Path, default=DEFAULT_SURVIVOR_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    current_path = args.current_baseline.resolve(strict=True)
    mutant_audit_path = args.mutant_audit.resolve(strict=True)
    survivor_path = args.survivor_manifest.resolve(strict=True)
    output_path = args.output.resolve()
    if not output_path.is_relative_to(RESULT_ROOT.resolve(strict=True)):
        raise ValueError("linkage report must remain under the formal result root")
    current = read_json(current_path)
    mutant_audit = read_json(mutant_audit_path)
    survivors = read_json(survivor_path)
    if current.get("format") != "rq1-c1-base-suite-audit/v1" or len(current.get("records", [])) != 36:
        raise ValueError("current sanitized C1 baseline audit is not a complete 36-task baseline")
    if mutant_audit.get("format") != "rq1-c1-mutmut-cohort-audit/v1" or mutant_audit.get("complete") is not True:
        raise ValueError("linked direct-mutant audit is incomplete")
    if mutant_audit.get("audited_task_count") != 36 or survivors.get("task_count") != 36:
        raise ValueError("full-study mutant/baseline inputs must cover all 36 tasks")

    current_by_task = {row["task_id"]: row for row in current["records"]}
    mutant_by_task = {row["task_id"]: row for row in mutant_audit["records"]}
    tasks: dict[str, Any] = {}
    issues: list[str] = []
    for task_id, survivor_row in sorted(survivors["tasks"].items()):
        baseline = current_by_task.get(task_id)
        mutant_audit_row = mutant_by_task.get(task_id)
        task_baseline = mutant_audit.get("task_baseline_audits", {}).get(task_id)
        if baseline is None or mutant_audit_row is None or task_baseline is None:
            issues.append(f"{task_id}: missing baseline/mutant audit linkage record")
            continue
        bundle = (ROOT / survivor_row["mutation_bundle"]).resolve(strict=True)
        if not bundle.is_relative_to(RESULT_ROOT.resolve(strict=True)):
            issues.append(f"{task_id}: mutation bundle path escapes formal results")
            continue
        summary_path = bundle / "mutation_summary.json"
        direct_summary_path = bundle / "direct_mutant_classification_summary.json"
        records_path = bundle / "mutant_records.jsonl"
        if not all(path.is_file() for path in (summary_path, direct_summary_path, records_path)):
            issues.append(f"{task_id}: required mutation bundle file is missing")
            continue
        summary = read_json(summary_path)
        direct_summary = read_json(direct_summary_path)
        expected_selection = sorted(baseline.get("stable_pass_test_ids", []))
        actual_selection = sorted(summary.get("mutation_test_selection", []))
        expected_sources = survivor_row.get("source_sha256", {})
        bundle_sources = summary.get("source_sha256", {})
        row_issues: list[str] = []
        if mutant_audit_row.get("status") != "complete":
            row_issues.append("mutmut cohort audit did not mark bundle complete")
        if summary.get("complete") is not True or direct_summary.get("complete") is not True:
            row_issues.append("normalized/direct classification is incomplete")
        if summary.get("task_id") != task_id or summary.get("checkpoint_id") != "C1":
            row_issues.append("mutation bundle task/checkpoint identity mismatch")
        if summary.get("base_tests_sha256") != baseline.get("base_tests_sha256"):
            row_issues.append("base_tests hash differs from the current baseline")
        if bundle_sources != expected_sources:
            row_issues.append("source hashes differ from the frozen C1 survivor package")
        if actual_selection != expected_selection:
            row_issues.append("mutation test selection differs from current stable passing test IDs")
        if summary.get("baseline_audit_sha256") != task_baseline.get("sha256"):
            row_issues.append("mutation bundle audit hash differs from its audited task baseline")
        if direct_summary.get("baseline_audit_sha256") != task_baseline.get("sha256"):
            row_issues.append("direct classification audit hash differs from its audited task baseline")
        if summary.get("mutant_count") != mutant_audit_row.get("mutant_count"):
            row_issues.append("mutant count differs from the completed cohort audit")
        if row_issues:
            issues.extend(f"{task_id}: {message}" for message in row_issues)
        tasks[task_id] = {
            "status": "linked" if not row_issues else "invalid",
            "mutation_bundle": str(bundle.relative_to(ROOT).as_posix()),
            "mutation_summary_sha256": sha256_file(summary_path),
            "direct_classification_summary_sha256": sha256_file(direct_summary_path),
            "mutation_records_sha256": sha256_file(records_path),
            "mutant_cohort_audit_record_status": mutant_audit_row.get("status"),
            "mutation_baseline_audit_path": task_baseline.get("path"),
            "mutation_baseline_audit_sha256": task_baseline.get("sha256"),
            "current_baseline_audit_sha256": sha256_file(current_path),
            "current_base_tests_sha256": baseline.get("base_tests_sha256"),
            "mutation_test_selection_count": len(actual_selection),
            "current_stable_pass_test_count": len(expected_selection),
            "mutation_test_selection_sha256": sha256_list(actual_selection),
            "current_stable_pass_test_ids_sha256": sha256_list(expected_selection),
            "source_sha256": bundle_sources,
            "source_sha256_matches_frozen_package": bundle_sources == expected_sources,
            "base_tests_match_current_baseline": summary.get("base_tests_sha256") == baseline.get("base_tests_sha256"),
            "stable_test_selection_matches_current_baseline": actual_selection == expected_selection,
            "issues": row_issues,
        }

    report = {
        "format": "rq1-guided-mutant-baseline-linkage/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "task_count": len(tasks),
        "expected_task_count": 36,
        "complete": len(tasks) == 36 and not issues,
        "current_baseline_audit_path": str(current_path.relative_to(ROOT).as_posix()),
        "current_baseline_audit_sha256": sha256_file(current_path),
        "mutant_cohort_audit_path": str(mutant_audit_path.relative_to(ROOT).as_posix()),
        "mutant_cohort_audit_sha256": sha256_file(mutant_audit_path),
        "survivor_manifest_sha256": sha256_file(survivor_path),
        "compatibility_rule": "Each complete mutant bundle must match the current C1 package source hashes, base_tests tree hash, and exact set of stable passing C1 test IDs. The original per-task baseline audit hash is retained as provenance and linked through the complete direct-mutant audit.",
        "status_counts": {"linked": sum(row["status"] == "linked" for row in tasks.values()), "invalid": sum(row["status"] != "linked" for row in tasks.values())},
        "tasks": tasks,
        "issues": issues,
    }
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"task_count": len(tasks), "complete": report["complete"], "status_counts": report["status_counts"], "issues": issues}, ensure_ascii=False))
    print(f"Baseline linkage report: {output_path}")
    if not report["complete"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

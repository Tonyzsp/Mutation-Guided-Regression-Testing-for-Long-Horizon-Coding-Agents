"""Combine v1 and recovery-v2 Mutation-Guided C1 coverage measurements."""

from __future__ import annotations

import hashlib
import json
import statistics
from pathlib import Path, PurePosixPath
from typing import Any

from validate_and_freeze_tests import filesystem_path, hash_tree


ROOT = Path(__file__).resolve().parents[1]
V1_RESULT = ROOT / "artifacts/results/rq1_full_36x1/rq2_mg_coverage_v1_20261118/rq2_mg_coverage_summary.json"
V1_TASK_ROOT = ROOT / "artifacts/results/rq1_full_36x1"
V1_SUFFIX = "mutation_guided_formal_v1_baseline_linked_20261117"
V2_ROOT = ROOT / "artifacts/results/rq1_recovery_v2"
V2_SUITE_SUMMARY = V2_ROOT / "mg_composite_suite_summary.json"
V2_COVERAGE = V2_ROOT / "rq2_mg_composite_coverage_v2_20261118/rq2_mg_coverage_summary.json"
OUTPUT_ROOT = V2_ROOT / "rq2_mg_combined_v1_v2_coverage_20261118"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_relative(value: str, field: str) -> PurePosixPath:
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or "\\" in value:
        raise ValueError(f"unsafe {field}: {value!r}")
    return relative


def generated_hashes(generation_dir: Path, frozen: dict[str, Any]) -> list[str]:
    suite_relative = safe_relative(str(frozen["suite_path"]), "suite_path")
    suite_root = generation_dir.joinpath(*suite_relative.parts)
    if hash_tree(suite_root) != frozen.get("suite_sha256"):
        raise ValueError(f"{generation_dir}: frozen suite hash mismatch")
    hashes: list[str] = []
    for value in frozen.get("generated_test_paths", []):
        relative = safe_relative(str(value), "generated_test_path")
        if relative.parts[: len(suite_relative.parts)] != suite_relative.parts:
            raise ValueError(f"{generation_dir}: generated test is outside frozen suite")
        path = generation_dir.joinpath(*relative.parts)
        with open(filesystem_path(path), "rb") as handle:
            hashes.append(hashlib.sha256(handle.read()).hexdigest())
    return sorted(hashes)


def percent(numerator: int, denominator: int) -> float | None:
    return numerator * 100.0 / denominator if denominator else None


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if any(row.get("coverage_status") != "complete" for row in rows):
        raise ValueError("cannot combine incomplete coverage rows")

    def group(items: list[dict[str, Any]]) -> dict[str, Any]:
        if not items:
            return {"task_count": 0}
        base_lines = sum(int(row["base_coverage_totals"].get("covered_lines", 0)) for row in items)
        line_total = sum(int(row["base_coverage_totals"].get("num_statements", 0)) for row in items)
        mg_lines = sum(int(row["coverage_totals"].get("covered_lines", 0)) for row in items)
        mg_line_total = sum(int(row["coverage_totals"].get("num_statements", 0)) for row in items)
        base_branches = sum(int(row["base_coverage_totals"].get("covered_branches", 0)) for row in items)
        branch_total = sum(int(row["base_coverage_totals"].get("num_branches", 0)) for row in items)
        mg_branches = sum(int(row["coverage_totals"].get("covered_branches", 0)) for row in items)
        mg_branch_total = sum(int(row["coverage_totals"].get("num_branches", 0)) for row in items)
        line_deltas = [float(row["line_delta_percentage_points"]) for row in items]
        branch_deltas = [float(row["branch_delta_percentage_points"]) for row in items if row.get("branch_delta_percentage_points") is not None]
        base_line_percent, mg_line_percent = percent(base_lines, line_total), percent(mg_lines, mg_line_total)
        base_branch_percent, mg_branch_percent = percent(base_branches, branch_total), percent(mg_branches, mg_branch_total)
        return {
            "task_count": len(items),
            "base_weighted_line_coverage_percent": base_line_percent,
            "mutation_guided_weighted_line_coverage_percent": mg_line_percent,
            "weighted_line_delta_percentage_points": mg_line_percent - base_line_percent,
            "base_weighted_branch_coverage_percent": base_branch_percent,
            "mutation_guided_weighted_branch_coverage_percent": mg_branch_percent,
            "weighted_branch_delta_percentage_points": mg_branch_percent - base_branch_percent,
            "macro_mean_line_delta_percentage_points": statistics.mean(line_deltas),
            "macro_median_line_delta_percentage_points": statistics.median(line_deltas),
            "macro_mean_branch_delta_percentage_points": statistics.mean(branch_deltas) if branch_deltas else None,
            "macro_median_branch_delta_percentage_points": statistics.median(branch_deltas) if branch_deltas else None,
            "tasks_with_line_coverage_increase": sum(value > 0 for value in line_deltas),
            "tasks_with_no_line_coverage_change": sum(value == 0 for value in line_deltas),
            "tasks_with_line_coverage_decrease": sum(value < 0 for value in line_deltas),
            "tasks_with_branch_coverage_increase": sum(value > 0 for value in branch_deltas),
            "tasks_with_no_branch_coverage_change": sum(value == 0 for value in branch_deltas),
            "tasks_with_branch_coverage_decrease": sum(value < 0 for value in branch_deltas),
        }

    return group(rows)


def main() -> int:
    v1 = read_json(V1_RESULT)
    suite_summary = read_json(V2_SUITE_SUMMARY)
    v2_coverage = read_json(V2_COVERAGE)
    if v1.get("status") != "complete" or v1.get("completed_task_count") != 36:
        raise ValueError("v1 coverage cohort is not a complete 36-task run")
    if v2_coverage.get("status") != "complete":
        raise ValueError("recovery-v2 changed-task coverage run is incomplete")

    v1_rows = {row["task_id"]: row for row in v1["task_rows"]}
    v2_rows = {row["task_id"]: row for row in v2_coverage["task_rows"]}
    composite_rows = {row["task_id"]: row for row in suite_summary["records"]}
    if not (len(v1_rows) == len(composite_rows) == 36):
        raise ValueError("expected 36 task records in v1 and composite suite manifests")
    changed_tasks = {task for task, row in composite_rows.items() if int(row["v2_test_count"]) > 0}
    if changed_tasks != set(v2_rows):
        raise ValueError("fresh composite coverage rows must exactly cover tasks with added v2 tests")
    if sum(int(row["generated_test_count"]) for row in composite_rows.values()) != 25:
        raise ValueError("expected 25 unique MG tests in the composite cohort")

    selected_rows: list[dict[str, Any]] = []
    for task in sorted(v1_rows):
        old = v1_rows[task]
        suite_record = composite_rows[task]
        v1_dir = V1_TASK_ROOT / task / "C1" / V1_SUFFIX
        v2_dir = V2_ROOT / task / "C1" / "mutation_guided_composite_v2"
        v1_frozen = read_json(v1_dir / "frozen_test_suite.json")
        composite_frozen = read_json(v2_dir / "frozen_test_suite.json")
        if composite_frozen.get("suite_sha256") != suite_record.get("suite_sha256"):
            raise ValueError(f"{task}: composite manifest and cohort summary suite hashes differ")
        if composite_frozen.get("metadata", {}).get("v1_frozen_suite_sha256") != v1_frozen.get("suite_sha256"):
            raise ValueError(f"{task}: composite suite does not link to its v1 frozen suite")
        # Some v1 suite copies no longer contain the generated source files.
        # The contemporaneous RQ2 row preserves their per-file SHA-256 values.
        old_hashes = sorted(test["sha256"] for test in old.get("generated_tests", []))
        composite_hashes = generated_hashes(v2_dir, composite_frozen)
        if not set(old_hashes).issubset(set(composite_hashes)):
            raise ValueError(f"{task}: composite suite does not preserve all v1 generated test files")
        if len(old_hashes) != int(suite_record["v1_test_count"]) or len(old_hashes) != int(old.get("generated_test_count", -1)):
            raise ValueError(f"{task}: v1 test count differs from the composite manifest")
        if len(composite_hashes) != int(suite_record["generated_test_count"]):
            raise ValueError(f"{task}: composite test count differs from its frozen manifest")

        if task in changed_tasks:
            row = v2_rows[task]
            if row.get("suite_sha256") != composite_frozen.get("suite_sha256"):
                raise ValueError(f"{task}: fresh coverage row used a different frozen suite")
            for key in ("source_sha256", "coverage_config_sha256", "docker_image", "base_tests_sha256"):
                old_key = "base_tests_sha256" if key == "base_tests_sha256" else key
                if row.get(old_key) != old.get(old_key):
                    raise ValueError(f"{task}: v1 and v2 coverage runs differ in {key}")
            if not row.get("paired_source_match") or not row.get("paired_config_match") or not row.get("paired_image_match"):
                raise ValueError(f"{task}: v2 paired source/config/image checks did not pass")
            base_metric_keys = (
                "covered_lines",
                "num_statements",
                "covered_branches",
                "num_branches",
                "percent_statements_covered",
                "percent_branches_covered",
            )
            old_base = old.get("base_coverage_totals", {})
            new_base = row.get("base_coverage_totals", {})
            if any(old_base.get(key) != new_base.get(key) for key in base_metric_keys):
                raise ValueError(f"{task}: rerun base-suite coverage differs from its v1 measurement")
            row = dict(row)
            row["coverage_source"] = "fresh_composite_v2_pair"
        else:
            if old.get("generated_test_count") != len(composite_hashes):
                raise ValueError(f"{task}: unchanged composite suite differs in generated test count")
            if old_hashes != composite_hashes:
                raise ValueError(f"{task}: v1 coverage is not reusable; generated test content changed")
            row = dict(old)
            row["coverage_source"] = "reused_v1_pair_identical_composite_suite"

        row["generated_test_count"] = int(suite_record["generated_test_count"])
        row["v1_test_count"] = int(suite_record["v1_test_count"])
        row["v2_test_count"] = int(suite_record["v2_test_count"])
        row["suite_sha256"] = composite_frozen["suite_sha256"]
        selected_rows.append(row)

    all_rows = sorted(selected_rows, key=lambda row: row["task_id"])
    added_rows = [row for row in all_rows if row["generated_test_count"] > 0]
    no_added_rows = [row for row in all_rows if row["generated_test_count"] == 0]
    base_nonzero = {row["task_id"] for row in all_rows if row.get("base_test_exit_nonzero_but_coverage_collected")}
    mg_nonzero = {row["task_id"] for row in all_rows if row.get("test_exit_nonzero_but_coverage_collected")}
    summary = {
        "format": "rq2-mg-combined-c1-coverage/v1",
        "status": "complete",
        "suite_cohort": "v1_plus_recovery_v2_composite",
        "task_count": 36,
        "generated_test_count": 25,
        "generated_test_task_count": len(added_rows),
        "freshly_rerun_tasks": sorted(changed_tasks),
        "freshly_rerun_task_count": len(changed_tasks),
        "reused_v1_task_count": 36 - len(changed_tasks),
        "reuse_rule": "A v1 paired measurement is reused only when the v2 composite manifest links the exact v1 suite hash and the generated test file hashes and counts are identical.",
        "all_tasks": summarize(all_rows),
        "tasks_with_guided_tests": summarize(added_rows),
        "tasks_without_guided_tests": summarize(no_added_rows),
        "base_test_runner_nonzero_with_coverage_available": len(base_nonzero),
        "mutation_guided_runner_nonzero_with_coverage_available": len(mg_nonzero),
        "nonzero_exit_status_overlap_task_count": len(base_nonzero & mg_nonzero),
        "base_only_nonzero_exit_task_count": len(base_nonzero - mg_nonzero),
        "mutation_guided_only_nonzero_exit_task_count": len(mg_nonzero - base_nonzero),
        "task_rows": all_rows,
    }
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    (OUTPUT_ROOT / "rq2_mg_combined_coverage_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    def fmt(value: Any) -> str:
        return "n/a" if value is None else f"{float(value):.2f}%"

    def delta(value: Any) -> str:
        return "n/a" if value is None else f"{float(value):.2f} pp"

    all_summary = summary["all_tasks"]
    added_summary = summary["tasks_with_guided_tests"]
    lines = [
        "# Combined Mutation-Guided RQ2 C1 coverage analysis (v1 + recovery v2)",
        "",
        "**Scope:** legal C1 base tests versus the frozen composite MG suite (v1 plus v2 additions). This is a within-MG-suite coverage increment, not a comparison against Original or Vanilla tests.",
        "",
        "Line coverage uses `percent_statements_covered`; branch coverage uses `percent_branches_covered`. Deltas are percentage points.",
        "",
        f"**Cohort:** 36 tasks; 25 MG tests across {summary['generated_test_task_count']} tasks. Fresh paired runs were performed for the {len(changed_tasks)} tasks receiving v2 tests; the remaining {36-len(changed_tasks)} v1 pairs were reused only after exact suite and generated-file hash checks.",
        "",
        "## Overall coverage increment",
        "",
        f"- All 36 tasks: weighted line coverage **{fmt(all_summary['base_weighted_line_coverage_percent'])} → {fmt(all_summary['mutation_guided_weighted_line_coverage_percent'])}** (Δ **{delta(all_summary['weighted_line_delta_percentage_points'])}**); weighted branch coverage **{fmt(all_summary['base_weighted_branch_coverage_percent'])} → {fmt(all_summary['mutation_guided_weighted_branch_coverage_percent'])}** (Δ **{delta(all_summary['weighted_branch_delta_percentage_points'])}**).",
        f"- Macro mean/median task delta: line **{delta(all_summary['macro_mean_line_delta_percentage_points'])} / {delta(all_summary['macro_median_line_delta_percentage_points'])}**; branch **{delta(all_summary['macro_mean_branch_delta_percentage_points'])} / {delta(all_summary['macro_median_branch_delta_percentage_points'])}**.",
        f"- Line coverage increased / unchanged / decreased: **{all_summary['tasks_with_line_coverage_increase']} / {all_summary['tasks_with_no_line_coverage_change']} / {all_summary['tasks_with_line_coverage_decrease']} tasks**. Branch: **{all_summary['tasks_with_branch_coverage_increase']} / {all_summary['tasks_with_no_branch_coverage_change']} / {all_summary['tasks_with_branch_coverage_decrease']} tasks**.",
        f"- Among the {added_summary['task_count']} tasks with MG tests: weighted line Δ **{delta(added_summary['weighted_line_delta_percentage_points'])}**; weighted branch Δ **{delta(added_summary['weighted_branch_delta_percentage_points'])}**.",
        f"- Nonzero test-run exits with valid coverage: base **{summary['base_test_runner_nonzero_with_coverage_available']}/36**, composite MG **{summary['mutation_guided_runner_nonzero_with_coverage_available']}/36**; overlap **{summary['nonzero_exit_status_overlap_task_count']}**.",
        "",
        "## Per-task results",
        "",
        "| Task | MG tests | v1 | v2 | Base line | MG line | Δ pp | Base branch | MG branch | Δ pp | Coverage source |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in all_rows:
        lines.append(
            f"| {row['task_id']} | {row['generated_test_count']} | {row['v1_test_count']} | {row['v2_test_count']} | "
            f"{fmt(row.get('base_line_coverage_percent'))} | {fmt(row.get('line_coverage_percent'))} | "
            f"{float(row['line_delta_percentage_points']):.2f} | {fmt(row.get('base_branch_coverage_percent'))} | "
            f"{fmt(row.get('branch_coverage_percent'))} | {float(row['branch_delta_percentage_points']):.2f} | {row['coverage_source']} |"
        )
    lines.extend([
        "",
        "## Interpretation and limits",
        "",
        "The composite MG tests add a small positive amount of C1 line and branch coverage overall; most tasks have no coverage change. Coverage gain does not establish improved later-checkpoint regression detection or that the covered code caused confirmed detections.",
        "",
        "The RQ2 coverage-control comparison is still incomplete: Original and Vanilla suites have not been measured under the same paired coverage protocol. Interpret these results as MG-only descriptive coverage increments.",
        "",
        "Source task measurements and hash-checked suite composition are recorded in `rq2_mg_combined_coverage_summary.json`; fresh v2 runs and logs are under `artifacts/results/rq1_recovery_v2/rq2_mg_composite_coverage_v2_20261118/`.",
        "",
    ])
    (OUTPUT_ROOT / "rq2_mg_combined_coverage_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({
        "status": summary["status"],
        "task_count": summary["task_count"],
        "generated_test_count": summary["generated_test_count"],
        "generated_test_task_count": summary["generated_test_task_count"],
        "weighted_line_delta_pp": all_summary["weighted_line_delta_percentage_points"],
        "weighted_branch_delta_pp": all_summary["weighted_branch_delta_percentage_points"],
        "fresh_v2_pairs": len(changed_tasks),
        "reused_v1_pairs": 36 - len(changed_tasks),
        "report": str(OUTPUT_ROOT / "rq2_mg_combined_coverage_report.md"),
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

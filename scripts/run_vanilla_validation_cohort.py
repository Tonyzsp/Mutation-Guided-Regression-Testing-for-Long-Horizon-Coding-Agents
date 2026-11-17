"""Independently review, validate, and freeze the formal Vanilla C1 cohort."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from validate_and_freeze_tests import read_json, sha256_file


ROOT = Path(__file__).resolve().parents[1]
RESULT_ROOT = ROOT / "artifacts" / "results" / "rq1_full_36x1"
PACKAGE_ROOT = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
UNITS_PATH = ROOT / "config" / "rq1_full_36x1_candidate_units.json"
AUDIT_PATH = RESULT_ROOT / "c1_base_audit_current_sanitized_20261116" / "c1_base_suite_audit.json"
VALIDATOR = ROOT / "scripts" / "validate_and_freeze_tests.py"
REVIEWER = ROOT / "scripts" / "review_generated_candidates.py"
PREPARE_RUNTIME = ROOT / "scripts" / "prepare_checkpoint_runtime.py"
BUDGET_PATH = ROOT / "config" / "generation_budget_rq1_c1_cohort_formal.json"
STUDY_BUDGET_PATH = ROOT / "config" / "rq1_full_study_budget_v1.json"
DEFAULT_SUFFIX = "vanilla_formal_v5_20261116"


def write_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def prepare_local_runtime(task: str) -> Path:
    """Stage the sanitized package locally when a handoff audit has another host's path."""
    result = subprocess.run(
        [
            sys.executable, str(PREPARE_RUNTIME),
            "--package", str(PACKAGE_ROOT / task / "C1"),
            "--problems-root", str(ROOT / "external" / "scb-problems"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    marker = "Prepared isolated checkpoint context: "
    stage_line = next((line for line in result.stdout.splitlines() if line.startswith(marker)), None)
    if result.returncode != 0 or stage_line is None:
        raise RuntimeError(f"local runtime staging failed: {result.stderr[-2000:] or result.stdout[-2000:]}")
    stage = Path(stage_line[len(marker):]).resolve(strict=True)
    if not (stage / "runtime_manifest.json").is_file():
        raise RuntimeError("local runtime staging did not create runtime_manifest.json")
    return stage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-suffix", default=DEFAULT_SUFFIX)
    parser.add_argument(
        "--cohort-log-name",
        help="Optional JSONL filename under the result root; useful when independent task batches run concurrently.",
    )
    parser.add_argument("--task", action="append", help="Limit to task IDs; may be repeated")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--review-timeout-seconds", type=int, default=900)
    parser.add_argument("--baseline-audit", type=Path, default=AUDIT_PATH)
    parser.add_argument("--budget-config", type=Path, default=BUDGET_PATH)
    parser.add_argument("--study-budget-config", type=Path, default=STUDY_BUDGET_PATH)
    args = parser.parse_args()

    units = read_json(UNITS_PATH)["units"]
    audit_path = args.baseline_audit.resolve(strict=True)
    baseline = read_json(audit_path)
    baseline_by_task = {row["task_id"]: row for row in baseline["records"]}
    expected_prompt = {
        "path": "prompts/vanilla_gap_v4.md",
        "sha256": sha256_file(ROOT / "prompts" / "vanilla_gap_v4.md"),
    }
    expected_budget = {
        "path": "config/generation_budget_rq1_c1_cohort_formal.json",
        "sha256": sha256_file(args.budget_config.resolve(strict=True)),
    }
    expected_study_budget = {
        "path": args.study_budget_config.resolve(strict=True).relative_to(ROOT).as_posix(),
        "sha256": sha256_file(args.study_budget_config.resolve(strict=True)),
    }
    budget = read_json(args.budget_config.resolve(strict=True))
    selected = [unit for unit in units if not args.task or unit["task_id"] in args.task]
    unknown = sorted(set(args.task or []) - {unit["task_id"] for unit in units})
    if unknown:
        raise ValueError(f"unknown task IDs: {unknown}")
    acceptable_baseline_statuses = {"stable_pass", "stable_with_failures"}
    if len(baseline_by_task) != 36 or any(
        row.get("status") not in acceptable_baseline_statuses for row in baseline_by_task.values()
    ):
        raise ValueError("the repeated 36-task C1 base-suite audit is incomplete")

    log_name = args.cohort_log_name or f"vanilla_validation_cohort_{args.output_suffix}.jsonl"
    if Path(log_name).name != log_name or log_name in {".", ".."}:
        raise ValueError("cohort log name must be a filename, not a path")
    log_path = RESULT_ROOT / log_name
    failures = 0
    for index, unit in enumerate(selected, 1):
        task = unit["task_id"]
        generation_dir = RESULT_ROOT / task / "C1" / args.output_suffix
        row: dict[str, Any] = {
            "task_id": task,
            "checkpoint_id": "C1",
            "generation_directory": str(generation_dir.relative_to(ROOT)),
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        try:
            package = read_json(PACKAGE_ROOT / task / "C1" / "package.json")
            generation = read_json(generation_dir / "generation_run.json")
            input_manifest = read_json(generation_dir / "input_manifest.json")
            response = read_json(generation_dir / "candidate_response.json")
            baseline_record = baseline_by_task[task]
            if generation.get("codex_exit_code") != 0:
                raise ValueError("generation run did not complete successfully")
            if generation.get("prompt_template_version") != "c1_shared_gap_v4_cohort_v1":
                raise ValueError("generation run did not use the shared v4 prompt")
            if generation.get("budget_id") != "rq1-c1-cohort-gpt6-luna-medium-3calls-12288out-8candidates-repeat3-v1":
                raise ValueError("generation budget ID differs from the formal paired budget")
            if input_manifest.get("prompt_template") != expected_prompt:
                raise ValueError("generation input manifest does not identify the current v4 prompt file")
            if input_manifest.get("budget_config") != expected_budget:
                raise ValueError("generation input manifest does not identify the formal budget file")
            if input_manifest.get("base_tests_sha256") != package.get("base_tests_sha256"):
                raise ValueError("generator input base-test hash differs from the sanitized C1 package")
            if baseline_record.get("base_tests_sha256") != package.get("base_tests_sha256"):
                raise ValueError("baseline audit base-test hash differs from the sanitized C1 package")
            if len(response.get("tests", [])) != generation.get("candidate_count"):
                raise ValueError("candidate response and generation-run counts differ")

            if input_manifest.get("study_budget_config") != expected_study_budget:
                raise ValueError("generation input manifest does not identify the current full-study budget")

            review_path = generation_dir / "correctness_review.json"
            review_run_path = generation_dir / "correctness_review_run.json"
            if not review_path.is_file() or not review_run_path.is_file():
                review = subprocess.run(
                    [
                        sys.executable, str(REVIEWER),
                        "--package", str(PACKAGE_ROOT / task / "C1"),
                        "--generation-dir", str(generation_dir),
                        "--model", str(budget["model"]["model_id"]),
                        "--reasoning-effort", str(budget["model"]["reasoning_effort"]),
                        "--timeout-seconds", str(args.review_timeout_seconds),
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                (generation_dir / "correctness-review-cohort.stdout.log").write_text(review.stdout, encoding="utf-8")
                (generation_dir / "correctness-review-cohort.stderr.log").write_text(review.stderr, encoding="utf-8")
                if review.returncode != 0:
                    raise RuntimeError(f"independent correctness review exited {review.returncode}: {review.stderr[-2500:]}")
            review_run = read_json(review_run_path)
            if response["tests"]:
                if (
                    review_run.get("status") != "complete"
                    or review_run.get("model") != budget["model"]["model_id"]
                    or review_run.get("reasoning_effort") != budget["model"]["reasoning_effort"]
                ):
                    raise ValueError("candidate correctness review is incomplete or used the wrong model configuration")
            elif review_run.get("status") != "not_needed_no_candidates":
                raise ValueError("empty candidate response must record a zero-call correctness review")

            frozen_path = generation_dir / "frozen_test_suite.json"
            if frozen_path.is_file():
                frozen = read_json(frozen_path)
                if (
                    frozen.get("method") == "vanilla_llm"
                    and frozen.get("task_id") == task
                    and frozen.get("source_checkpoint") == "C1"
                    and frozen.get("metadata", {}).get("baseline_audit_sha256") == sha256_file(audit_path)
                ):
                    row["status"] = "already_frozen"
                    row["suite_sha256"] = frozen.get("suite_sha256")
                else:
                    raise ValueError("an existing frozen suite does not match the current C1 baseline")
            else:
                handoff_runtime = Path(baseline_record["runtime_directory"])
                runtime_dir = handoff_runtime.resolve() if handoff_runtime.is_dir() else prepare_local_runtime(task)
                command = [
                    sys.executable,
                    str(VALIDATOR),
                    "--generation-dir",
                    str(generation_dir),
                    "--runtime-dir",
                    str(runtime_dir),
                    "--correctness-review",
                    str(review_path),
                    "--repeats",
                    "3",
                    "--timeout-seconds",
                    str(args.timeout_seconds),
                    "--baseline-audit",
                    str(audit_path),
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
                (generation_dir / "cohort-validation.stdout.log").write_text(result.stdout, encoding="utf-8")
                (generation_dir / "cohort-validation.stderr.log").write_text(result.stderr, encoding="utf-8")
                if result.returncode != 0:
                    raise RuntimeError(f"validator exited {result.returncode}: {result.stderr[-2500:]}")
                frozen = read_json(frozen_path)
                row["status"] = "frozen"
                row["suite_sha256"] = frozen.get("suite_sha256")
            row["candidate_count"] = generation.get("candidate_count")
            row["retained_count"] = read_json(generation_dir / "generation_run.json").get("retained_count")
            row["baseline_audit_sha256"] = sha256_file(audit_path)
            row["generation_run_sha256"] = sha256_file(generation_dir / "generation_run.json")
            print(f"[{index}/{len(selected)}] {task}: {row['status']}", flush=True)
        except Exception as error:
            failures += 1
            row["status"] = "error"
            row["error"] = f"{type(error).__name__}: {error}"
            print(f"[{index}/{len(selected)}] {task}: ERROR {row['error']}", file=sys.stderr, flush=True)
        row["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        write_jsonl(log_path, row)

    print(f"Vanilla validation cohort: {len(selected) - failures}/{len(selected)}; errors={failures}")
    print(f"Validation log: {log_path}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

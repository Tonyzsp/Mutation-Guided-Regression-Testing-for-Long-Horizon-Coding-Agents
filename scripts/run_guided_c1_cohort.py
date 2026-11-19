"""Generate, locally review, validate, and freeze Guided C1 suites per task."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESULT_ROOT = ROOT / "artifacts" / "results" / "rq1_full_36x1"
PACKAGE_ROOT = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
UNITS_PATH = ROOT / "config" / "rq1_full_36x1_candidate_units.json"
AUDIT_PATH = RESULT_ROOT / "c1_base_audit_current_sanitized_20261116" / "c1_base_suite_audit.json"
MUTANT_AUDIT_PATH = RESULT_ROOT / "mutmut_cohort_audit_sanitized_linked_20261117.json"
MUTANT_BASELINE_LINKAGE_PATH = RESULT_ROOT / "guided_mutation_baseline_linkage_formal_v1_20261117.json"
CANDIDATE_REVIEW_REPORT_PATH = RESULT_ROOT / "candidate_review_cohort_mutation_guided_formal_v1_baseline_linked_20261117.json"
CANDIDATE_REVIEW_INPUT_ROOT = RESULT_ROOT / "candidate_review_inputs_guided_formal_v1_baseline_linked_20261117"
BUDGET_PATH = ROOT / "config" / "generation_budget_rq1_c1_cohort_formal.json"
STUDY_BUDGET_PATH = ROOT / "config" / "rq1_full_study_budget_v1.json"
STUDY_BUDGET_LOCK_PATH = ROOT / "config" / "rq1_full_study_budget_v1.lock.json"
SURVIVOR_MANIFEST_PATH = RESULT_ROOT / "survivor_only_input_v1" / "manifest.json"
PROMPT_PATH = ROOT / "prompts" / "mutation_guided_gap_cohort_v1.md"
GENERATOR = ROOT / "scripts" / "run_mutation_guided_generation.py"
VALIDATOR = ROOT / "scripts" / "validate_and_freeze_tests.py"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(command: list[str], label: str, timeout: int | None = None) -> tuple[int, str, str]:
    print(f"  {label}", flush=True)
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, check=False,
    )
    return result.returncode, result.stdout, result.stderr


def complete_existing(
    generation_dir: Path,
    budget_id: str,
    mutation_sha: str,
    audit_sha: str,
    prompt_sha: str,
    budget_sha: str,
    study_budget_sha: str,
    triage_sha: str | None,
    brief_sha: str,
    linkage_sha: str,
    mutation_baseline_sha: str,
) -> bool:
    generation_path = generation_dir / "generation_run.json"
    frozen_path = generation_dir / "frozen_test_suite.json"
    manifest_path = generation_dir / "input_manifest.json"
    if not generation_path.is_file() or not frozen_path.is_file() or not manifest_path.is_file():
        return False
    generation = read_json(generation_path)
    frozen = read_json(frozen_path)
    manifest = read_json(manifest_path)
    return (
        generation.get("budget_id") == budget_id
        and generation.get("study_budget_sha256") == study_budget_sha
        and generation.get("mutation_record_set_sha256") == mutation_sha
        and manifest.get("prompt_template", {}).get("sha256") == prompt_sha
        and manifest.get("budget_config", {}).get("sha256") == budget_sha
        and manifest.get("study_budget_config", {}).get("sha256") == study_budget_sha
        and manifest.get("mutant_triage_sha256") == triage_sha
        and manifest.get("opportunity_brief", {}).get("sha256") == brief_sha
        and manifest.get("mutation_baseline_linkage", {}).get("sha256") == linkage_sha
        and manifest.get("mutation_baseline_linkage", {}).get("mutation_baseline_audit_sha256") == mutation_baseline_sha
        and generation.get("mutation_baseline_linkage_sha256") == linkage_sha
        and generation.get("mutation_baseline_audit_sha256") == mutation_baseline_sha
        and frozen.get("method") == "mutation_guided"
        and frozen.get("metadata", {}).get("baseline_audit_sha256") == audit_sha
    )


def successful_generation(
    generation_dir: Path,
    budget_id: str,
    mutation_sha: str,
    prompt_sha: str,
    budget_sha: str,
    study_budget_sha: str,
    triage_sha: str | None,
    brief_sha: str,
    linkage_sha: str,
    mutation_baseline_sha: str,
) -> bool:
    generation_path = generation_dir / "generation_run.json"
    response_path = generation_dir / "candidate_response.json"
    manifest_path = generation_dir / "input_manifest.json"
    if not generation_path.is_file() or not response_path.is_file() or not manifest_path.is_file():
        return False
    generation = read_json(generation_path)
    manifest = read_json(manifest_path)
    response = read_json(response_path)
    return (
        generation.get("codex_exit_code") == 0
        and generation.get("budget_id") == budget_id
        and generation.get("study_budget_sha256") == study_budget_sha
        and generation.get("mutation_record_set_sha256") == mutation_sha
        and manifest.get("mutation_record_set_sha256") == mutation_sha
        and manifest.get("prompt_template", {}).get("sha256") == prompt_sha
        and manifest.get("budget_config", {}).get("sha256") == budget_sha
        and manifest.get("study_budget_config", {}).get("sha256") == study_budget_sha
        and manifest.get("mutant_triage_sha256") == triage_sha
        and manifest.get("opportunity_brief", {}).get("sha256") == brief_sha
        and manifest.get("mutation_baseline_linkage", {}).get("sha256") == linkage_sha
        and manifest.get("mutation_baseline_linkage", {}).get("mutation_baseline_audit_sha256") == mutation_baseline_sha
        and generation.get("mutation_baseline_linkage_sha256") == linkage_sha
        and generation.get("mutation_baseline_audit_sha256") == mutation_baseline_sha
        and isinstance(response.get("tests"), list)
        and generation.get("candidate_count") == len(response["tests"])
    )


def attach_baseline_linkage(
    generation_dir: Path,
    linkage_sha: str,
    linkage_path: Path,
    linkage_record: dict[str, Any],
    current_baseline_sha: str,
    mutant_audit_sha: str,
) -> None:
    manifest_path = generation_dir / "input_manifest.json"
    generation_path = generation_dir / "generation_run.json"
    if not manifest_path.is_file() or not generation_path.is_file():
        raise FileNotFoundError("cannot attach C1 baseline linkage to an incomplete generation")
    manifest = read_json(manifest_path)
    generation = read_json(generation_path)
    entry = {
        "format": "rq1-guided-mutant-baseline-linkage/v1",
        "report_path": str(linkage_path.relative_to(ROOT).as_posix()),
        "sha256": linkage_sha,
        "current_baseline_audit_sha256": current_baseline_sha,
        "mutant_cohort_audit_path": str(MUTANT_AUDIT_PATH.relative_to(ROOT).as_posix()),
        "mutant_cohort_audit_sha256": mutant_audit_sha,
        "mutation_baseline_audit_path": linkage_record["mutation_baseline_audit_path"],
        "mutation_baseline_audit_sha256": linkage_record["mutation_baseline_audit_sha256"],
        "mutation_test_selection_sha256": linkage_record["mutation_test_selection_sha256"],
        "current_stable_pass_test_ids_sha256": linkage_record["current_stable_pass_test_ids_sha256"],
    }
    existing = manifest.get("mutation_baseline_linkage")
    if existing is not None and existing != entry:
        raise ValueError("existing generation input manifest has a different baseline linkage")
    manifest["mutation_baseline_linkage"] = entry
    generation["mutation_baseline_linkage_sha256"] = linkage_sha
    generation["mutation_baseline_audit_sha256"] = linkage_record["mutation_baseline_audit_sha256"]
    write_json(manifest_path, manifest)
    write_json(generation_path, generation)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", action="append", help="Limit to task IDs; may be repeated")
    parser.add_argument("--output-suffix", default="mutation_guided_formal_v1")
    parser.add_argument(
        "--mutant-triage-dir",
        type=Path,
        help="Directory containing one audited <task>/mutant_triage.json per selected task; only capped eligible representatives are sent to the generator.",
    )
    parser.add_argument(
        "--allow-untriaged-survivors",
        action="store_true",
        help="Exploratory override: allow sending all direct survivors when no audited triage directory is available.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--mutmut-timeout-seconds", type=int, default=900)
    parser.add_argument("--cohort-log-name", help="Optional JSONL filename under the result root")
    parser.add_argument(
        "--generate-only",
        action="store_true",
        help="Generate candidates and stop for local specification review before validation.",
    )
    args = parser.parse_args()
    if Path(args.output_suffix).name != args.output_suffix or args.output_suffix in {"", ".", ".."}:
        raise ValueError("output suffix must be a single safe path component")
    if args.mutant_triage_dir is None and not args.allow_untriaged_survivors:
        raise ValueError("RQ1 Guided cohort requires audited mutant triage; use --mutant-triage-dir, or explicitly opt into an exploratory run with --allow-untriaged-survivors")

    units = read_json(UNITS_PATH)["units"]
    baseline_audit = read_json(AUDIT_PATH)
    baseline_by_task = {row["task_id"]: row for row in baseline_audit["records"]}
    budget = read_json(BUDGET_PATH)
    study_budget = read_json(STUDY_BUDGET_PATH)
    lock = read_json(STUDY_BUDGET_LOCK_PATH)
    study_budget_sha = sha256_file(STUDY_BUDGET_PATH)
    if lock.get("budget_path") != STUDY_BUDGET_PATH.relative_to(ROOT).as_posix() or lock.get("sha256") != study_budget_sha:
        raise ValueError("formal-study budget SHA-256 differs from its lock")
    formal_generation = study_budget["generation"]
    if (
        budget["model"]["model_id"] != formal_generation["model"]
        or budget["model"]["reasoning_effort"] != formal_generation["reasoning_effort"]
        or budget["budget"]["max_calls"] != formal_generation["max_calls_per_method_task"]
        or budget["budget"]["max_candidates"] != formal_generation["max_candidates_per_method_task"]
        or budget["budget"]["per_call_output_tokens"] != formal_generation["requested_output_tokens_per_call"]
        or budget["budget"]["max_output_tokens"] != formal_generation["requested_output_tokens_per_method_task"]
        or budget["validation"]["repeat_runs"] != study_budget["source_checkpoint_validation"]["candidate_plus_base_repeat_runs"]
    ):
        raise ValueError("generation budget does not match the locked full-study budget")
    survivor_manifest = read_json(SURVIVOR_MANIFEST_PATH)
    if survivor_manifest.get("task_count") != 36 or survivor_manifest.get("survivor_count") != 8490:
        raise ValueError("current survivor-only input package is incomplete or changed")
    survivor_by_task = survivor_manifest["tasks"]
    audit_sha = sha256_file(AUDIT_PATH)
    mutant_audit = read_json(MUTANT_AUDIT_PATH)
    linkage_report = read_json(MUTANT_BASELINE_LINKAGE_PATH)
    candidate_review_report = read_json(CANDIDATE_REVIEW_REPORT_PATH)
    mutant_audit_sha = sha256_file(MUTANT_AUDIT_PATH)
    linkage_sha = sha256_file(MUTANT_BASELINE_LINKAGE_PATH)
    if mutant_audit.get("complete") is not True or mutant_audit.get("audited_task_count") != 36:
        raise ValueError("direct-mutant audit does not certify the full 36-task cohort")
    if linkage_report.get("complete") is not True or linkage_report.get("task_count") != 36:
        raise ValueError("mutation-to-current-baseline linkage is incomplete")
    if (
        linkage_report.get("current_baseline_audit_sha256") != audit_sha
        or linkage_report.get("mutant_cohort_audit_sha256") != mutant_audit_sha
        or linkage_report.get("survivor_manifest_sha256") != sha256_file(SURVIVOR_MANIFEST_PATH)
    ):
        raise ValueError("mutation baseline linkage report no longer matches its frozen inputs")
    if (
        candidate_review_report.get("generation_suffix") != args.output_suffix
        or candidate_review_report.get("task_count") != 36
        or candidate_review_report.get("candidate_count") != 32
        or candidate_review_report.get("actual_review_calls", 73) > 72
    ):
        raise ValueError("method-blind candidate review report is incomplete or uses a different Guided generation cohort")
    linkage_by_task = linkage_report["tasks"]
    candidate_review_by_task = {row["task_id"]: row for row in candidate_review_report.get("records", [])}
    if len(candidate_review_by_task) != 36:
        raise ValueError("candidate review report does not contain all 36 C1 task records")
    budget_id = budget["budget_id"]
    prompt_sha = sha256_file(PROMPT_PATH)
    budget_sha = sha256_file(BUDGET_PATH)
    triage_root = args.mutant_triage_dir.resolve(strict=True) if args.mutant_triage_dir is not None else None
    if triage_root is not None and not triage_root.is_relative_to(ROOT):
        raise ValueError(f"mutant triage directory must remain under {ROOT}")
    log_name = args.cohort_log_name or f"guided_c1_cohort_{args.output_suffix}.jsonl"
    if Path(log_name).name != log_name or log_name in {"", ".", ".."}:
        raise ValueError("cohort log name must be a filename")
    log_path = RESULT_ROOT / log_name
    generation_log_root = RESULT_ROOT / "guided_logs_linked_v1"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    failures = 0

    selected = [unit for unit in units if not args.task or unit["task_id"] in args.task]
    unknown_tasks = sorted(set(args.task or []) - {unit["task_id"] for unit in units})
    if unknown_tasks:
        raise ValueError(f"unknown task ID(s): {unknown_tasks}")
    if not selected:
        raise ValueError("no C1 units selected")
    triage_by_task = {
        unit["task_id"]: triage_root / unit["task_id"] / "mutant_triage.json"
        for unit in selected
    } if triage_root is not None else {}
    missing_triage = [str(path) for path in triage_by_task.values() if not path.is_file()]
    if missing_triage:
        raise FileNotFoundError("mutant triage files are missing before generation starts: " + ", ".join(missing_triage))

    for index, unit in enumerate(selected, 1):
        task = unit["task_id"]
        print(f"[{index}/{len(selected)}] {task}", flush=True)
        mutation_bundle = ROOT / survivor_by_task[task]["mutation_bundle"]
        if not mutation_bundle.resolve(strict=True).is_relative_to(RESULT_ROOT.resolve(strict=True)):
            raise ValueError(f"{task}: mutation bundle path escapes the formal results directory")
        generation_dir = RESULT_ROOT / task / "C1" / args.output_suffix
        brief = RESULT_ROOT / task / "C1" / "formal_v1_inputs_20261117" / "gap_opportunities.md"
        triage_path = triage_by_task.get(task)
        triage_sha = sha256_file(triage_path) if triage_path is not None else None
        row: dict[str, Any] = {
            "task_id": task,
            "checkpoint_id": "C1",
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "generation_directory": str(generation_dir.relative_to(ROOT)),
            "study_budget_sha256": study_budget_sha,
        }
        launch_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        generation_log_dir = generation_log_root / task
        generation_log_dir.mkdir(parents=True, exist_ok=True)
        generation_stdout_path = generation_log_dir / f"{launch_stamp}.stdout.log"
        generation_stderr_path = generation_log_dir / f"{launch_stamp}.stderr.log"
        row["generation_stdout_log"] = str(generation_stdout_path.relative_to(ROOT))
        row["generation_stderr_log"] = str(generation_stderr_path.relative_to(ROOT))
        try:
            summary_path = mutation_bundle / "mutation_summary.json"
            records_path = mutation_bundle / "mutant_records.jsonl"
            if not summary_path.is_file() or not records_path.is_file():
                raise FileNotFoundError(f"direct-classified mutant bundle is missing: {mutation_bundle}")
            summary = read_json(summary_path)
            baseline = baseline_by_task[task]
            lineage = linkage_by_task.get(task)
            mutant_audit_record = next(
                (item for item in mutant_audit.get("records", []) if item.get("task_id") == task),
                None,
            )
            if (
                lineage is None
                or lineage.get("status") != "linked"
                or mutant_audit_record is None
                or mutant_audit_record.get("status") != "complete"
                or summary.get("complete") is not True
                or summary.get("task_id") != task
                or summary.get("checkpoint_id") != "C1"
                or summary.get("base_tests_sha256") != baseline.get("base_tests_sha256")
                or summary.get("baseline_audit_sha256") != lineage.get("mutation_baseline_audit_sha256")
                or sha256_file(summary_path) != lineage.get("mutation_summary_sha256")
                or sha256_file(records_path) != lineage.get("mutation_records_sha256")
                or summary.get("source_sha256") != lineage.get("source_sha256")
                or sorted(summary.get("mutation_test_selection") or []) != sorted(baseline.get("stable_pass_test_ids") or [])
            ):
                raise ValueError("direct mutant bundle lacks a valid link to the final repeated C1 baseline")
            mutation_sha = sha256_file(records_path)
            row.update({
                "mutant_count": summary.get("mutant_count"),
                "status_counts": summary.get("status_counts"),
                "mutation_score": summary.get("mutation_score"),
                "mutation_record_set_sha256": mutation_sha,
                "baseline_audit_sha256": audit_sha,
                "mutation_baseline_audit_sha256": lineage["mutation_baseline_audit_sha256"],
                "mutation_baseline_linkage_sha256": linkage_sha,
                "mutmut_cohort_audit_sha256": mutant_audit_sha,
            })
            brief_sha = sha256_file(brief)
            triage_data = read_json(triage_path) if triage_path is not None else None
            eligible_hint_count = triage_data.get("selected_for_generation_count", 0) if triage_data else 0
            row["eligible_mutant_hint_count"] = eligible_hint_count
            if eligible_hint_count == 0:
                row["skip_reason"] = "No C1 survivor passed semantic triage as an externally testable, specification-backed behavior."
                print("  no eligible hints; recording a zero-call empty Guided result and freezing the shared base suite", flush=True)
            if complete_existing(
                generation_dir, budget_id, mutation_sha, audit_sha, prompt_sha, budget_sha,
                study_budget_sha, triage_sha, brief_sha, linkage_sha,
                lineage["mutation_baseline_audit_sha256"],
            ):
                row["status"] = "already_frozen"
            else:
                if (generation_dir / "frozen_test_suite.json").is_file():
                    raise ValueError("existing frozen suite does not match current budget/input hashes")
                if not successful_generation(
                    generation_dir, budget_id, mutation_sha, prompt_sha, budget_sha,
                    study_budget_sha, triage_sha, brief_sha, linkage_sha,
                    lineage["mutation_baseline_audit_sha256"],
                ):
                    command = [
                        sys.executable, str(GENERATOR),
                        "--package", str(PACKAGE_ROOT / task / "C1"),
                        "--mutant-records", str(records_path),
                        "--output-dir", str(generation_dir),
                        "--budget-config", str(BUDGET_PATH),
                        "--study-budget-config", str(STUDY_BUDGET_PATH),
                        "--prompt-template", str(PROMPT_PATH),
                        "--opportunity-brief", str(brief),
                        "--timeout-seconds", str(args.timeout_seconds),
                    ]
                    if triage_path is not None:
                        command.extend(["--mutant-triage", str(triage_path)])
                        row["mutant_triage_path"] = str(triage_path.relative_to(ROOT))
                        row["mutant_triage_sha256"] = triage_sha
                    elif args.allow_untriaged_survivors:
                        command.append("--allow-untriaged-survivors")
                    code, stdout, stderr = run(command, "generate Guided candidates", timeout=args.timeout_seconds + 120)
                    generation_stdout_path.write_text(stdout, encoding="utf-8")
                    generation_stderr_path.write_text(stderr, encoding="utf-8")
                    if code != 0:
                        attempts_path = generation_dir / "generation_attempts.jsonl"
                        attempts = [json.loads(line) for line in attempts_path.read_text(encoding="utf-8").splitlines() if line.strip()] if attempts_path.is_file() else []
                        launched = max(1, len(attempts))
                        while code != 0 and launched < int(budget["budget"]["max_calls"]) and attempts and not (generation_dir / "candidate_response.json").is_file():
                            latest = attempts[-1] if attempts else {}
                            response_name = latest.get("response_path")
                            response_file = generation_dir / response_name if isinstance(response_name, str) else None
                            structured_response_exists = (
                                response_file is not None
                                and response_file.is_file()
                                and response_file.stat().st_size > 0
                            )
                            if structured_response_exists or latest.get("status") == "completed":
                                break
                            prior_attempt_count = len(attempts)
                            launched += 1
                            code, stdout, stderr = run(command, "retry a failed pre-response generation call", timeout=args.timeout_seconds + 120)
                            with generation_stdout_path.open("a", encoding="utf-8") as handle:
                                handle.write("\n--- retry ---\n" + stdout)
                            with generation_stderr_path.open("a", encoding="utf-8") as handle:
                                handle.write("\n--- retry ---\n" + stderr)
                            attempts = [json.loads(line) for line in attempts_path.read_text(encoding="utf-8").splitlines() if line.strip()] if attempts_path.is_file() else []
                            if len(attempts) <= prior_attempt_count:
                                break
                        if code != 0:
                            raise RuntimeError(f"Guided generation failed (exit {code}): {stderr[-2000:]}")

                attach_baseline_linkage(
                    generation_dir,
                    linkage_sha,
                    MUTANT_BASELINE_LINKAGE_PATH,
                    lineage,
                    audit_sha,
                    mutant_audit_sha,
                )
                generation = read_json(generation_dir / "generation_run.json")
                if not successful_generation(
                    generation_dir,
                    budget_id,
                    mutation_sha,
                    prompt_sha,
                    budget_sha,
                    study_budget_sha,
                    triage_sha,
                    brief_sha,
                    linkage_sha,
                    lineage["mutation_baseline_audit_sha256"],
                ):
                    raise ValueError("Guided candidate output is incomplete or does not match the current survivor bundle")
                if args.generate_only:
                    row["status"] = "generation_complete_review_pending"
                else:
                    review_path = generation_dir / "correctness_review.json"
                    review_run_path = generation_dir / "correctness_review_run.json"
                    review_record = candidate_review_by_task.get(task)
                    candidate_count = int(generation.get("candidate_count", -1))
                    if review_record is None:
                        raise FileNotFoundError("method-blind candidate review cohort record is missing")
                    if candidate_count:
                        if not review_path.is_file() or not review_run_path.is_file():
                            raise FileNotFoundError("method-blind candidate review output is missing")
                        review = read_json(review_path)
                        review_run = read_json(review_run_path)
                        expected_review_status = "complete"
                        if (
                            review_run.get("status") != expected_review_status
                            or review_record.get("status") != expected_review_status
                            or review_run.get("candidate_count") != candidate_count
                            or review_record.get("candidate_count") != candidate_count
                            or review_run.get("study_budget_sha256") != study_budget_sha
                            or review_run.get("candidate_response_sha256") != sha256_file(generation_dir / "candidate_response.json")
                            or review_run.get("review_sha256") != sha256_file(review_path)
                            or review_record.get("review_run_sha256") != sha256_file(review_run_path)
                            or review_run.get("base_tests_sha256") != read_json(PACKAGE_ROOT / task / "C1" / "package.json").get("base_tests_sha256")
                            or review_run.get("excluded_model_inputs", []) != read_json(generation_dir / "input_manifest.json").get("excluded_model_inputs", [])
                        ):
                            raise ValueError("method-blind correctness review does not match the Guided C1 candidates or package")
                        prepared_manifest_path = CANDIDATE_REVIEW_INPUT_ROOT / task / "input_manifest.json"
                        prepared_prompt_path = CANDIDATE_REVIEW_INPUT_ROOT / task / "review_prompt.md"
                        if not prepared_manifest_path.is_file() or not prepared_prompt_path.is_file():
                            raise FileNotFoundError("prepared method-blind review input is missing")
                        prepared_manifest = read_json(prepared_manifest_path)
                        prompt_logical_sha = hashlib.sha256(prepared_prompt_path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
                        if (
                            prepared_manifest.get("method_label_in_prompt") is not False
                            or prepared_manifest.get("llm_calls_made") != 0
                            or prepared_manifest.get("prompt_sha256") != prompt_logical_sha
                            or prepared_manifest.get("source_candidate_response_sha256") != review_run.get("candidate_response_sha256")
                            or prepared_manifest.get("prompt_sha256") != review_run.get("prompt_sha256")
                        ):
                            raise ValueError("method-blind review prompt provenance does not match the model review")
                        if review_run.get("model") != study_budget["candidate_review"]["reviewer_model"] or review_run.get("reasoning_effort") != study_budget["model"]["reasoning_effort"]:
                            raise ValueError("candidate review used a different model or reasoning budget")
                    else:
                        expected_review_status = "not_needed_no_generated_tests" if eligible_hint_count == 0 else "not_needed_no_candidates"
                        if (
                            review_record.get("status") != expected_review_status
                            or review_record.get("candidate_count") != 0
                            or review_record.get("call_count") != 0
                        ):
                            raise ValueError("empty Guided result does not match the no-call correctness-review record")
                        if review_path.is_file():
                            review = read_json(review_path)
                        else:
                            review = {"reviews": []}
                            write_json(review_path, review)
                        if review.get("reviews") != []:
                            raise ValueError("empty Guided result has non-empty candidate review data")
                    if len(review.get("reviews", [])) != candidate_count:
                        raise ValueError("candidate correctness review does not include every Guided candidate")
                    runtime_dir = Path(baseline["runtime_directory"]).resolve(strict=True)
                    validate_command = [
                        sys.executable, str(VALIDATOR),
                        "--generation-dir", str(generation_dir),
                        "--runtime-dir", str(runtime_dir),
                        "--correctness-review", str(review_path),
                        "--repeats", str(budget["validation"]["repeat_runs"]),
                        "--timeout-seconds", str(args.timeout_seconds),
                        "--mutmut-timeout-seconds", str(args.mutmut_timeout_seconds),
                        "--baseline-audit", str(AUDIT_PATH),
                    ]
                    code, stdout, stderr = run(validate_command, "validate candidates and freeze C1 suite", timeout=None)
                    (generation_dir / "cohort-validation.stdout.log").write_text(stdout, encoding="utf-8")
                    (generation_dir / "cohort-validation.stderr.log").write_text(stderr, encoding="utf-8")
                    if code != 0:
                        raise RuntimeError(f"candidate validation failed (exit {code}): {stderr[-2000:]}")
                    row["status"] = "frozen"

            if (generation_dir / "generation_run.json").is_file():
                generation = read_json(generation_dir / "generation_run.json")
                row["candidate_count"] = generation.get("candidate_count")
                row["retained_count"] = generation.get("retained_count")
                row["generation_usage"] = generation.get("actual_usage")
            if (generation_dir / "frozen_test_suite.json").is_file():
                frozen = read_json(generation_dir / "frozen_test_suite.json")
                row["suite_sha256"] = frozen.get("suite_sha256")
                row["generated_test_paths"] = frozen.get("generated_test_paths")
        except Exception as error:
            failures += 1
            row["status"] = "error"
            row["error"] = f"{type(error).__name__}: {error}"
            print(f"  ERROR: {row['error']}", file=sys.stderr, flush=True)
        row["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"  {row['status']}", flush=True)

    print(f"Guided C1 cohort: {len(selected) - failures}/{len(selected)} frozen or reused; {failures} errors")
    print(f"Cohort log: {log_path}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

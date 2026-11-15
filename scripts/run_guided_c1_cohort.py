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
AUDIT_PATH = RESULT_ROOT / "c1_base_audit_isolation_revalidated_20261019" / "c1_base_suite_audit.json"
BUDGET_PATH = ROOT / "config" / "generation_budget_rq1_c1_cohort_formal.json"
PROMPT_PATH = ROOT / "prompts" / "mutation_guided_gap_cohort_v1.md"
GENERATOR = ROOT / "scripts" / "run_mutation_guided_generation.py"
VALIDATOR = ROOT / "scripts" / "validate_and_freeze_tests.py"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


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
    triage_sha: str | None,
    brief_sha: str,
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
        and generation.get("mutation_record_set_sha256") == mutation_sha
        and manifest.get("prompt_template", {}).get("sha256") == prompt_sha
        and manifest.get("budget_config", {}).get("sha256") == budget_sha
        and manifest.get("mutant_triage_sha256") == triage_sha
        and manifest.get("opportunity_brief", {}).get("sha256") == brief_sha
        and frozen.get("method") == "mutation_guided"
        and frozen.get("metadata", {}).get("baseline_audit_sha256") == audit_sha
    )


def successful_generation(
    generation_dir: Path,
    budget_id: str,
    mutation_sha: str,
    prompt_sha: str,
    budget_sha: str,
    triage_sha: str | None,
    brief_sha: str,
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
        and generation.get("mutation_record_set_sha256") == mutation_sha
        and manifest.get("mutation_record_set_sha256") == mutation_sha
        and manifest.get("prompt_template", {}).get("sha256") == prompt_sha
        and manifest.get("budget_config", {}).get("sha256") == budget_sha
        and manifest.get("mutant_triage_sha256") == triage_sha
        and manifest.get("opportunity_brief", {}).get("sha256") == brief_sha
        and isinstance(response.get("tests"), list)
        and generation.get("candidate_count") == len(response["tests"])
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", action="append", help="Limit to task IDs; may be repeated")
    parser.add_argument("--output-suffix", default="guided_mutant_v3_20261114")
    parser.add_argument("--mutant-bundle-suffix", default="mutmut_direct_v3_20261114")
    parser.add_argument(
        "--mutant-triage-dir",
        type=Path,
        help="Directory containing one audited <task>.json triage file per selected task; only eligible survivors are sent to the generator.",
    )
    parser.add_argument(
        "--allow-untriaged-survivors",
        action="store_true",
        help="Exploratory override: allow sending all direct survivors when no audited triage directory is available.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--mutmut-timeout-seconds", type=int, default=900)
    parser.add_argument(
        "--generate-only",
        action="store_true",
        help="Generate candidates and stop for local specification review before validation.",
    )
    args = parser.parse_args()
    if args.mutant_triage_dir is None and not args.allow_untriaged_survivors:
        raise ValueError("RQ1 Guided cohort requires audited mutant triage; use --mutant-triage-dir, or explicitly opt into an exploratory run with --allow-untriaged-survivors")

    units = read_json(UNITS_PATH)["units"]
    baseline_audit = read_json(AUDIT_PATH)
    baseline_by_task = {row["task_id"]: row for row in baseline_audit["records"]}
    budget = read_json(BUDGET_PATH)
    audit_sha = sha256_file(AUDIT_PATH)
    budget_id = budget["budget_id"]
    prompt_sha = sha256_file(PROMPT_PATH)
    budget_sha = sha256_file(BUDGET_PATH)
    triage_root = args.mutant_triage_dir.resolve(strict=True) if args.mutant_triage_dir is not None else None
    if triage_root is not None and not triage_root.is_relative_to(ROOT):
        raise ValueError(f"mutant triage directory must remain under {ROOT}")
    log_path = RESULT_ROOT / "guided_c1_cohort_mutant_v3_20261114.jsonl"
    generation_log_root = RESULT_ROOT / "guided_cohort_logs_mutant_v3_20261114"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    failures = 0

    selected = [unit for unit in units if not args.task or unit["task_id"] in args.task]
    unknown_tasks = sorted(set(args.task or []) - {unit["task_id"] for unit in units})
    if unknown_tasks:
        raise ValueError(f"unknown task ID(s): {unknown_tasks}")
    if not selected:
        raise ValueError("no C1 units selected")
    triage_by_task = {
        unit["task_id"]: triage_root / f"{unit['task_id']}.json"
        for unit in selected
    } if triage_root is not None else {}
    missing_triage = [str(path) for path in triage_by_task.values() if not path.is_file()]
    if missing_triage:
        raise FileNotFoundError("mutant triage files are missing before generation starts: " + ", ".join(missing_triage))

    for index, unit in enumerate(selected, 1):
        task = unit["task_id"]
        print(f"[{index}/{len(selected)}] {task}", flush=True)
        mutation_bundle = RESULT_ROOT / task / "C1" / args.mutant_bundle_suffix
        generation_dir = RESULT_ROOT / task / "C1" / args.output_suffix
        brief = RESULT_ROOT / task / "C1" / "gap_opportunities_isolation_revalidated_v3.md"
        triage_path = triage_by_task.get(task)
        triage_sha = sha256_file(triage_path) if triage_path is not None else None
        row: dict[str, Any] = {
            "task_id": task,
            "checkpoint_id": "C1",
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "generation_directory": str(generation_dir.relative_to(ROOT)),
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
            if (
                summary.get("complete") is not True
                or summary.get("task_id") != task
                or summary.get("checkpoint_id") != "C1"
                or summary.get("base_tests_sha256") != baseline.get("base_tests_sha256")
                or summary.get("baseline_audit_sha256") != audit_sha
                or summary.get("mutation_test_selection") != baseline.get("stable_pass_test_ids")
            ):
                raise ValueError("direct mutant bundle does not match the final repeated C1 baseline")
            mutation_sha = sha256_file(records_path)
            row.update({
                "mutant_count": summary.get("mutant_count"),
                "status_counts": summary.get("status_counts"),
                "mutation_score": summary.get("mutation_score"),
                "mutation_record_set_sha256": mutation_sha,
                "baseline_audit_sha256": audit_sha,
            })
            brief_sha = sha256_file(brief)
            if complete_existing(generation_dir, budget_id, mutation_sha, audit_sha, prompt_sha, budget_sha, triage_sha, brief_sha):
                row["status"] = "already_frozen"
            else:
                if (generation_dir / "frozen_test_suite.json").is_file():
                    raise ValueError("existing frozen suite does not match current budget/input hashes")
                if not successful_generation(generation_dir, budget_id, mutation_sha, prompt_sha, budget_sha, triage_sha, brief_sha):
                    command = [
                        sys.executable, str(GENERATOR),
                        "--package", str(PACKAGE_ROOT / task / "C1"),
                        "--mutant-records", str(records_path),
                        "--output-dir", str(generation_dir),
                        "--budget-config", str(BUDGET_PATH),
                        "--prompt-template", str(PROMPT_PATH),
                        "--opportunity-brief", str(brief),
                        "--timeout-seconds", str(args.timeout_seconds),
                    ]
                    if triage_path is not None:
                        command.extend(["--mutant-triage", str(triage_path)])
                        row["mutant_triage_path"] = str(triage_path.relative_to(ROOT))
                        row["mutant_triage_sha256"] = triage_sha
                        row["mutant_triage_path"] = str(triage_path.relative_to(ROOT))
                        row["mutant_triage_sha256"] = sha256_file(triage_path)
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

                generation = read_json(generation_dir / "generation_run.json")
                if not successful_generation(
                    generation_dir,
                    budget_id,
                    mutation_sha,
                    prompt_sha,
                    budget_sha,
                    triage_sha,
                    brief_sha,
                ):
                    raise ValueError("Guided candidate output is incomplete or does not match the current survivor bundle")
                if args.generate_only:
                    row["status"] = "generation_complete_review_pending"
                else:
                    review_path = generation_dir / "correctness_review.local.json"
                    if generation.get("candidate_count") == 0 and not review_path.is_file():
                        review_path.write_text(json.dumps({"reviews": []}, indent=2) + "\n", encoding="utf-8")
                    if not review_path.is_file():
                        raise FileNotFoundError(
                            "local specification review is missing; external candidate review is not run by this cohort script"
                        )
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

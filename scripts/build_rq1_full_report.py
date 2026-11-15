"""Build the reproducible RQ1 report from frozen suites, evaluation, and adjudication."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESULT_ROOT = ROOT / "artifacts" / "results" / "rq1_full_36x1"
PACKAGE_ROOT = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
DEFAULT_UNITS = ROOT / "config" / "rq1_full_36x1_candidate_units.json"
DEFAULT_AUDIT = RESULT_ROOT / "c1_base_audit_isolation_revalidated_20261019" / "c1_base_suite_audit.json"
VANILLA_AUDIT_PATH = RESULT_ROOT / "c1_vanilla_cohort_audit_20261019.json"
INPUT_ISOLATION_AUDIT_PATH = RESULT_ROOT / "c1_input_isolation_audit_20261019.json"
RELEASE_RESTORE_PATH = ROOT / "data" / "checkpoints" / "BASE_TESTS_RELEASE_RESTORE_20261019.json"
GUIDED_SUFFIX = "guided_iso_v3_20261019"
MUTANT_SUFFIX = "mutmut_direct_revalidated_20261019"
METHODS = {
    "original": "Original Tests",
    "vanilla_llm": "Vanilla LLM Tests",
    "mutation_guided": "Mutation-Guided Tests",
}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    output.extend("| " + " | ".join(str(value).replace("|", "\\|").replace("\n", " ") for value in row) + " |" for row in rows)
    return "\n".join(output)


def normalize_issue_key(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return normalized or fallback


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation-dir", required=True, type=Path)
    parser.add_argument("--adjudication-dir", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "RQ1_full_36x1_report.md")
    args = parser.parse_args()

    evaluation_dir = args.evaluation_dir.resolve(strict=True)
    if not evaluation_dir.is_relative_to(RESULT_ROOT.resolve(strict=True)):
        raise ValueError("evaluation directory must remain beneath the RQ1 result root")
    adjudication_dir = args.adjudication_dir.resolve(strict=True) if args.adjudication_dir else evaluation_dir / "blinded_adjudication"
    if not adjudication_dir.is_relative_to(evaluation_dir):
        raise ValueError("adjudication directory must remain inside the evaluation directory")

    units = read_json(DEFAULT_UNITS)["units"]
    baseline = read_json(DEFAULT_AUDIT)
    report_issues: list[str] = []
    vanilla_audit = read_json(VANILLA_AUDIT_PATH)
    vanilla_suffix_by_task = {
        row["task_id"]: row["cohort_directory"]
        for row in vanilla_audit.get("records", [])
        if row.get("status") == "complete"
    }
    input_isolation_audit = read_json(INPUT_ISOLATION_AUDIT_PATH)
    release_restore = read_json(RELEASE_RESTORE_PATH)
    if vanilla_audit.get("status") != "complete" or len(vanilla_suffix_by_task) != len(units):
        report_issues.append("the audited Vanilla suite selection is incomplete")
    if release_restore.get("status") != "overlaid_original_release_base_tests":
        report_issues.append("the original Release base-test restoration is not verified")
    baseline_sha = sha256_file(DEFAULT_AUDIT)
    baseline_counts = Counter(row.get("status") for row in baseline.get("records", []))
    baseline_by_task = {row["task_id"]: row for row in baseline.get("records", [])}
    budget_ids: Counter[str] = Counter()
    generation_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    generation_status_counts: Counter[str] = Counter()
    total_mutants = total_killed = total_survived = 0
    other_mutant_status_counts: Counter[str] = Counter()
    mutant_bundle_complete = 0
    mutation_scores: list[float] = []
    task_rows: list[list[Any]] = []
    suite_complete = Counter()
    total_input_tokens = Counter()
    total_output_tokens = Counter()
    usage_complete = Counter()
    guided_no_eligible_mutant_tasks = 0
    guided_zero_call_tasks = 0
    for unit in units:
        task = unit["task_id"]
        package = read_json(PACKAGE_ROOT / task / "C1" / "package.json")
        base_ok = package.get("base_tests_sha256") == baseline_by_task.get(task, {}).get("base_tests_sha256")
        methods_cell = []
        task_generation: dict[str, dict[str, Any]] = {}
        for method, suffix in (("vanilla_llm", vanilla_suffix_by_task.get(task, "")), ("mutation_guided", GUIDED_SUFFIX)):
            generation_dir = RESULT_ROOT / task / "C1" / suffix
            frozen_path = generation_dir / "frozen_test_suite.json"
            run_path = generation_dir / "generation_run.json"
            if not frozen_path.is_file() or not run_path.is_file():
                suite_complete[method] += 0
                methods_cell.append(f"{METHODS[method]}: missing")
                report_issues.append(f"{task}: missing frozen {method} suite or generation record")
                continue
            frozen = read_json(frozen_path)
            run = read_json(run_path)
            input_manifest_path = generation_dir / "input_manifest.json"
            if not input_manifest_path.is_file():
                report_issues.append(f"{task}: missing {method} generation input manifest")
            else:
                input_manifest = read_json(input_manifest_path)
                if input_manifest.get("base_tests_sha256") != package.get("base_tests_sha256"):
                    report_issues.append(f"{task}: {method} generation used a different C1 base-test hash")
            if frozen.get("task_id") != task or frozen.get("method") != method or frozen.get("source_checkpoint") != "C1":
                report_issues.append(f"{task}: frozen {method} suite identity mismatch")
            if frozen.get("budget_id") != run.get("budget_id"):
                report_issues.append(f"{task}: {method} frozen suite and generation budget IDs differ")
            suite_complete[method] += 1
            budget_ids[run.get("budget_id", "missing")] += 1
            actual = run.get("actual_usage") or {}
            if method == "mutation_guided" and int(run.get("eligible_mutant_count", 0) or 0) == 0:
                guided_no_eligible_mutant_tasks += 1
                if int(actual.get("calls", 0) or 0) == 0:
                    guided_zero_call_tasks += 1
            if int(actual.get("calls", 0) or 0) > 0 and actual.get("token_usage_available") and isinstance(actual.get("input_tokens"), int) and isinstance(actual.get("output_tokens"), int):
                total_input_tokens[method] += actual["input_tokens"]
                total_output_tokens[method] += actual["output_tokens"]
                usage_complete[method] += 1
            retained = int(run.get("retained_count", frozen.get("metadata", {}).get("candidate_files_retained", 0)) or 0)
            emitted = int(run.get("candidate_count", frozen.get("metadata", {}).get("candidate_files_emitted", 0)) or 0)
            generation_status_counts[f"{method}:emitted"] += emitted
            generation_status_counts[f"{method}:retained"] += retained
            generation_status_counts[f"{method}:empty_tasks"] += int(emitted == 0)
            task_generation[method] = {
                "emitted": emitted,
                "retained": retained,
                "suite_hash": frozen.get("suite_sha256"),
                "budget_id": run.get("budget_id"),
            }
            methods_cell.append(f"{METHODS[method]} {retained}/{emitted}")
        if "vanilla_llm" in task_generation and "mutation_guided" in task_generation:
            if task_generation["vanilla_llm"]["budget_id"] != task_generation["mutation_guided"]["budget_id"]:
                report_issues.append(f"{task}: Vanilla and Guided generation budget IDs differ")
        mutation_path = RESULT_ROOT / task / "C1" / MUTANT_SUFFIX / "mutation_summary.json"
        mutation_cell = "missing"
        if mutation_path.is_file():
            mutation = read_json(mutation_path)
            counts = mutation.get("status_counts", {})
            if mutation.get("complete") and mutation.get("baseline_audit_sha256") == baseline_sha:
                mutant_bundle_complete += 1
            else:
                report_issues.append(f"{task}: direct mutant summary incomplete or not matched to the baseline audit")
            killed = int(counts.get("killed", 0))
            survived = int(counts.get("survived", 0))
            mutant_count = int(mutation.get("mutant_count", 0))
            status_total = sum(int(value) for value in counts.values())
            if status_total != mutant_count:
                report_issues.append(f"{task}: mutant status counts ({status_total}) do not match mutant_count ({mutant_count})")
            total_mutants += mutant_count
            total_killed += killed
            total_survived += survived
            for status, count in counts.items():
                if status not in {"killed", "survived"}:
                    other_mutant_status_counts[status] += int(count)
            if isinstance(mutation.get("mutation_score"), (int, float)):
                mutation_scores.append(float(mutation["mutation_score"]))
            mutation_cell = f"{mutation.get('mutant_count', 0)} ({killed} killed, {survived} survived)"
        else:
            report_issues.append(f"{task}: missing direct mutant bundle")
        if not base_ok:
            report_issues.append(f"{task}: C1 base test hash differs from the repeated baseline audit")
        task_rows.append([
            task,
            len(unit.get("target_checkpoints", [])),
            baseline_by_task.get(task, {}).get("status", "missing"),
            "yes" if base_ok else "NO",
            mutation_cell,
            "; ".join(methods_cell),
        ])

    evaluation_summary = read_json(evaluation_dir / "evaluation_summary.json")
    observations = read_jsonl(evaluation_dir / "candidate_failure_observations.jsonl")
    stable_obs = [row for row in observations if row.get("reproducibility", {}).get("status") == "stable_failure"]
    nonstable_obs = len(observations) - len(stable_obs)
    candidate_observations_by_method = Counter()
    stable_observations_by_method = Counter()
    for observation in observations:
        for method in observation.get("methods_detected", []):
            if method in METHODS:
                candidate_observations_by_method[method] += 1
                if observation.get("reproducibility", {}).get("status") == "stable_failure":
                    stable_observations_by_method[method] += 1

    adjudication_summary_path = adjudication_dir / "llm_judge_summary.json"
    adjudication_reviews_path = adjudication_dir / "llm_judge_reviews.json"
    adjudication_ready = adjudication_summary_path.is_file() and adjudication_reviews_path.is_file()
    reviews_by_case: dict[str, dict[str, Any]] = {}
    mapping_by_case: dict[str, dict[str, Any]] = {}
    if adjudication_dir.joinpath("private_case_mapping.json").is_file():
        mapping_by_case = {row["case_id"]: row for row in read_json(adjudication_dir / "private_case_mapping.json")}
    if adjudication_ready:
        reviews_by_case = {row["case_id"]: row for row in read_json(adjudication_reviews_path).get("reviews", [])}
        if set(reviews_by_case) != set(mapping_by_case):
            report_issues.append("LLM judge review IDs do not exactly match the blinded case mapping")
    case_rows: list[dict[str, Any]] = []
    inconclusive_cases: list[str] = []
    verdict_counts: Counter[str] = Counter()
    if adjudication_ready:
        for case_id, judge in reviews_by_case.items():
            mapping = mapping_by_case.get(case_id)
            if not mapping:
                report_issues.append(f"{case_id}: judge review has no private observation mapping")
                continue
            final = dict(judge)
            review_method = "llm_only_judge"
            raw_verdict = str(judge.get("verdict", "inconclusive"))
            if judge.get("human_review_recommended") or float(judge.get("confidence", 0)) < 0.80 or raw_verdict == "inconclusive":
                final["llm_verdict"] = raw_verdict
                final["verdict"] = "inconclusive"
                inconclusive_cases.append(case_id)
            verdict_counts[final.get("verdict", "missing")] += 1
            case_rows.append({"case_id": case_id, **mapping, **final, "review_method": review_method})

    checkpoint_order = {
        unit["task_id"]: {checkpoint: index for index, checkpoint in enumerate(unit.get("target_checkpoints", []))}
        for unit in units
    }
    by_issue: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in case_rows:
        if row.get("verdict") != "confirmed_regression":
            continue
        case_id = row["case_id"]
        issue_key = normalize_issue_key(str(row.get("issue_key", "")), case_id)
        by_issue[(row["task_id"], issue_key)][row["target_checkpoint"]].append(row)

    event_episodes: list[dict[str, Any]] = []
    for (task, issue_key), checkpoints in sorted(by_issue.items()):
        ordered = sorted(checkpoints, key=lambda cp: checkpoint_order.get(task, {}).get(cp, 10**6))
        current: dict[str, Any] | None = None
        previous_index: int | None = None
        for checkpoint in ordered:
            index = checkpoint_order.get(task, {}).get(checkpoint, 10**6)
            if current is None or (previous_index is not None and index > previous_index + 1):
                if current is not None:
                    event_episodes.append(current)
                current = {
                    "task_id": task,
                    "issue_key": issue_key,
                    "first_target_checkpoint": checkpoint,
                    "observed_checkpoints": [],
                    "methods": set(),
                    "case_ids": [],
                    "summaries": [],
                }
            current["observed_checkpoints"].append(checkpoint)
            for row in checkpoints[checkpoint]:
                current["methods"].update(row.get("methods_detected", []))
                current["case_ids"].append(row["case_id"])
                current["summaries"].append(row.get("issue_summary", ""))
            previous_index = index
        if current is not None:
            event_episodes.append(current)

    method_events = Counter()
    unique_method_events = Counter()
    for event in event_episodes:
        event["methods"] = sorted(event["methods"])
        for method in event["methods"]:
            if method in METHODS:
                method_events[method] += 1
        if len(event["methods"]) == 1 and event["methods"][0] in METHODS:
            unique_method_events[event["methods"][0]] += 1

    output_path = args.output.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    complete_eval = evaluation_summary.get("status") == "complete" and evaluation_summary.get("task_count") == len(units)
    complete_suites = (
        suite_complete["vanilla_llm"] == len(units)
        and suite_complete["mutation_guided"] == len(units)
        and vanilla_audit.get("status") == "complete"
        and len(vanilla_suffix_by_task) == len(units)
    )
    complete_mutants = mutant_bundle_complete == len(units)
    judge_complete = adjudication_ready and read_json(adjudication_summary_path).get("status") == "complete"
    final_ready = complete_eval and complete_suites and complete_mutants and judge_complete and not report_issues
    weighted_score = total_killed / (total_killed + total_survived) if total_killed + total_survived else None
    macro_score = sum(mutation_scores) / len(mutation_scores) if mutation_scores else None

    report = [
        "# RQ1 Full Experiment Report — 36×1 Cohort",
        "",
        f"**Generated:** {datetime.now(timezone.utc).isoformat()}  ",
        f"**Status:** {'Complete' if final_ready else 'Awaiting required experiment artifacts'}  ",
        f"**Evaluation bundle:** `{evaluation_dir.relative_to(ROOT)}`  ",
        f"**RQ1:** Do Mutation-Guided Tests detect more later-checkpoint regressions than Original Tests or Vanilla LLM Tests under the same generation budget?",
        "",
        "## Executive result",
        "",
    ]
    if final_ready:
        union_count = len(event_episodes)
        counts = [method_events.get(name, 0) for name in METHODS]
        winner = "Mutation-Guided Tests detected more adjudicated regression episodes than both baselines." if counts[2] > max(counts[0], counts[1]) else (
            "Mutation-Guided Tests did not detect more adjudicated regression episodes than both baselines."
        )
        report.extend([
            winner,
            "",
            f"Across the observed union of confirmed regression episodes ({union_count}), Original Tests detected {counts[0]}, Vanilla LLM Tests detected {counts[1]}, and Mutation-Guided Tests detected {counts[2]}. These are deduplicated event counts; each event may be detected by more than one method.",
        ])
    else:
        report.extend([
            "No final RQ1 claim is reported yet. The remaining readiness, adjudication, or human-review items are listed below.",
        ])
    report.extend([
        "",
        "## Experimental design",
        "",
        f"The cohort contains {len(units)} task trajectories, each with C1 as the frozen source checkpoint and {sum(len(unit.get('target_checkpoints', [])) for unit in units)} later checkpoint snapshots in the supplied evaluation manifest. Original, Vanilla, and Mutation-Guided suites are frozen at C1 and then executed unchanged on later snapshots.",
        "",
        "Vanilla and Mutation-Guided use the same GPT-6 Luna medium configuration and the same per-task limits: at most 3 calls, 8 candidate files, and 12,288 generated output tokens. Each retained candidate must pass repeated C1 validation; Guided candidates must also kill at least one named C1 survivor when run alone. Original Tests use the complete legal C1 base suite. Hidden, future, and excluded tests are not supplied to generators.",
        "",
        "## Data isolation and Release integrity",
        "",
        f"The Release archive restoration covered {release_restore.get('package_count', 0)} checkpoint packages and {release_restore.get('restored_file_count', 0)} base-test files. The corrected sanitizer and package audit pass before the revalidated cohort is used.",
        "",
        f"The historical input-provenance audit compared {input_isolation_audit.get('manifest_count', 0)} C1 generation manifests; {input_isolation_audit.get('status_counts', {}).get('invalidated_input_mismatch', 0)} do not match the restored, sanitized packages and are excluded from this report. Within the 36-task formal Vanilla cohort, {len({row['task_id'] for row in input_isolation_audit.get('records', []) if row.get('method') == 'vanilla_llm' and row.get('run_directory') == 'vanilla_cohort_gpt6luna_medium_20261018' and row.get('status') == 'invalidated_input_mismatch'})} historical suites were regenerated under the same budget.",
        "",
        f"The Release metadata hash differs from the archived file contents for {release_restore.get('release_hash_metadata_mismatch_count', 0)} `file_merger` checkpoint packages ({', '.join(release_restore.get('release_hash_metadata_mismatch_packages', [])) or 'none'}). Sanitized hashes are recomputed from the archived test files.",
        "",
        "The packaged C1 snapshots and per-checkpoint `agent_run_manifest.json` files are treated as frozen trajectory evidence. Raw conversation transcripts are absent. `file_backup/errors` remains excluded according to the Release isolation manifest.",
        "",
        "## C1 baseline and mutation analysis",
        "",
        f"The repeated C1 base-suite audit reports {baseline_counts.get('stable_pass', 0)} tasks with a fully passing stable suite and {baseline_counts.get('stable_with_failures', 0)} tasks with stable pre-existing failures. Regression candidates are restricted to tests that passed consistently at C1; known C1 failures are not counted as later regressions.",
        "",
        f"Direct classification covers {mutant_bundle_complete}/{len(units)} task bundles with {total_mutants} mutants total: {total_killed} killed, {total_survived} surviving, and {sum(other_mutant_status_counts.values())} with other statuses ({dict(other_mutant_status_counts)}). The pooled direct mutation score is {weighted_score:.3f} and the task-macro average is {macro_score:.3f}; these scores use only decisive killed/survived outcomes. Classification applies each mutant and runs stable C1 passing tests because raw mutmut statuses can misclassify CLI tests that invoke subprocesses.",
        "",
        "## Generation and frozen suites",
        "",
        f"Vanilla retained {generation_status_counts.get('vanilla_llm:retained', 0)} of {generation_status_counts.get('vanilla_llm:emitted', 0)} emitted candidate files across {suite_complete.get('vanilla_llm', 0)}/{len(units)} frozen suites; {generation_status_counts.get('vanilla_llm:empty_tasks', 0)} tasks returned no candidates. Mutation-Guided retained {generation_status_counts.get('mutation_guided:retained', 0)} of {generation_status_counts.get('mutation_guided:emitted', 0)} across {suite_complete.get('mutation_guided', 0)}/{len(units)} frozen suites; {generation_status_counts.get('mutation_guided:empty_tasks', 0)} tasks returned no candidates.",
        "",
        f"Of the Guided task packages, {guided_no_eligible_mutant_tasks} had no eligible surviving mutants; {guided_zero_call_tasks} therefore used a documented zero-call empty result and froze only the shared base suite.",
        "",
        f"Recorded Vanilla usage: {total_input_tokens.get('vanilla_llm', 0)} input and {total_output_tokens.get('vanilla_llm', 0)} output tokens over {usage_complete.get('vanilla_llm', 0)} tasks with service usage. Recorded Mutation-Guided usage: {total_input_tokens.get('mutation_guided', 0)} input and {total_output_tokens.get('mutation_guided', 0)} output tokens over {usage_complete.get('mutation_guided', 0)} tasks with service usage. Call counts and candidate limits are the binding budget dimensions.",
        "",
        "## Later-checkpoint execution and adjudication",
        "",
        f"Evaluation status: `{evaluation_summary.get('status')}`; {evaluation_summary.get('unique_suite_runs', 0)} unique suite runs across {evaluation_summary.get('target_run_count', 0)} task/checkpoint targets; {evaluation_summary.get('candidate_failure_observation_count', 0)} initial candidate failure observations, of which {len(stable_obs)} reproduced in all three targeted repeats and {nonstable_obs} did not.",
        "",
        md_table(
            ["Method", "Initial failure observations", "Stable three-run failures"],
            [[METHODS[name], candidate_observations_by_method.get(name, 0), stable_observations_by_method.get(name, 0)] for name in METHODS],
        ),
        "",
        f"Adjudication status: `{read_json(adjudication_summary_path).get('status') if adjudication_ready else 'not started'}`; {len(reviews_by_case)} cases reviewed; verdicts: {dict(verdict_counts)}. Confirmed regression episodes after deduplication: {len(event_episodes)}.",
        "",
        md_table(
            ["Method", "Confirmed episodes detected", "Detected only by this method"],
            [[METHODS[name], method_events.get(name, 0), unique_method_events.get(name, 0)] for name in METHODS],
        ),
        "",
        "### Confirmed event episodes",
        "",
        md_table(
            ["Task", "First failing checkpoint", "Issue key", "Detected by", "Case IDs"],
            [[
                event["task_id"],
                event["first_target_checkpoint"],
                event["issue_key"],
                ", ".join(METHODS.get(method, method) for method in event["methods"]),
                ", ".join(event["case_ids"]),
            ] for event in event_episodes],
        ) if event_episodes else "No confirmed event episodes are available.",
        "",
        "The denominator is the union of confirmed episodes found by the three observed suites. It is not an oracle for regressions missed by every method, so the report does not interpret these counts as absolute recall over all possible regressions.",
        "",
        "## Task-level audit table",
        "",
        md_table(["Task", "Later checkpoints", "C1 baseline", "Base hash match", "Mutants", "Generated suites retained/emitted"], task_rows),
        "",
        "## Reproducibility and artifacts",
        "",
        f"Baseline audit SHA-256: `{baseline_sha}`  ",
        f"Evaluation runs: `{evaluation_dir / 'evaluation_runs.jsonl'}`  ",
        f"Candidate observations: `{evaluation_dir / 'candidate_failure_observations.jsonl'}`  ",
        f"Blinded judge inputs and outputs: `{adjudication_dir}`  ",
        f"The evaluator stores source/target specifications, checkpoint manifests, C1-to-target workspace diffs, target logs, per-failure three-run reproduction evidence, and package hashes alongside the candidate observations.",
        "",
        "## Items needing attention",
        "",
    ])
    if inconclusive_cases:
        report.append("LLM-only adjudication marked these blinded cases inconclusive under the frozen confidence rule; they are excluded from confirmed-regression counts: " + ", ".join(f"`{case}`" for case in inconclusive_cases) + ".")
    if report_issues:
        report.extend(f"- {item}" for item in report_issues)
    if not report_issues and final_ready:
        report.append("No incomplete cohort or adjudication items were found by the report builder.")
    if not report_issues and not final_ready:
        report.append("Waiting for remaining experiment artifacts: complete all 36 mutant bundles, freeze all Guided suites, run later-checkpoint evaluation, and adjudicate stable failures.")
    report.extend([
        "",
        "## Interpretation limits",
        "",
        "- The Release supplies one frozen trajectory per task. Results do not estimate variability across independent agent trajectories.",
        "- Stable C1 base-suite failures reduce the number of eligible tests on those tasks; they are recorded and excluded from later-regression counts.",
        "- Surviving mutants may include equivalent or behaviorally unreachable mutations unless a separate review identifies them; the direct classifier establishes test behavior against a mutant, not semantic non-equivalence.",
        "- The measured event set depends on failures observed by at least one tested suite and on blinded adjudication; defects missed by all suites are not observable here.",
        "- Regression labels are LLM-adjudicated. Cases below the 0.80 confidence threshold or flagged for human review are recorded as inconclusive and excluded; no human adjudication is performed.",
        "",
    ])
    output_path.write_text("\n".join(report), encoding="utf-8")
    print(f"Report written: {output_path}")
    print(f"Final-ready: {final_ready}; issues={len(report_issues)}; inconclusive_cases={len(inconclusive_cases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

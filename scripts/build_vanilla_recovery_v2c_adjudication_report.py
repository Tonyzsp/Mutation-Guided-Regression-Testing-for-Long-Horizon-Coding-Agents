"""Build an auditable Vanilla recovery-v2c adjudication and episode ledger.

This is deliberately a single-method result: it must not be interpreted as an
RQ1 comparison against Original or Mutation-Guided suites.
"""

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
RESULTS_ROOT = ROOT / "artifacts" / "results"
DEFAULT_EVALUATION = RESULTS_ROOT / "rq1_vanilla_recovery_v2c_later_eval"
DEFAULT_VANILLA_ROOT = RESULTS_ROOT / "rq1_vanilla_recovery_v2c_merged"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_issue_key(value: str, fallback: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or fallback


def checkpoint_sort_key(value: str) -> tuple[int, str]:
    match = re.fullmatch(r"C(\d+)", value)
    return (int(match.group(1)), value) if match else (10**9, value)


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(item).replace("|", "\\|").replace("\n", " ") for item in row) + " |")
    return "\n".join(lines)


def effective_verdict(review: dict[str, Any]) -> tuple[str, str]:
    """Apply the protocol's countability rule to one blinded LLM review."""
    raw = str(review.get("verdict", "inconclusive"))
    if raw == "inconclusive":
        return "inconclusive", "judge_inconclusive"
    if bool(review.get("human_review_recommended")):
        return "inconclusive", "human_review_recommended"
    if float(review.get("confidence", 0) or 0) < 0.80:
        return "inconclusive", "below_confidence_threshold"
    return raw, "countable"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation-dir", type=Path, default=DEFAULT_EVALUATION)
    parser.add_argument("--adjudication-dir", type=Path)
    parser.add_argument("--vanilla-root", type=Path, default=DEFAULT_VANILLA_ROOT)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    evaluation_dir = args.evaluation_dir.resolve(strict=True)
    if not evaluation_dir.is_relative_to(RESULTS_ROOT.resolve(strict=True)):
        raise ValueError("evaluation directory must be under artifacts/results")
    adjudication_dir = (args.adjudication_dir or evaluation_dir / "blinded_adjudication").resolve(strict=True)
    if not adjudication_dir.is_relative_to(evaluation_dir):
        raise ValueError("adjudication directory must be inside the evaluation directory")
    vanilla_root = args.vanilla_root.resolve(strict=True)
    if not vanilla_root.is_relative_to(RESULTS_ROOT.resolve(strict=True)):
        raise ValueError("Vanilla root must be under artifacts/results")
    output_dir = (args.output_dir or adjudication_dir).resolve()
    if not output_dir.is_relative_to(evaluation_dir):
        raise ValueError("output directory must be inside the evaluation directory")

    config = read_json(evaluation_dir / "evaluation_config.json")
    summary = read_json(evaluation_dir / "evaluation_summary.json")
    task_ids = list(config.get("task_ids", []))
    if not task_ids or len(set(task_ids)) != len(task_ids):
        raise ValueError("evaluation config has no unique task inventory")
    if config.get("methods") != ["vanilla_llm"]:
        raise ValueError("this report only supports a Vanilla-only evaluation")
    if summary.get("status") != "complete":
        raise ValueError(f"evaluation is not complete: {summary.get('status')}")

    judge_summary = read_json(adjudication_dir / "llm_judge_summary.json")
    reviews = read_json(adjudication_dir / "llm_judge_reviews.json").get("reviews", [])
    mapping = read_json(adjudication_dir / "private_case_mapping.json")
    observations = read_jsonl(evaluation_dir / "candidate_failure_observations.jsonl")
    runs = read_jsonl(evaluation_dir / "evaluation_runs.jsonl")
    if judge_summary.get("status") != "complete":
        raise ValueError(f"judge is not complete: {judge_summary.get('status')}")
    if judge_summary.get("call_failure_count") != 0 or judge_summary.get("error_count") != 0:
        raise ValueError("judge contains failed calls or malformed responses")
    if not (len(reviews) == len(mapping) == judge_summary.get("review_count") == judge_summary.get("case_count")):
        raise ValueError("judge summary, reviews, and private mapping counts disagree")

    reviews_by_case = {row["case_id"]: row for row in reviews}
    mapping_by_case = {row["case_id"]: row for row in mapping}
    observations_by_id = {row["observation_id"]: row for row in observations}
    if len(reviews_by_case) != len(reviews) or len(mapping_by_case) != len(mapping):
        raise ValueError("duplicate case IDs in reviews or mapping")
    if set(reviews_by_case) != set(mapping_by_case):
        raise ValueError("review IDs do not exactly match private mapping IDs")
    if len(observations_by_id) != len(observations):
        raise ValueError("duplicate observation IDs")

    # The run file, rather than a global cohort manifest, specifies which
    # checkpoints were actually evaluated. A missing evaluated checkpoint splits
    # a persistent failure into a separate event episode.
    checkpoint_order: dict[str, dict[str, int]] = {}
    evaluated_checkpoint_counts: dict[str, int] = {}
    for task in task_ids:
        checkpoints = sorted(
            {str(row["target_checkpoint"]) for row in runs if row.get("task_id") == task and row.get("status") == "complete"},
            key=checkpoint_sort_key,
        )
        if not checkpoints:
            raise ValueError(f"{task}: no complete evaluation runs")
        checkpoint_order[task] = {checkpoint: index for index, checkpoint in enumerate(checkpoints)}
        evaluated_checkpoint_counts[task] = len(checkpoints)

    # The evaluator overlays generated tests beneath base_tests, so classify by
    # frozen manifest basename rather than the path in a pytest node ID.
    generated_basenames: dict[str, set[str]] = {}
    for task in task_ids:
        frozen = read_json(vanilla_root / task / "C1" / "vanilla_llm" / "frozen_test_suite.json")
        if frozen.get("task_id") != task or frozen.get("method") != "vanilla_llm":
            raise ValueError(f"{task}: Vanilla frozen-suite identity mismatch")
        generated_basenames[task] = {Path(str(path)).name for path in frozen.get("generated_test_paths", [])}

    case_rows: list[dict[str, Any]] = []
    raw_counts: Counter[str] = Counter()
    effective_counts: Counter[str] = Counter()
    countability_counts: Counter[str] = Counter()
    origin_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for case_id in sorted(reviews_by_case):
        review = reviews_by_case[case_id]
        private = mapping_by_case[case_id]
        case_path = adjudication_dir / "cases" / f"{case_id}.json"
        if sha256_file(case_path) != private.get("case_sha256"):
            raise ValueError(f"{case_id}: blinded case-file hash mismatch")
        observation = observations_by_id.get(private.get("observation_id"))
        if observation is None:
            raise ValueError(f"{case_id}: mapping refers to an unknown observation")
        if any(observation.get(key) != private.get(key) for key in ("task_id", "target_checkpoint", "test_nodeid")):
            raise ValueError(f"{case_id}: mapping disagrees with evaluation observation")
        if observation.get("reproducibility", {}).get("status") != "stable_failure":
            raise ValueError(f"{case_id}: adjudicated observation is not a stable assertion failure")
        if private.get("methods_detected") != ["vanilla_llm"]:
            raise ValueError(f"{case_id}: adjudicated observation is not Vanilla-only")

        task = str(private["task_id"])
        test_file = Path(str(private["test_nodeid"]).split("::", 1)[0].replace("\\", "/")).name
        origin = "vanilla_generated_test" if test_file in generated_basenames[task] else "base_test"
        effective, countability_reason = effective_verdict(review)
        raw = str(review.get("verdict", "inconclusive"))
        raw_counts[raw] += 1
        effective_counts[effective] += 1
        countability_counts[countability_reason] += 1
        origin_counts[origin][effective] += 1
        case_rows.append({
            **private,
            "test_origin": origin,
            "judge_verdict": raw,
            "effective_verdict": effective,
            "countability_reason": countability_reason,
            "confidence": float(review.get("confidence", 0) or 0),
            "human_review_recommended": bool(review.get("human_review_recommended")),
            "issue_key": str(review.get("issue_key", "")),
            "issue_summary": str(review.get("issue_summary", "")),
            "rationale": str(review.get("rationale", "")),
            "evidence_refs": review.get("evidence_refs", []),
        })

    stable_failure_count = sum(row.get("reproducibility", {}).get("status") == "stable_failure" for row in observations)
    stable_error_count = sum(row.get("reproducibility", {}).get("status") == "stable_error" for row in observations)
    if stable_failure_count != len(case_rows):
        raise ValueError("the judge batch does not exactly cover all stable assertion failures")

    # Frozen operational event rule: same task + normalized judge issue key,
    # merge observations at the same checkpoint, and join adjacent evaluated
    # checkpoints. This intentionally does not make undocumented semantic merges
    # across different LLM issue keys.
    groups: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in case_rows:
        if row["effective_verdict"] != "confirmed_regression":
            continue
        issue_key = normalize_issue_key(row["issue_key"], row["case_id"])
        row["normalized_issue_key"] = issue_key
        groups[(row["task_id"], issue_key)][row["target_checkpoint"]].append(row)

    events: list[dict[str, Any]] = []
    for (task, issue_key), by_checkpoint in sorted(groups.items()):
        current: dict[str, Any] | None = None
        previous_index: int | None = None
        for checkpoint in sorted(by_checkpoint, key=lambda value: checkpoint_order[task].get(value, 10**9)):
            index = checkpoint_order[task].get(checkpoint)
            if index is None:
                raise ValueError(f"{task}/{checkpoint}: no matching completed evaluation run")
            if current is None or (previous_index is not None and index > previous_index + 1):
                if current is not None:
                    events.append(current)
                current = {
                    "task_id": task,
                    "source_checkpoint": "C1",
                    "issue_key": issue_key,
                    "first_target_checkpoint": checkpoint,
                    "observed_checkpoints": [],
                    "case_ids": [],
                    "observation_ids": [],
                    "test_nodeids": [],
                    "test_origins": [],
                    "issue_summaries": [],
                    "judge_issue_keys": [],
                }
            current["observed_checkpoints"].append(checkpoint)
            for row in by_checkpoint[checkpoint]:
                for field, value in (
                    ("case_ids", row["case_id"]),
                    ("observation_ids", row["observation_id"]),
                    ("test_nodeids", row["test_nodeid"]),
                    ("test_origins", row["test_origin"]),
                    ("issue_summaries", row["issue_summary"]),
                    ("judge_issue_keys", row["issue_key"]),
                ):
                    current[field].append(value)
            previous_index = index
        if current is not None:
            events.append(current)

    for index, event in enumerate(events, start=1):
        event["event_id"] = f"VANILLA-V2C-E{index:04d}"
        for field in ("case_ids", "observation_ids", "test_nodeids", "test_origins", "issue_summaries", "judge_issue_keys"):
            event[field] = sorted(set(value for value in event[field] if value))
        event["case_count"] = len(event["case_ids"])
        event["detected_by_added_vanilla_test"] = "vanilla_generated_test" in event["test_origins"]
        event["also_hit_by_base_test"] = "base_test" in event["test_origins"]

    confirmed = [row for row in case_rows if row["effective_verdict"] == "confirmed_regression"]
    unresolved = [row for row in case_rows if row["effective_verdict"] == "inconclusive"]
    generated_confirmed = [row for row in confirmed if row["test_origin"] == "vanilla_generated_test"]
    events_with_generated = [row for row in events if row["detected_by_added_vanilla_test"]]
    events_only_generated = [row for row in events if row["detected_by_added_vanilla_test"] and not row["also_hit_by_base_test"]]
    events_by_task: Counter[str] = Counter(row["task_id"] for row in events)
    events_by_checkpoint: Counter[str] = Counter(row["first_target_checkpoint"] for row in events)

    output_files = {
        "cases": output_dir / "vanilla_v2c_adjudicated_cases.json",
        "events": output_dir / "vanilla_v2c_confirmed_events.json",
        "inconclusive": output_dir / "vanilla_v2c_inconclusive_cases.json",
        "audit": output_dir / "vanilla_v2c_adjudication_result_audit.json",
        "report": output_dir / "vanilla_v2c_adjudication_report.md",
    }
    generated_at = datetime.now(timezone.utc).isoformat()
    event_rule = (
        "Group countable confirmed observations by task and normalized judge issue_key; merge observations at the same "
        "checkpoint; join consecutive completed-evaluation checkpoints; split across an evaluated gap. No cross-issue-key "
        "semantic merge was applied."
    )
    audit = {
        "format": "rq1-vanilla-recovery-v2c-adjudication-result-audit/v1",
        "status": "complete_llm_only_with_unresolved_cases",
        "scope": "Vanilla-only 29-task recovery cohort; no cross-method RQ1 comparison",
        "evaluation_dir": str(evaluation_dir.relative_to(ROOT)),
        "adjudication_dir": str(adjudication_dir.relative_to(ROOT)),
        "vanilla_suite_root": str(vanilla_root.relative_to(ROOT)),
        "task_count": len(task_ids),
        "target_pair_count": int(summary.get("target_run_count", 0)),
        "unique_suite_run_count": int(summary.get("unique_suite_runs", 0)),
        "candidate_observation_count": len(observations),
        "stable_assertion_failure_count": stable_failure_count,
        "stable_error_count_excluded_from_this_adjudication_batch": stable_error_count,
        "judge_model": judge_summary.get("model"),
        "reasoning_effort": judge_summary.get("reasoning_effort"),
        "valid_review_count": len(reviews),
        "call_failure_count": judge_summary.get("call_failure_count"),
        "review_and_mapping_ids_match": True,
        "all_case_hashes_match_private_mapping": True,
        "all_mapping_observations_are_stable_vanilla_failures": True,
        "raw_verdict_counts": dict(sorted(raw_counts.items())),
        "effective_verdict_counts": dict(sorted(effective_counts.items())),
        "countability_reason_counts": dict(sorted(countability_counts.items())),
        "confirmed_regression_observations": len(confirmed),
        "confirmed_regression_event_episodes": len(events),
        "confirmed_cases_from_added_vanilla_tests": len(generated_confirmed),
        "confirmed_event_episodes_hit_by_added_vanilla_tests": len(events_with_generated),
        "confirmed_event_episodes_hit_only_by_added_vanilla_tests_in_this_run": len(events_only_generated),
        "unresolved_case_count": len(unresolved),
        "event_deduplication_rule": event_rule,
        "interpretation_limitations": [
            "This is a Vanilla-only recovery cohort, not a matched three-method RQ1 result.",
            "Raw confirmed labels are excluded when the judge requests human review or confidence is below 0.80.",
            "Issue-key grouping is a reproducible operational rule; different LLM issue keys are not manually merged.",
            "Stable target-code errors were retained as diagnostics but were not in this prepared assertion-failure batch.",
        ],
        "output_files": {key: path.name for key, path in output_files.items()},
        "generated_at_utc": generated_at,
    }
    write_json(output_files["cases"], {
        "format": "rq1-vanilla-recovery-v2c-adjudicated-cases/v1",
        "case_count": len(case_rows),
        "effective_verdict_rule": "Inconclusive if raw verdict is inconclusive, human review is requested, or confidence is below 0.80.",
        "cases": case_rows,
    })
    write_json(output_files["events"], {
        "format": "rq1-vanilla-recovery-v2c-confirmed-regression-events/v1",
        "deduplication_rule": event_rule,
        "event_count": len(events),
        "events": events,
    })
    write_json(output_files["inconclusive"], {
        "format": "rq1-vanilla-recovery-v2c-inconclusive-cases/v1",
        "case_count": len(unresolved),
        "cases": unresolved,
    })
    write_json(output_files["audit"], audit)

    task_rows = [
        [task, evaluated_checkpoint_counts[task], sum(row["task_id"] == task and row["effective_verdict"] == "confirmed_regression" for row in case_rows), events_by_task.get(task, 0)]
        for task in task_ids
    ]
    origin_rows = [[origin, counts.get("confirmed_regression", 0), counts.get("inconclusive", 0)] for origin, counts in sorted(origin_counts.items())]
    report = [
        "# Vanilla LLM Recovery v2c: Later-Checkpoint Adjudication",
        "",
        f"**Generated:** {generated_at}  ",
        "**Scope:** Vanilla-only 29-task recovery cohort; not a three-method RQ1 comparison.",
        "",
        "## Completed execution",
        "",
        f"The frozen Vanilla suites ran on {summary.get('target_run_count')} later task/checkpoint pairs with {summary.get('unique_suite_runs')} unique suite runs and no infrastructure errors. The evaluator recorded {len(observations)} candidate observations: {stable_failure_count} stable assertion failures and {stable_error_count} stable errors. The blinded batch covers all stable assertion failures; stable errors remain diagnostic and are outside this batch.",
        "",
        f"The fixed judge `{judge_summary.get('model')}` / `{judge_summary.get('reasoning_effort')}` returned {len(reviews)}/{len(reviews)} valid reviews with {judge_summary.get('call_failure_count')} call failures.",
        "",
        "## Countable adjudication result",
        "",
        "The frozen rule treats a case as inconclusive when the judge returns `inconclusive`, requests human review, or assigns confidence below 0.80.",
        "",
        md_table(["Effective verdict", "Cases"], [[key, value] for key, value in sorted(effective_counts.items())]),
        "",
        f"- Raw judge labels included {raw_counts.get('confirmed_regression', 0)} `confirmed_regression` cases. {raw_counts.get('confirmed_regression', 0) - effective_counts.get('confirmed_regression', 0)} requested human review, leaving **{len(confirmed)}** countable confirmed observations.",
        f"- Those observations mechanically deduplicate to **{len(events)} provisional episode(s)** under the recorded rule.",
        f"- **{len(unresolved)}** cases are unresolved and excluded from confirmed-event counts.",
        "",
        "## Test-origin audit",
        "",
        md_table(["Frozen-test origin", "Confirmed observations", "Unresolved observations"], origin_rows),
        "",
        f"Added Vanilla tests account for {len(generated_confirmed)} confirmed observations and touch {len(events_with_generated)} provisional episodes; {len(events_only_generated)} episodes have no base-test observation in this one suite run. This is internal attribution, not evidence of superiority over another method.",
        "",
        "## Per-task episode audit",
        "",
        md_table(["Task", "Later checkpoints", "Confirmed observations", "Provisional episodes"], task_rows),
        "",
        "## Episode onset",
        "",
        md_table(["First failing checkpoint", "Episodes"], [[checkpoint, events_by_checkpoint[checkpoint]] for checkpoint in sorted(events_by_checkpoint, key=checkpoint_sort_key)]) if events_by_checkpoint else "No countable confirmed episodes.",
        "",
        "## Deduplication and limits",
        "",
        event_rule,
        "",
        "The ledger does not manually merge different LLM issue keys, even where they may describe related behavior. It is therefore a reproducible operational/provisional episode count, not a cross-method ranking or a final semantic-reconciliation claim.",
        "",
        "## Machine-readable artifacts",
        "",
        *[f"- `{path.name}`" for path in output_files.values()],
        "",
    ]
    output_files["report"].write_text("\n".join(report), encoding="utf-8")
    print(json.dumps({
        "status": audit["status"],
        "reviews": len(reviews),
        "effective_verdict_counts": dict(sorted(effective_counts.items())),
        "confirmed_observations": len(confirmed),
        "deduplicated_event_episodes": len(events),
        "unresolved_cases": len(unresolved),
        "outputs": {key: str(path.relative_to(ROOT)) for key, path in output_files.items()},
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

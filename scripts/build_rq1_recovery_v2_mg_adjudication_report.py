#!/usr/bin/env python3
"""Summarize blinded MG-only v2 regression adjudication and deduplicate events."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVALUATION = ROOT / "artifacts" / "results" / "rq1_recovery_v2" / "eval_mg_composite_v2"
DEFAULT_ADJUDICATION = DEFAULT_EVALUATION / "guided_judge"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_issue_key(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return normalized or fallback


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation-dir", type=Path, default=DEFAULT_EVALUATION)
    parser.add_argument("--adjudication-dir", type=Path, default=DEFAULT_ADJUDICATION)
    args = parser.parse_args()

    evaluation_dir = args.evaluation_dir.resolve(strict=True)
    adjudication_dir = args.adjudication_dir.resolve(strict=True)
    if not adjudication_dir.is_relative_to(evaluation_dir):
        raise ValueError("adjudication directory must remain inside the v2 evaluation directory")

    summary = read_json(adjudication_dir / "llm_judge_summary.json")
    payload = read_json(adjudication_dir / "llm_judge_reviews.json")
    mapping = read_json(adjudication_dir / "private_case_mapping.json")
    source = read_json(adjudication_dir / "summary.json")
    if summary.get("status") != "complete" or summary.get("call_failure_count") != 0 or summary.get("error_count") != 0:
        raise ValueError("judge batch is incomplete or contains failed/malformed calls")
    reviews = payload.get("reviews", [])
    if len(reviews) != source.get("case_count") or len(mapping) != source.get("case_count"):
        raise ValueError("judge reviews and private case mapping do not match the prepared six-case batch")
    reviews_by_id = {row["case_id"]: row for row in reviews}
    mapping_by_id = {row["case_id"]: row for row in mapping}
    if len(reviews_by_id) != len(reviews) or set(reviews_by_id) != set(mapping_by_id):
        raise ValueError("duplicate or unmatched case IDs")

    call_manifests = [
        read_json(path)
        for path in sorted((adjudication_dir / "reviews").glob("case-*/run_manifest.json"))
    ]
    input_tokens = sum((manifest.get("token_usage") or {}).get("input_tokens", 0) for manifest in call_manifests)
    output_tokens = sum((manifest.get("token_usage") or {}).get("output_tokens", 0) for manifest in call_manifests)

    effective: list[dict[str, Any]] = []
    raw_counts: Counter[str] = Counter()
    judge_effective_counts: Counter[str] = Counter()
    for case_id in sorted(reviews_by_id):
        review = reviews_by_id[case_id]
        private = mapping_by_id[case_id]
        case_path = adjudication_dir / "cases" / f"{case_id}.json"
        import hashlib
        if hashlib.sha256(case_path.read_bytes()).hexdigest() != private["case_sha256"]:
            raise ValueError(f"blinded case hash mismatch: {case_id}")
        verdict = str(review.get("verdict", "inconclusive"))
        confidence = float(review.get("confidence", 0) or 0)
        flagged = bool(review.get("human_review_recommended"))
        effective_verdict = verdict
        if verdict == "inconclusive" or flagged or confidence < 0.80:
            effective_verdict = "inconclusive"
        raw_counts[verdict] += 1
        row = {
            **private,
            "raw_verdict": verdict,
            "judge_effective_verdict": effective_verdict,
            "effective_verdict": effective_verdict,
            "confidence": confidence,
            "human_review_recommended": flagged,
            "issue_key": str(review.get("issue_key", "")),
            "issue_summary": str(review.get("issue_summary", "")),
            "rationale": str(review.get("rationale", "")),
            "evidence_refs": review.get("evidence_refs", []),
            "deduplication_notes": [],
        }
        effective.append(row)

    # Cross-case coherence audit: three file_backup/C3 tests all omit the
    # newly-required destination and fail before exercising their assertions.
    # The judge returned two expected_spec_change decisions and one confirmed
    # decision for this same behavior. Preserve every raw verdict, but mark the
    # conflicting confirmed outlier inconclusive at the event-counting layer.
    file_backup_candidates: list[dict[str, Any]] = []
    for row in effective:
        if row["task_id"] != "file_backup" or row["target_checkpoint"] != "C3":
            continue
        case = read_json(adjudication_dir / "cases" / f"{row['case_id']}.json")
        target_spec = case.get("target_specification", "").lower()
        diff = case.get("workspace_diff", "").lower()
        source = case.get("frozen_test_source", "").lower()
        call_crash = case.get("initial_target_result", {}).get("phases", {}).get("call", {}).get("crash_message", "").lower()
        if "destination" in row["issue_key"].lower() and "destination" not in source and "destination" in target_spec and "destination" in diff and "exit status 2" in call_crash:
            file_backup_candidates.append(row)
    conflict_clusters: list[dict[str, Any]] = []
    if len(file_backup_candidates) > 1 and len({row["judge_effective_verdict"] for row in file_backup_candidates}) > 1:
        for row in file_backup_candidates:
            row["consistency_conflict"] = True
            row["deduplication_notes"].append(
                "Same file_backup/C3 missing-destination behavior received conflicting judge verdicts; not counted as a confirmed event."
            )
            if row["judge_effective_verdict"] == "confirmed_regression":
                row["effective_verdict"] = "inconclusive"
        conflict_clusters.append({
            "task_id": "file_backup",
            "target_checkpoint": "C3",
            "behavior_key": "schedule_missing_newly_required_destination",
            "case_ids": sorted(row["case_id"] for row in file_backup_candidates),
            "judge_verdicts": {row["case_id"]: row["raw_verdict"] for row in file_backup_candidates},
            "conflict_rule": "Two cases judged the C3 destination requirement as expected_spec_change; one case judged the same missing-destination failure as confirmed_regression. The contradictory cluster is excluded from confirmed event counts.",
        })

    for row in effective:
        judge_effective_counts[row["judge_effective_verdict"]] += 1

    # The two test_translator/C4 cases differ in test setup and literal value,
    # but have the same target failure: `test` exits 2 with an empty error
    # result for a Python `!=` assertion after the same parser rewrite. Merge
    # them as two test detections of one task behavior, retaining both cases.
    translator_cases: list[dict[str, Any]] = []
    for row in effective:
        if row["task_id"] != "test_translator" or row["target_checkpoint"] != "C4":
            continue
        case = read_json(adjudication_dir / "cases" / f"{row['case_id']}.json")
        call_crash = case.get("initial_target_result", {}).get("phases", {}).get("call", {}).get("crash_message", "")
        source = case.get("frozen_test_source", "")
        if "!=" in source and '"status":"error","passed":[],"failed":[]' in call_crash.replace("\\\"", "\""):
            translator_cases.append(row)
    deduplicated_case_groups: list[dict[str, Any]] = []
    if len(translator_cases) > 1:
        for row in translator_cases:
            row["event_issue_key_override"] = "python_inequality_assertion_error"
            row["deduplication_notes"].append(
                "Merged with other test_translator/C4 `!=` candidate(s): same parser change and same error status with empty passed/failed lists."
            )
        deduplicated_case_groups.append({
            "task_id": "test_translator",
            "target_checkpoint": "C4",
            "event_issue_key": "python_inequality_assertion_error",
            "case_ids": sorted(row["case_id"] for row in translator_cases),
            "judge_issue_keys": sorted(set(row["issue_key"] for row in translator_cases)),
            "deduplication_rule": "Same task/checkpoint, same `!=` behavior, identical target CLI error signature, same assertion-discovery code change.",
        })

    eval_cfg = read_json(evaluation_dir / "evaluation_config.json")
    task_order: dict[str, dict[str, int]] = {}
    eval_runs = [json.loads(line) for line in (evaluation_dir / "evaluation_runs.jsonl").read_text(encoding="utf-8").splitlines() if line]
    checkpoints_by_task: dict[str, set[str]] = defaultdict(set)
    for run in eval_runs:
        checkpoints_by_task[run["task_id"]].add(run["target_checkpoint"])
    for task, checkpoints in checkpoints_by_task.items():
        task_order[task] = {checkpoint: i for i, checkpoint in enumerate(sorted(checkpoints, key=lambda x: int(x[1:])), start=0)}

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in effective:
        if row["effective_verdict"] == "confirmed_regression":
            issue_key = normalize_issue_key(row.get("event_issue_key_override", row["issue_key"]), row["case_id"])
            row["normalized_issue_key"] = issue_key
            grouped[(row["task_id"], issue_key)].append(row)

    events: list[dict[str, Any]] = []
    for (task, issue_key), rows in sorted(grouped.items()):
        checkpoint_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            observed = row.get("repeat_checkpoints_deduplicated") or [row["target_checkpoint"]]
            for checkpoint in observed:
                checkpoint_rows[checkpoint].append(row)
        ordered = sorted(checkpoint_rows, key=lambda cp: task_order.get(task, {}).get(cp, 10**9))
        current: dict[str, Any] | None = None
        previous_index: int | None = None
        for checkpoint in ordered:
            index = task_order.get(task, {}).get(checkpoint, 10**9)
            if current is None or (previous_index is not None and index > previous_index + 1):
                if current is not None:
                    events.append(current)
                current = {
                    "task_id": task,
                    "issue_key": issue_key,
                    "first_target_checkpoint": checkpoint,
                    "observed_checkpoints": [],
                    "case_ids": [],
                    "candidate_test_files": [],
                    "issue_summaries": [],
                }
            current["observed_checkpoints"].append(checkpoint)
            for row in checkpoint_rows[checkpoint]:
                current["case_ids"].append(row["case_id"])
                current["candidate_test_files"].append(row["candidate_test_file"])
                current["issue_summaries"].append(row["issue_summary"])
            previous_index = index
        if current is not None:
            events.append(current)

    for index, event in enumerate(events, start=1):
        event["event_id"] = f"MGV2-E{index:03d}"
        for key in ("case_ids", "candidate_test_files", "issue_summaries", "observed_checkpoints"):
            event[key] = sorted(set(event[key]))

    report = {
        "format": "rq1-recovery-v2-mg-adjudication-report/v1",
        "evaluation_dir": str(evaluation_dir.relative_to(ROOT)),
        "judge_model": summary.get("model"),
        "judge_reasoning_effort": summary.get("reasoning_effort"),
        "model_call_attempt_count": sum(len(manifest.get("attempts", [])) for manifest in call_manifests),
        "judge_input_tokens": input_tokens,
        "judge_output_tokens": output_tokens,
        "case_count": len(effective),
        "raw_verdict_counts": dict(raw_counts),
        "judge_effective_verdict_counts": dict(judge_effective_counts),
        "effective_verdict_counts": dict(Counter(row["effective_verdict"] for row in effective)),
        "effective_confirmed_case_count": sum(row["effective_verdict"] == "confirmed_regression" for row in effective),
        "unique_confirmed_event_count": len(events),
        "confirmed_events_by_task": dict(Counter(event["task_id"] for event in events)),
        "consistency_conflict_cluster_count": len(conflict_clusters),
        "consistency_conflict_clusters": conflict_clusters,
        "deduplicated_case_groups": deduplicated_case_groups,
        "candidate_cases": effective,
        "confirmed_events": events,
        "interpretation": (
            "This is an MG-only recovery-batch result. Cases with confidence below 0.80, an inconclusive verdict, "
            "or a human-review flag are treated as inconclusive. Repeated checkpoints and tests with the same "
            "task/issue key are merged into event episodes. Automated adjudication is evidence, not an independent human oracle."
        ),
    }
    write_json(adjudication_dir / "mg_v2_adjudicated_cases.json", {
        "format": report["format"] + "/cases",
        "case_count": len(effective),
        "cases": effective,
    })
    write_json(adjudication_dir / "mg_v2_confirmed_events.json", {
        "format": report["format"] + "/events",
        "deduplication_rule": "Same task plus normalized judge issue_key, with the documented test_translator shared-failure group merged; persistent consecutive checkpoint observations form one episode. Conflicting file_backup/C3 cluster is excluded.",
        "event_count": len(events),
        "events": events,
    })
    write_json(adjudication_dir / "mg_v2_consistency_audit.json", {
        "format": report["format"] + "/consistency-audit",
        "conflict_clusters": conflict_clusters,
        "deduplicated_case_groups": deduplicated_case_groups,
    })
    write_json(adjudication_dir / "mg_v2_adjudication_summary.json", report)

    lines = [
        "# MG-only v2 Regression Adjudication",
        "",
        f"- Blinded candidate cases: {len(effective)}",
        f"- Judge-confirmed case decisions at confidence ≥ 0.80: {judge_effective_counts['confirmed_regression']}",
        f"- Confirmed cases after consistency audit: {report['effective_confirmed_case_count']}",
        f"- Deduplicated confirmed event episodes: {len(events)}",
        f"- Judge: {summary.get('model')} ({summary.get('reasoning_effort')})",
        f"- Judge call attempts: {report['model_call_attempt_count']}; usage: {input_tokens:,} input and {output_tokens:,} output tokens.",
        "- No Original or Vanilla suites were run in this supplemental batch.",
        "- Results are limited to MG-generated test detections; shared official base-test failures were excluded from these cases.",
        "",
        "## Verdicts",
        "",
        "| Verdict | Raw decisions | Effective decisions |",
        "|---|---:|---:|",
    ]
    adjusted_counts = Counter(row["effective_verdict"] for row in effective)
    for verdict in sorted(set(raw_counts) | set(adjusted_counts)):
        lines.append(f"| {verdict} | {raw_counts[verdict]} | {adjusted_counts[verdict]} |")
    if conflict_clusters:
        lines.extend(["", "## Consistency conflict", ""])
        for cluster in conflict_clusters:
            lines.append(
                f"- `{cluster['task_id']}/{cluster['target_checkpoint']}`: {cluster['conflict_rule']} "
                f"Cases: {', '.join(cluster['case_ids'])}. This cluster contributes no confirmed event."
            )
    if deduplicated_case_groups:
        lines.extend(["", "## Duplicate test detections merged", ""])
        for group in deduplicated_case_groups:
            lines.append(
                f"- `{group['task_id']}/{group['target_checkpoint']}`: merged cases "
                f"{', '.join(group['case_ids'])} under `{group['event_issue_key']}`."
            )
    lines.extend(["", "## Confirmed events", "", "| Event | Task | First checkpoint | Observed checkpoints | Tests | Issue |", "|---|---|---:|---|---|---|"])
    for event in events:
        lines.append(
            f"| {event['event_id']} | {event['task_id']} | {event['first_target_checkpoint']} | "
            f"{', '.join(event['observed_checkpoints'])} | {', '.join(event['candidate_test_files'])} | {event['issue_key']} |"
        )
    lines.extend([
        "",
        "Low-confidence, flagged, and inconclusive reviews are not counted as confirmed events. This automated judge result is not an independent human oracle.",
        "",
    ])
    (adjudication_dir / "MG_v2_adjudication_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({k: report[k] for k in (
        "case_count", "raw_verdict_counts", "judge_effective_verdict_counts", "effective_verdict_counts",
        "model_call_attempt_count", "judge_input_tokens", "judge_output_tokens",
        "effective_confirmed_case_count", "consistency_conflict_cluster_count", "unique_confirmed_event_count",
        "confirmed_events_by_task"
    )}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

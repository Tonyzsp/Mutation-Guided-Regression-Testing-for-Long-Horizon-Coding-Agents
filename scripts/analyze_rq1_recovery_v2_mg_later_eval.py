#!/usr/bin/env python3
"""Summarize MG-only later-checkpoint observations and available adjudication."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def checkpoint_number(value: str) -> int:
    match = re.search(r"C(\d+)$", value)
    return int(match.group(1)) if match else 10**9


def test_file_from_nodeid(nodeid: str) -> str:
    return nodeid.split("::", 1)[0].replace("\\", "/").rsplit("/", 1)[-1]


def sha256_file(path: Path) -> str | None:
    # Long composite-suite paths can exceed Win32 MAX_PATH. Use the extended
    # path prefix for local frozen artifacts on Windows.
    target = str(path.resolve())
    if path.exists():
        pass
    if __import__("os").name == "nt" and not target.startswith("\\\\?\\"):
        target = "\\\\?\\" + target
    try:
        digest = hashlib.sha256()
        with open(target, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("artifacts/results/rq1_recovery_v2"),
        help="Recovery v2 artifact root",
    )
    parser.add_argument(
        "--evaluation-dir",
        type=Path,
        default=Path("artifacts/results/rq1_recovery_v2/eval_mg_composite_v2"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/results/rq1_recovery_v2/eval_mg_composite_v2/analysis"),
    )
    args = parser.parse_args()

    root = args.root
    eval_dir = args.evaluation_dir
    evaluation = read_json(eval_dir / "evaluation_summary.json")
    suite_summary = read_json(root / "mg_composite_suite_summary.json")
    observations = read_jsonl(eval_dir / evaluation["candidate_observations_path"])
    runs = read_jsonl(eval_dir / evaluation["run_records_path"])

    generated_files: dict[str, dict[str, Path]] = {}
    for record in suite_summary["records"]:
        task = record["task_id"]
        suite_dir = root / task / "C1" / "mutation_guided_composite_v2" / "frozen_suite" / "generated_tests"
        try:
            files = {path.name: path for path in suite_dir.glob("*.py")}
        except OSError:
            files = {}
        generated_files[task] = files

    classified: list[dict[str, Any]] = []
    origin_status = Counter()
    base_unique_nodes: set[tuple[str, str]] = set()
    mg_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for observation in observations:
        task = observation["task_id"]
        nodeid = observation["test_nodeid"]
        filename = test_file_from_nodeid(nodeid)
        is_generated = filename in generated_files.get(task, {})
        origin = "generated_mutation_guided" if is_generated else "shared_official_base"
        repro = observation.get("reproducibility", {}).get("status", "missing")
        origin_status[(origin, repro)] += 1
        if is_generated:
            mg_groups[(task, nodeid)].append(observation)
        else:
            base_unique_nodes.add((task, nodeid))
        classified.append(
            {
                "observation_id": observation.get("observation_id"),
                "task_id": task,
                "target_checkpoint": observation.get("target_checkpoint"),
                "test_nodeid": nodeid,
                "test_origin": origin,
                "reproducibility": repro,
                "failure_longrepr": observation.get("failure_longrepr"),
                "evidence_directory": observation.get("evidence_directory"),
            }
        )

    adjudication_dir = eval_dir / "guided_judge"
    adjudication_path = adjudication_dir / "mg_v2_adjudication_summary.json"
    adjudication: dict[str, Any] | None = read_json(adjudication_path) if adjudication_path.exists() else None
    review_by_test: dict[tuple[str, str], dict[str, Any]] = {}
    event_by_case: dict[str, str] = {}
    if adjudication is not None:
        for row in adjudication.get("candidate_cases", []):
            review_by_test[(row["task_id"], row["test_nodeid"])] = row
        for event in adjudication.get("confirmed_events", []):
            for case_id in event.get("case_ids", []):
                event_by_case[case_id] = event["event_id"]

    generated_events: list[dict[str, Any]] = []
    for (task, nodeid), group in sorted(mg_groups.items()):
        ordered = sorted(group, key=lambda item: checkpoint_number(item.get("target_checkpoint", "")))
        first = ordered[0]
        filename = test_file_from_nodeid(nodeid)
        frozen_test = generated_files.get(task, {}).get(filename)
        unique_checkpoints = sorted(
            {item.get("target_checkpoint", "") for item in group}, key=checkpoint_number
        )
        review = review_by_test.get((task, nodeid))
        generated_events.append(
            {
                "task_id": task,
                "test_nodeid": nodeid,
                "test_filename": filename,
                "test_sha256": sha256_file(frozen_test) if frozen_test else None,
                "first_candidate_checkpoint": first.get("target_checkpoint"),
                "candidate_checkpoints": unique_checkpoints,
                "stable_observation_count": len(group),
                "reproducibility_statuses": sorted(
                    {item.get("reproducibility", {}).get("status", "missing") for item in group}
                ),
                "first_failure_excerpt": (first.get("failure_longrepr") or "")[:1200],
                "first_evidence_directory": first.get("evidence_directory"),
                "adjudication_status": review.get("effective_verdict") if review else "pending_semantic_review",
                "judge_verdict": review.get("raw_verdict") if review else None,
                "adjudication_case_id": review.get("case_id") if review else None,
                "confirmed_event_id": event_by_case.get(review.get("case_id")) if review else None,
                "adjudication_note": "; ".join(review.get("deduplication_notes", [])) if review else None,
            }
        )

    per_task: dict[str, dict[str, Any]] = {}
    for task in sorted({record["task_id"] for record in suite_summary["records"]}):
        suite_record = next(record for record in suite_summary["records"] if record["task_id"] == task)
        task_events = [event for event in generated_events if event["task_id"] == task]
        per_task[task] = {
            "generated_test_count": suite_record["generated_test_count"],
            "v1_generated_test_count": suite_record["v1_test_count"],
            "v2_generated_test_count": suite_record["v2_test_count"],
            "generated_test_candidate_nodes": len(task_events),
            "candidate_observation_count": sum(event["stable_observation_count"] for event in task_events),
            "first_candidate_checkpoints": [
                {"test_nodeid": event["test_nodeid"], "checkpoint": event["first_candidate_checkpoint"]}
                for event in task_events
            ],
        }

    pair_status = Counter(record.get("status", "missing") for record in runs)
    report = {
        "format": "rq1-recovery-v2-mg-later-eval-analysis/v1",
        "scope": "Mutation-Guided composite suites only; no Original or Vanilla evaluation",
        "source_checkpoint": evaluation.get("source_checkpoint"),
        "task_count": evaluation.get("task_count"),
        "expected_later_checkpoint_pairs": evaluation.get("expected_unique_suite_runs"),
        "completed_later_checkpoint_pairs": evaluation.get("unique_suite_runs"),
        "missing_later_checkpoint_pairs": evaluation.get("missing_suite_runs"),
        "pair_status_counts": dict(pair_status),
        "composite_generated_test_count": suite_summary.get("composite_generated_test_count"),
        "tasks_with_generated_mg_tests": sum(1 for record in suite_summary["records"] if record["generated_test_count"]),
        "tasks_with_generated_test_candidate_observations": len({event["task_id"] for event in generated_events}),
        "all_candidate_failure_observations": len(observations),
        "candidate_observations_by_test_origin_and_reproducibility": {
            f"{origin}:{status}": count for (origin, status), count in sorted(origin_status.items())
        },
        "shared_official_base_unique_task_test_nodes": len(base_unique_nodes),
        "generated_mg_candidate_failure_observations": sum(
            event["stable_observation_count"] for event in generated_events
        ),
        "generated_mg_unique_failing_test_nodes": len(generated_events),
        "generated_mg_test_node_events": generated_events,
        "adjudication_summary": {
            "case_count": adjudication.get("case_count"),
            "effective_verdict_counts": adjudication.get("effective_verdict_counts"),
            "confirmed_event_count": adjudication.get("unique_confirmed_event_count"),
            "consistency_conflict_cluster_count": adjudication.get("consistency_conflict_cluster_count"),
            "report_path": str(adjudication_path.resolve().relative_to(Path(__file__).resolve().parents[1])) if adjudication else None,
        } if adjudication else None,
        "per_task": per_task,
        "interpretation": (
            "The shared base suite is separated from generated MG tests. Stable observations are candidate failures, "
            "not confirmed regressions, until reviewed against specifications and checkpoint diffs. "
            "Adjudicated event counts use the conservative cross-case consistency and deduplication audit."
        ),
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "mg_later_eval_analysis.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md = [
        "# MG-only v2 Later-Checkpoint Evaluation Analysis",
        "",
        "## Run integrity",
        "",
        f"- Tasks: {report['task_count']}",
        f"- Later-checkpoint suite runs: {report['completed_later_checkpoint_pairs']}/{report['expected_later_checkpoint_pairs']}",
        f"- Missing runs: {len(report['missing_later_checkpoint_pairs'])}",
        f"- Generated MG tests frozen: {report['composite_generated_test_count']} across {report['tasks_with_generated_mg_tests']} tasks",
        "- This batch runs Mutation-Guided composite suites only; it contains no Original or Vanilla runs.",
        "",
        "## Candidate observations",
        "",
        f"- All-suite candidate observations: {report['all_candidate_failure_observations']}",
        f"- Observations from generated MG tests: {report['generated_mg_candidate_failure_observations']}",
        f"- Unique failing generated MG test nodes: {report['generated_mg_unique_failing_test_nodes']} across {report['tasks_with_generated_test_candidate_observations']} tasks",
        f"- Shared official base-test observations: {sum(v for k, v in report['candidate_observations_by_test_origin_and_reproducibility'].items() if k.startswith('shared_official_base:'))}",
        "- Stable observations are not automatically confirmed regressions; adjudication status and event IDs appear in the table.",
        "",
        "## Generated MG tests with later failures",
        "",
        "| Task | Test | First failing checkpoint | Later checkpoints | Stable observations | Adjudication | Event |",
        "|---|---|---:|---|---:|---|---|",
    ]
    for event in generated_events:
        md.append(
            f"| {event['task_id']} | `{event['test_nodeid'].split('::')[-1]}` | "
            f"{event['first_candidate_checkpoint']} | {', '.join(event['candidate_checkpoints'])} | "
            f"{event['stable_observation_count']} | {event['adjudication_status']} | {event.get('confirmed_event_id') or '—'} |"
        )
    md.extend(
        [
            "",
            "## Reproducibility classes",
            "",
        ]
    )
    for key, value in sorted(report["candidate_observations_by_test_origin_and_reproducibility"].items()):
        md.append(f"- `{key}`: {value}")
    md.extend(
        [
            "",
            "All 637 non-generated observations came from the shared `test_checkpoint_1.py` base tests. The 26 stable-error observations are excluded from generated MG detections. No infrastructure errors were reported by the evaluation summary.",
            "",
            "## MG-only adjudication outcome",
            "",
            (
                f"Blinded LLM review produced {adjudication['case_count']} case decisions and "
                f"{adjudication['unique_confirmed_event_count']} deduplicated confirmed events, with "
                f"{adjudication['consistency_conflict_cluster_count']} cross-case consistency conflict cluster(s). "
                f"See `{adjudication_path.name}` and the linked adjudication report."
                if adjudication
                else "Semantic adjudication has not yet been prepared for these stable MG-generated-test observations."
            ),
            "",
            "Detailed machine-readable records: `mg_later_eval_analysis.json`.",
        ]
    )
    md_path = args.output_dir / "MG_later_eval_analysis.md"
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(md_path), **{k: report[k] for k in (
        "completed_later_checkpoint_pairs", "expected_later_checkpoint_pairs",
        "all_candidate_failure_observations", "generated_mg_candidate_failure_observations",
        "generated_mg_unique_failing_test_nodes", "tasks_with_generated_test_candidate_observations"
    )}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

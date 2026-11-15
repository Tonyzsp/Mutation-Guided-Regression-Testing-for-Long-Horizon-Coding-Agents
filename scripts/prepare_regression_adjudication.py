"""Create method-blinded, evidence-complete cases for the fixed RQ1 judge."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def safe_child(root: Path, relative: str) -> Path:
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root.resolve(strict=True)):
        raise ValueError(f"evidence path escaped evaluation root: {relative}")
    return path


def read_log(directory: Path, name: str, limit: int = 50_000) -> str:
    path = directory / name
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > limit:
        return text[:limit] + "\n[truncated for blinded review; full log remains in the evaluation artifact]"
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation-dir", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    evaluation_dir = args.evaluation_dir.resolve(strict=True)
    allowed_root = (ROOT / "artifacts" / "results" / "rq1_full_36x1").resolve(strict=True)
    if not evaluation_dir.is_relative_to(allowed_root):
        raise ValueError("evaluation directory must remain under the RQ1 result root")
    output_dir = args.output_dir.resolve() if args.output_dir else evaluation_dir / "blinded_adjudication"
    if not output_dir.is_relative_to(evaluation_dir) or output_dir.exists():
        raise ValueError("adjudication output must be a new directory inside the evaluation directory")
    observations_path = evaluation_dir / "candidate_failure_observations.jsonl"
    observations = read_jsonl(observations_path)
    stable = [
        row for row in observations
        if row.get("reproducibility", {}).get("status") == "stable_failure"
        and row.get("reproducibility", {}).get("repeat_count") == 3
        and row.get("reproducibility", {}).get("repeated_failure_count") == 3
    ]
    output_dir.mkdir(parents=True)
    cases_dir = output_dir / "cases"
    cases_dir.mkdir()
    mapping: list[dict[str, Any]] = []
    for index, observation in enumerate(stable, 1):
        case_id = f"case-{index:04d}"
        evidence_root = evaluation_dir
        source_spec = safe_child(evidence_root, observation["source_specification_path"])
        target_spec = safe_child(evidence_root, observation["target_specification_path"])
        diff = safe_child(evidence_root, observation["workspace_diff_path"])
        evidence_manifest = safe_child(evidence_root, observation["evidence_manifest_path"])
        source_agent_manifest = safe_child(evidence_root, observation["source_agent_run_manifest_path"]) if observation.get("source_agent_run_manifest_path") else None
        target_agent_manifest = safe_child(evidence_root, observation["target_agent_run_manifest_path"]) if observation.get("target_agent_run_manifest_path") else None
        failure_dir = safe_child(ROOT, observation["evidence_run_directory"])
        repeat_runs = observation["reproducibility"]["runs"]
        repeat_summaries = []
        for run in repeat_runs:
            report_rel = run.get("pytest_report_path")
            report_value = read_json(safe_child(failure_dir, report_rel)) if report_rel else None
            repeat_summaries.append({
                "repetition": run["repetition"],
                "run_status": run["run_status"],
                "exit_code": run["exit_code"],
                "test_outcomes": [
                    {"outcome": item.get("outcome"), "longrepr": item.get("longrepr")}
                    for item in run.get("outcomes", [])
                ],
                "report_created": report_value is not None,
            })
        case = {
            "case_id": case_id,
            "task_id": observation["task_id"],
            "trajectory_id": observation["trajectory_id"],
            "source_checkpoint": observation["source_checkpoint"],
            "target_checkpoint": observation["target_checkpoint"],
            "source_specification": source_spec.read_text(encoding="utf-8"),
            "target_specification": target_spec.read_text(encoding="utf-8"),
            "frozen_test_source": observation.get("test_code", ""),
            "workspace_diff": diff.read_text(encoding="utf-8", errors="replace"),
            "checkpoint_evidence": read_json(evidence_manifest),
            "source_agent_run_manifest": read_json(source_agent_manifest) if source_agent_manifest else None,
            "target_agent_run_manifest": read_json(target_agent_manifest) if target_agent_manifest else None,
            "initial_stdout_log": read_log(failure_dir, "stdout.log"),
            "initial_stderr_log": read_log(failure_dir, "stderr.log"),
            "failure_trace": observation.get("failure_longrepr"),
            "repeat_runs": repeat_summaries,
        }
        case_path = cases_dir / f"{case_id}.json"
        case_path.write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        mapping.append({
            "case_id": case_id,
            "observation_id": observation["observation_id"],
            "task_id": observation["task_id"],
            "target_checkpoint": observation["target_checkpoint"],
            "test_nodeid": observation["test_nodeid"],
            "test_sha256": observation.get("test_sha256"),
            "methods_detected": observation.get("methods_detected", []),
            "suite_sha256": observation.get("suite_sha256"),
            "case_sha256": hashlib.sha256(case_path.read_bytes()).hexdigest(),
        })
    (output_dir / "private_case_mapping.json").write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary = {
        "format": "rq1-blinded-adjudication-input/v1",
        "evaluation_dir": str(evaluation_dir.relative_to(ROOT)),
        "source_observation_count": len(observations),
        "stable_failure_case_count": len(stable),
        "excluded_nonstable_observation_count": len(observations) - len(stable),
        "cases_directory": "cases",
        "private_mapping_path": "private_case_mapping.json",
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if not stable:
        (output_dir / "llm_judge_reviews.json").write_text("{\"reviews\": []}\n", encoding="utf-8")
        (output_dir / "llm_judge_summary.json").write_text(json.dumps({
            "format": "rq1-llm-adjudication/v1",
            "status": "complete",
            "model": None,
            "reasoning_effort": None,
            "case_count": 0,
            "review_count": 0,
            "error_count": 0,
            "errors": [],
            "reviews_path": "llm_judge_reviews.json",
            "note": "No stable failure cases met the predeclared three-of-three reproducibility rule.",
        }, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

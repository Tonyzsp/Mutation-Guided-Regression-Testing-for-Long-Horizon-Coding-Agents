#!/usr/bin/env python3
"""Prepare method-blinded adjudication cases for MG-generated v2 detections."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_V2_ROOT = ROOT / "artifacts" / "results" / "rq1_recovery_v2"
DEFAULT_EVALUATION = DEFAULT_V2_ROOT / "eval_mg_composite_v2"


def fs_path(path: Path) -> str:
    value = os.path.abspath(os.fspath(path))
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        return "\\\\?\\" + value
    return value


def read_text(path: Path) -> str:
    with open(fs_path(path), "r", encoding="utf-8", errors="replace") as stream:
        return stream.read()


def write_text(path: Path, value: str) -> None:
    os.makedirs(fs_path(path.parent), exist_ok=True)
    with open(fs_path(path), "w", encoding="utf-8", newline="") as stream:
        stream.write(value)


def read_json(path: Path) -> Any:
    return json.loads(read_text(path))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in read_text(path).splitlines() if line.strip()]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(fs_path(path), "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def task_generated_tests(v2_root: Path, task: str) -> dict[str, Path]:
    directory = v2_root / task / "C1" / "mutation_guided_composite_v2" / "frozen_suite" / "generated_tests"
    result: dict[str, Path] = {}
    try:
        with os.scandir(fs_path(directory)) as entries:
            for entry in entries:
                if entry.is_file() and entry.name.endswith(".py"):
                    result[entry.name] = Path(entry.path)
    except OSError:
        return {}
    return result


def node_filename(nodeid: str) -> str:
    return nodeid.split("::", 1)[0].replace("\\", "/").rsplit("/", 1)[-1]


def cnum(checkpoint: str) -> int:
    match = re.search(r"C(\d+)$", checkpoint)
    return int(match.group(1)) if match else 10**9


def report_test(report_path: Path, nodeid: str) -> dict[str, Any]:
    report = read_json(report_path)
    item = next((row for row in report.get("tests", []) if row.get("nodeid") == nodeid), None)
    if item is None:
        raise ValueError(f"pytest report lacks candidate test: {report_path.name} / {nodeid}")
    phases: dict[str, Any] = {}
    for phase_name in ("setup", "call", "teardown"):
        phase = item.get(phase_name)
        if not isinstance(phase, dict):
            continue
        crash = phase.get("crash") if isinstance(phase.get("crash"), dict) else {}
        phases[phase_name] = {
            "outcome": phase.get("outcome"),
            "crash_message": str(crash.get("message", ""))[:4000],
            "traceback": phase.get("traceback", []),
            "longrepr": str(phase.get("longrepr", ""))[:12000],
        }
    return {
        "outcome": item.get("outcome"),
        "phases": phases,
        "longrepr": str(item.get("longrepr", ""))[:16000],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v2-root", type=Path, default=DEFAULT_V2_ROOT)
    parser.add_argument("--evaluation-dir", type=Path, default=DEFAULT_EVALUATION)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_EVALUATION / "guided_judge")
    args = parser.parse_args()

    v2_root = args.v2_root.resolve(strict=True)
    evaluation_dir = args.evaluation_dir.resolve(strict=True)
    output_dir = Path(os.path.abspath(args.output_dir))
    if not output_dir.is_relative_to(evaluation_dir):
        raise ValueError("adjudication output must stay inside this v2 evaluation directory")
    if os.path.exists(fs_path(output_dir)):
        existing_cases = output_dir / "cases"
        try:
            if os.path.isdir(fs_path(existing_cases)) and not os.listdir(fs_path(existing_cases)):
                os.rmdir(fs_path(existing_cases))
            if not os.listdir(fs_path(output_dir)):
                os.rmdir(fs_path(output_dir))
            else:
                raise FileExistsError(f"refusing to overwrite existing v2 adjudication directory: {output_dir}")
        except OSError as error:
            raise FileExistsError(f"refusing to overwrite non-empty v2 adjudication directory: {output_dir}") from error

    evaluation = read_json(evaluation_dir / "evaluation_summary.json")
    if evaluation.get("status") != "complete" or evaluation.get("unique_suite_runs") != 160:
        raise ValueError("v2 evaluation is incomplete; expected all 160 later-checkpoint suite runs")
    suite_summary = read_json(v2_root / "mg_composite_suite_summary.json")
    c1_summary = read_json(v2_root / "mg_composite_c1_validation_summary.json")
    c1_by_task = {row["task_id"]: row for row in c1_summary["records"]}
    observations = read_jsonl(evaluation_dir / evaluation["candidate_observations_path"])

    generated_paths: dict[str, dict[str, Path]] = {}
    for record in suite_summary["records"]:
        generated_paths[record["task_id"]] = task_generated_tests(v2_root, record["task_id"])

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for observation in observations:
        task = observation["task_id"]
        filename = node_filename(observation["test_nodeid"])
        if filename not in generated_paths.get(task, {}):
            continue  # shared official tests are not attributable to the generated MG tests
        repro = observation.get("reproducibility", {})
        if repro.get("status") != "stable_failure" or repro.get("repeat_count") != 3 or repro.get("repeated_failure_count") != 3:
            continue
        groups[(task, observation["test_nodeid"])].append(observation)

    if not groups:
        raise ValueError("no stable later-checkpoint failures from generated MG tests")

    cases_dir = output_dir / "cases"
    mapping: list[dict[str, Any]] = []
    case_payloads: list[tuple[Path, str]] = []
    case_index = 0
    for (task, nodeid), group in sorted(groups.items()):
        # One blind case per frozen test behavior, anchored at its first failing checkpoint.
        observation = sorted(group, key=lambda row: cnum(row["target_checkpoint"]))[0]
        c1_record = c1_by_task.get(task)
        if not c1_record or c1_record.get("status") != "stable_pass":
            raise ValueError(f"C1 composite suite did not pass its three-run gate for {task}")
        successful_c1_runs = [
            run for run in c1_record.get("runs", [])
            if run.get("status") == "passed"
            and run.get("pytest_status") == "complete"
            and not run.get("generated_tests_not_passing")
            and not run.get("missing_generated_tests")
        ]
        if len(successful_c1_runs) < 3:
            raise ValueError(f"C1 composite suite lacks three successful runs for {task}")
        if any(run.get("generated_tests_not_passing") for run in c1_record["runs"] if run.get("status") == "passed"):
            raise ValueError(f"a generated test failed at C1 for {task}")

        generated_path = generated_paths[task][node_filename(nodeid)]
        frozen_source = read_text(generated_path)
        evidence_dir = evaluation_dir / observation["evidence_directory"]
        run_dir = ROOT / observation["evidence_run_directory"]
        manifest = read_json(evidence_dir / "evidence_manifest.json")
        source_spec = read_text(evidence_dir / "source_specification.md")
        target_spec = read_text(evidence_dir / "target_specification.md")
        workspace_diff = read_text(evidence_dir / "workspace_diff.patch")
        initial_result = report_test(run_dir / "pytest-report.json", nodeid)
        repeats = []
        for repeat in observation["reproducibility"]["runs"]:
            if repeat.get("run_status") != "complete" or repeat.get("exit_code") != 1:
                raise ValueError(f"candidate repeat is not a normal failure for {task}/{nodeid}")
            repeats.append({
                "repetition": repeat["repetition"],
                "test_result": report_test(run_dir / repeat["pytest_report_path"], nodeid),
            })

        case_index += 1
        case_id = f"case-{case_index:04d}"
        anonymous_nodeid = f"candidate_test.py::{nodeid.split('::')[-1]}"
        case = {
            "case_id": case_id,
            "task_id": task,
            "source_checkpoint": observation["source_checkpoint"],
            "target_checkpoint": observation["target_checkpoint"],
            "test_nodeid": anonymous_nodeid,
            "frozen_test_source": frozen_source,
            "frozen_test_sha256": sha256_file(generated_path),
            "source_checkpoint_validation": {
                "checkpoint": "C1",
                "status": "stable_pass",
                "repeat_count": 3,
                "evidence": "The frozen composite suite collected all generated tests; none failed or went missing in each of three C1 runs.",
                "suite_sha256": c1_record["input_manifest"]["suite_sha256"],
            },
            "target_observation_class": "stable_failure",
            "source_specification": source_spec,
            "target_specification": target_spec,
            "workspace_diff": workspace_diff,
            "checkpoint_evidence": manifest,
            "initial_target_result": initial_result,
            "repeat_runs": repeats,
        }
        case_path = cases_dir / f"{case_id}.json"
        case_text = json.dumps(case, ensure_ascii=False, indent=2) + "\n"
        case_payloads.append((case_path, case_text))
        mapping.append({
            "case_id": case_id,
            "observation_id": observation["observation_id"],
            "task_id": task,
            "target_checkpoint": observation["target_checkpoint"],
            "test_nodeid": nodeid,
            "target_observation_class": "stable_failure",
            "methods_detected": ["mutation_guided"],
            "suite_sha256": observation.get("suite_sha256"),
            "candidate_test_sha256": sha256_file(generated_path),
            "candidate_test_file": node_filename(nodeid),
            "repeat_checkpoints_deduplicated": sorted({row["target_checkpoint"] for row in group}, key=cnum),
            "case_sha256": sha256_text(case_text),
        })

    os.makedirs(fs_path(cases_dir), exist_ok=False)
    for case_path, case_text in case_payloads:
        write_text(case_path, case_text)
    write_text(output_dir / "private_case_mapping.json", json.dumps(mapping, indent=2, ensure_ascii=False) + "\n")
    output_summary = {
        "format": "rq1-recovery-v2-mg-blinded-adjudication-input/v1",
        "source_evaluation_dir": str(evaluation_dir.relative_to(ROOT)),
        "scope": "MG-generated test candidate observations only; shared official base-test failures excluded",
        "source_candidate_observation_count": len(observations),
        "stable_mg_generated_test_nodes": len(groups),
        "case_count": case_index,
        "observations_merged_into_earliest_checkpoint_cases": sum(len(group) - 1 for group in groups.values()),
        "all_cases_method_blinded": True,
        "all_cases_include_C1_pass_and_3x_target_failure_evidence": True,
        "case_ids": [row["case_id"] for row in mapping],
        "cases_directory": "cases",
        "private_mapping_path": "private_case_mapping.json",
        "judge_prompt": "prompts/regression_adjudication_v1.md",
        "schema": "schemas/regression-adjudication-review.schema.json",
    }
    write_text(output_dir / "summary.json", json.dumps(output_summary, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(output_summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

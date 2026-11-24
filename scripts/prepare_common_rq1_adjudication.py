"""Prepare method-blinded, earliest-checkpoint RQ1 adjudication cases.

This is a preparation-only tool: it reads a completed later-checkpoint evaluation,
validates the exact C1 test source for each candidate, and writes a private
method mapping plus method-blinded cases.  It never invokes the judge or changes
any frozen suite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import defaultdict
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from prepare_regression_adjudication import (
    BASELINE_PATH,
    RESULT_ROOT,
    ROOT,
    filesystem_path,
    is_dir,
    is_file,
    limited_text,
    read_json,
    read_jsonl,
    read_text,
    report_test,
    safe_child,
    sha256_file,
    write_text,
)


PACKAGE_ROOT = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
UNIT_PATH = ROOT / "config" / "rq1_full_36x1_candidate_units.json"
RESULTS_ROOT = ROOT / "artifacts" / "results"
DEFAULT_VANILLA_SUFFIX = "vanilla_formal_v5_20261116"
DEFAULT_GUIDED_SUFFIX = "mutation_guided_composite_v2"
DEFAULT_GUIDED_ROOT = RESULTS_ROOT / "rq1_recovery_v2"
METHODS = ("original", "vanilla_llm", "mutation_guided")


def safe_relative_test(nodeid: str) -> Path:
    source_rel = nodeid.split("::", 1)[0].replace("\\", "/")
    source_parts = PurePosixPath(source_rel).parts
    if not source_parts or source_parts[0] != "base_tests" or ".." in source_parts:
        raise ValueError(f"unexpected frozen test path in node ID: {nodeid}")
    return Path(*source_parts[1:])


@lru_cache(maxsize=1)
def baseline_by_task() -> dict[str, dict[str, Any]]:
    baseline = read_json(BASELINE_PATH)
    if baseline.get("status") != "complete":
        raise ValueError("C1 base audit is incomplete")
    rows = {row["task_id"]: row for row in baseline.get("records", [])}
    if len(rows) != 36:
        raise ValueError("C1 base audit does not contain 36 task records")
    return rows


def c1_source_validation(task: str, nodeid: str, generation_dir: Path | None, frozen: dict[str, Any] | None) -> dict[str, Any]:
    baseline = baseline_by_task().get(task)
    if baseline is None:
        raise ValueError(f"missing C1 baseline record for {task}")
    if nodeid in set(baseline.get("stable_pass_test_ids", [])):
        pass_runs = []
        for repetition in baseline.get("repetitions", []):
            outcomes = [test.get("outcome") for test in repetition.get("tests", []) if test.get("nodeid") == nodeid]
            if len(outcomes) != 1:
                raise ValueError(f"C1 baseline repetition lacks a unique outcome for {task}/{nodeid}")
            pass_runs.append({"repetition": repetition.get("repetition"), "outcome": outcomes[0]})
    else:
        if generation_dir is None or frozen is None:
            raise ValueError(f"Original suite has no C1 stable-pass evidence for {task}/{nodeid}")
        validation_path = safe_child(generation_dir, str(frozen["validation_log_path"]))
        retained = [
            row for row in read_jsonl(validation_path)
            if row.get("status") == "retained" and nodeid in row.get("collection_ids", [])
        ]
        if len(retained) != 1:
            raise ValueError(f"missing unique retained C1 validation for {task}/{nodeid}")
        record = retained[0]
        pass_runs = [
            {"repetition": run.get("repetition"), "outcome": "passed" if run.get("passed") is True else "failed"}
            for run in record.get("runs", [])
        ]
    if len(pass_runs) != 3 or not all(run["outcome"] == "passed" for run in pass_runs):
        raise ValueError(f"C1 pass validation is incomplete for {task}/{nodeid}: {pass_runs}")
    return {"checkpoint": "C1", "status": "stable_pass", "repeat_count": len(pass_runs), "runs": pass_runs}


def frozen_source(task: str, nodeid: str, method: str, suffix: str, root: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    expected_method = "vanilla_llm" if method == "vanilla_llm" else "mutation_guided"
    if PurePosixPath(suffix).is_absolute() or ".." in PurePosixPath(suffix).parts or "/" in suffix or "\\" in suffix:
        raise ValueError(f"unsafe suite suffix: {suffix!r}")
    generation_dir = (root / task / "C1" / suffix).resolve(strict=True)
    if not generation_dir.is_relative_to(root):
        raise ValueError(f"frozen suite escaped its configured root for {task}")
    frozen = read_json(generation_dir / "frozen_test_suite.json")
    if frozen.get("task_id") != task or frozen.get("source_checkpoint") != "C1" or frozen.get("method") != expected_method:
        raise ValueError(f"frozen suite identity mismatch for {task}/{method}")
    suite_relative = PurePosixPath(str(frozen["suite_path"]))
    if suite_relative.is_absolute() or ".." in suite_relative.parts or "\\" in str(frozen["suite_path"]):
        raise ValueError(f"unsafe frozen suite path for {task}")
    suite_root = generation_dir.joinpath(*suite_relative.parts)
    if not suite_root.is_relative_to(generation_dir) or not is_dir(suite_root):
        raise ValueError(f"invalid frozen suite path for {task}")

    relative_test = safe_relative_test(nodeid)
    candidates: list[Path] = []
    base_candidate = suite_root / "base_tests" / relative_test
    if is_file(base_candidate):
        candidates.append(base_candidate)
    for generated in frozen.get("generated_test_paths", []):
        path = safe_child(generation_dir, str(generated))
        if path.name == relative_test.name:
            candidates.append(path)
    unique_candidates = list(dict.fromkeys(candidates))
    if len(unique_candidates) != 1:
        raise ValueError(f"expected exactly one frozen test source for {task}/{nodeid}; found {len(unique_candidates)}")
    test_path = unique_candidates[0]
    return test_path, c1_source_validation(task, nodeid, generation_dir, frozen), frozen


def original_source(task: str, nodeid: str) -> tuple[Path, dict[str, Any], None]:
    package_dir = (PACKAGE_ROOT / task / "C1").resolve(strict=True)
    package = read_json(package_dir / "package.json")
    base_relative = PurePosixPath(str(package.get("base_tests_path", "")))
    if not base_relative.parts or base_relative.is_absolute() or ".." in base_relative.parts or "\\" in str(base_relative):
        raise ValueError(f"unsafe C1 base suite path for {task}")
    base_dir = package_dir.joinpath(*base_relative.parts)
    test_path = base_dir / safe_relative_test(nodeid)
    if not is_file(test_path):
        raise FileNotFoundError(test_path)
    return test_path, c1_source_validation(task, nodeid, None, None), None


def configured_root(value: Path) -> Path:
    root = value.resolve(strict=True)
    if not root.is_relative_to(RESULTS_ROOT.resolve(strict=True)):
        raise ValueError(f"suite root must remain under artifacts/results: {root}")
    return root


def checkpoint_order() -> tuple[dict[str, int], dict[tuple[str, str], int]]:
    units = read_json(UNIT_PATH).get("units", [])
    task_order: dict[str, int] = {}
    target_order: dict[tuple[str, str], int] = {}
    for task_index, unit in enumerate(units):
        task = str(unit["task_id"])
        task_order[task] = task_index
        for target_index, checkpoint in enumerate(unit.get("target_checkpoints", [])):
            target_order[(task, str(checkpoint))] = target_index
    if len(task_order) != 36 or len(target_order) != 160:
        raise ValueError("locked RQ1 candidate-unit manifest is incomplete")
    return task_order, target_order


def status_is_reproducible(observation: dict[str, Any], include_stable_errors: bool) -> bool:
    reproducibility = observation.get("reproducibility", {})
    if reproducibility.get("repeat_count") != 3:
        return False
    if reproducibility.get("status") == "stable_failure":
        return reproducibility.get("repeated_failure_count") == 3
    return bool(
        include_stable_errors
        and reproducibility.get("status") == "stable_error"
        and reproducibility.get("repeated_error_count") == 3
    )


def result_evidence(evaluation_dir: Path, observation: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    nodeid = str(observation["test_nodeid"])
    failure_dir = safe_child(ROOT, str(observation["evidence_run_directory"]))
    initial_result = report_test(failure_dir / "pytest-report.json", nodeid)
    if initial_result is None:
        raise ValueError("initial pytest report has no matching test outcome")
    repeats: list[dict[str, Any]] = []
    for run in observation["reproducibility"]["runs"]:
        report_rel = run.get("pytest_report_path")
        report_path = safe_child(failure_dir, str(report_rel)) if report_rel else None
        repeated_result = report_test(report_path, nodeid)
        if repeated_result is None:
            raise ValueError(f"repeat {run.get('repetition')} has no matching pytest report result")
        repeats.append({
            "repetition": run.get("repetition"),
            "run_status": run.get("run_status"),
            "exit_code": run.get("exit_code"),
            "test_result": repeated_result,
        })
    source_spec = safe_child(evaluation_dir, str(observation["source_specification_path"]))
    target_spec = safe_child(evaluation_dir, str(observation["target_specification_path"]))
    diff = safe_child(evaluation_dir, str(observation["workspace_diff_path"]))
    evidence_manifest = safe_child(evaluation_dir, str(observation["evidence_manifest_path"]))
    source_agent_manifest = safe_child(evaluation_dir, str(observation["source_agent_run_manifest_path"])) if observation.get("source_agent_run_manifest_path") else None
    target_agent_manifest = safe_child(evaluation_dir, str(observation["target_agent_run_manifest_path"])) if observation.get("target_agent_run_manifest_path") else None
    evidence = {
        "source_specification": read_text(source_spec),
        "target_specification": read_text(target_spec),
        "workspace_diff": read_text(diff, errors="replace"),
        "checkpoint_evidence": read_json(evidence_manifest),
        "source_agent_run_manifest": read_json(source_agent_manifest) if source_agent_manifest else None,
        "target_agent_run_manifest": read_json(target_agent_manifest) if target_agent_manifest else None,
    }
    return initial_result, repeats, evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation-dir", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--methods", nargs="+", choices=METHODS, default=("original", "vanilla_llm"), help="Methods whose stable observations enter the private mapping")
    parser.add_argument("--include-stable-errors", action="store_true", help="Include 3/3 stable test errors; default is stable assertion failures only")
    parser.add_argument("--vanilla-root", type=Path)
    parser.add_argument("--vanilla-suffix")
    parser.add_argument("--guided-root", type=Path)
    parser.add_argument("--guided-suffix")
    args = parser.parse_args()

    evaluation_dir = args.evaluation_dir.resolve(strict=True)
    if not evaluation_dir.is_relative_to(RESULTS_ROOT.resolve(strict=True)):
        raise ValueError("evaluation directory must remain under artifacts/results")
    summary_input = read_json(evaluation_dir / "evaluation_summary.json")
    config_input = read_json(evaluation_dir / "evaluation_config.json")
    vanilla_root = configured_root(args.vanilla_root or Path(str(config_input.get("vanilla_root", RESULT_ROOT))))
    vanilla_suffix = args.vanilla_suffix or str(summary_input.get("vanilla_suite_suffix") or config_input.get("vanilla_suffix") or DEFAULT_VANILLA_SUFFIX)
    guided_root = configured_root(args.guided_root or Path(str(config_input.get("guided_root", DEFAULT_GUIDED_ROOT))))
    guided_suffix = args.guided_suffix or str(summary_input.get("guided_suite_suffix") or config_input.get("guided_suffix") or DEFAULT_GUIDED_SUFFIX)
    methods = tuple(dict.fromkeys(args.methods))

    output_dir = (args.output_dir or evaluation_dir / "blinded_adjudication_common_v1").resolve()
    if not output_dir.is_relative_to(evaluation_dir) or os.path.exists(filesystem_path(output_dir)):
        raise ValueError("adjudication output must be a new directory inside the evaluation directory")
    task_order, target_order = checkpoint_order()
    observations = read_jsonl(evaluation_dir / "candidate_failure_observations.jsonl")

    candidate_rows: list[dict[str, Any]] = []
    preflight_issues: list[str] = []
    skipped_not_selected = 0
    skipped_not_reproducible = 0
    for observation in observations:
        matched_methods = tuple(method for method in methods if method in observation.get("methods_detected", []))
        if not matched_methods:
            skipped_not_selected += 1
            continue
        if not status_is_reproducible(observation, args.include_stable_errors):
            skipped_not_reproducible += 1
            continue
        task = str(observation.get("task_id"))
        nodeid = str(observation.get("test_nodeid"))
        checkpoint = str(observation.get("target_checkpoint"))
        try:
            source_candidates: list[tuple[str, Path, dict[str, Any]]] = []
            for method in matched_methods:
                if method == "original":
                    test_path, validation, _frozen = original_source(task, nodeid)
                elif method == "vanilla_llm":
                    test_path, validation, _frozen = frozen_source(task, nodeid, method, vanilla_suffix, vanilla_root)
                else:
                    test_path, validation, _frozen = frozen_source(task, nodeid, method, guided_suffix, guided_root)
                source_candidates.append((method, test_path, validation))
            source_hashes = {sha256_file(path) for _method, path, _validation in source_candidates}
            if len(source_hashes) != 1:
                raise ValueError(f"method-shared observation resolves to different C1 tests: {matched_methods}")
            source_validation = source_candidates[0][2]
            if any(validation != source_validation for _method, _path, validation in source_candidates[1:]):
                raise ValueError(f"method-shared observation has inconsistent C1 pass validation: {matched_methods}")
            if (task, checkpoint) not in target_order:
                raise ValueError(f"target checkpoint is absent from locked order: {task}/{checkpoint}")
            candidate_rows.append({
                "observation": observation,
                "methods": matched_methods,
                "test_path": source_candidates[0][1],
                "source_validation": source_validation,
                "test_sha256": next(iter(source_hashes)),
            })
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            preflight_issues.append(f"{task}/{checkpoint}/{nodeid}: {error}")

    if preflight_issues:
        raise RuntimeError("adjudication source preflight failed:\n" + "\n".join(preflight_issues[:40]))
    if not candidate_rows:
        raise ValueError("no reproducible candidate observations matched the selected methods")

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidate_rows:
        observation = candidate["observation"]
        groups[(str(observation["task_id"]), str(observation["test_nodeid"]), candidate["test_sha256"])].append(candidate)

    anchors: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for candidates in groups.values():
        ordered = sorted(
            candidates,
            key=lambda row: (
                target_order[(str(row["observation"]["task_id"]), str(row["observation"]["target_checkpoint"]))],
                str(row["observation"]["observation_id"]),
            ),
        )
        anchors.append((ordered[0], ordered))
    anchors.sort(
        key=lambda pair: (
            task_order[str(pair[0]["observation"]["task_id"])],
            target_order[(str(pair[0]["observation"]["task_id"]), str(pair[0]["observation"]["target_checkpoint"]))],
            str(pair[0]["observation"]["test_nodeid"]),
        )
    )

    cases_dir = output_dir / "cases"
    Path(filesystem_path(cases_dir)).mkdir(parents=True)
    mapping: list[dict[str, Any]] = []
    stable_failure_case_count = 0
    stable_error_case_count = 0
    for index, (anchor, group) in enumerate(anchors, 1):
        observation = anchor["observation"]
        initial_result, repeats, evidence = result_evidence(evaluation_dir, observation)
        case_id = f"case-{index:04d}"
        status = str(observation["reproducibility"]["status"])
        stable_failure_case_count += status == "stable_failure"
        stable_error_case_count += status == "stable_error"
        case = {
            "case_id": case_id,
            "task_id": observation["task_id"],
            "trajectory_id": observation["trajectory_id"],
            "source_checkpoint": observation["source_checkpoint"],
            "target_checkpoint": observation["target_checkpoint"],
            "test_nodeid": observation["test_nodeid"],
            "frozen_test_source": read_text(anchor["test_path"]),
            "frozen_test_sha256": anchor["test_sha256"],
            "source_checkpoint_validation": anchor["source_validation"],
            "target_observation_class": status,
            "source_specification": evidence["source_specification"],
            "target_specification": evidence["target_specification"],
            "workspace_diff": evidence["workspace_diff"],
            "checkpoint_evidence": evidence["checkpoint_evidence"],
            "source_agent_run_manifest": evidence["source_agent_run_manifest"],
            "target_agent_run_manifest": evidence["target_agent_run_manifest"],
            "initial_target_result": initial_result,
            "repeat_runs": repeats,
        }
        case_path = cases_dir / f"{case_id}.json"
        write_text(case_path, json.dumps(case, ensure_ascii=False, indent=2) + "\n")
        group_methods = sorted({method for candidate in group for method in candidate["methods"]})
        mapping.append({
            "case_id": case_id,
            "case_sha256": sha256_file(case_path),
            "anchor_observation_id": observation["observation_id"],
            "task_id": observation["task_id"],
            "test_nodeid": observation["test_nodeid"],
            "frozen_test_sha256": anchor["test_sha256"],
            "anchor_target_checkpoint": observation["target_checkpoint"],
            "target_observation_class": status,
            "methods_detected": group_methods,
            "merged_observations": [
                {
                    "observation_id": candidate["observation"]["observation_id"],
                    "target_checkpoint": candidate["observation"]["target_checkpoint"],
                    "methods_detected": list(candidate["methods"]),
                    "target_observation_class": candidate["observation"]["reproducibility"]["status"],
                }
                for candidate in group
            ],
        })

    write_text(output_dir / "private_case_mapping.json", json.dumps(mapping, ensure_ascii=False, indent=2) + "\n")
    summary = {
        "format": "rq1-common-blinded-adjudication-input/v1",
        "evaluation_dir": str(evaluation_dir.relative_to(ROOT)),
        "selected_methods": list(methods),
        "vanilla_suite": {"root": str(vanilla_root.relative_to(ROOT)), "suffix": vanilla_suffix},
        "guided_suite": {"root": str(guided_root.relative_to(ROOT)), "suffix": guided_suffix},
        "source_observation_count": len(observations),
        "selected_reproducible_observation_count": len(candidate_rows),
        "stable_failure_case_count": stable_failure_case_count,
        "stable_error_case_count": stable_error_case_count,
        "case_count": len(mapping),
        "excluded_not_selected_method_count": skipped_not_selected,
        "excluded_not_reproducible_or_diagnostic_count": skipped_not_reproducible,
        "deduplication_rule": "same task_id + test_nodeid + frozen_test_sha256; retain the earliest locked target checkpoint",
        "method_labels_in_cases": False,
        "all_cases_include_c1_test_source_and_validation": True,
        "all_cases_include_target_failure_traces_and_repeats": True,
        "cases_directory": "cases",
        "private_mapping_path": "private_case_mapping.json",
    }
    write_text(output_dir / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

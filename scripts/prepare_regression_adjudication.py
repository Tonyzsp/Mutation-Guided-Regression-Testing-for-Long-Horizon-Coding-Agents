"""Create method-blinded, evidence-complete cases for the fixed RQ1 judge."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "artifacts" / "results" / "rq1_full_36x1" / "c1_base_audit_current_sanitized_20261116" / "c1_base_suite_audit.json"
RESULT_ROOT = ROOT / "artifacts" / "results" / "rq1_full_36x1"


def filesystem_path(path: Path) -> str:
    value = os.path.abspath(os.fspath(path))
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        return "\\\\?\\" + value
    return value


def read_text(path: Path, *, errors: str = "strict") -> str:
    with open(filesystem_path(path), "r", encoding="utf-8", errors=errors) as handle:
        return handle.read()


def write_text(path: Path, value: str) -> None:
    with open(filesystem_path(path), "w", encoding="utf-8", newline="") as handle:
        handle.write(value)


def is_file(path: Path) -> bool:
    return os.path.isfile(filesystem_path(path))


def is_dir(path: Path) -> bool:
    return os.path.isdir(filesystem_path(path))


def read_json(path: Path) -> Any:
    return json.loads(read_text(path))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in read_text(path).splitlines() if line.strip()]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(filesystem_path(path), "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_child(root: Path, relative: str) -> Path:
    root_abs = Path(os.path.abspath(os.fspath(root)))
    path = Path(os.path.abspath(os.fspath(root_abs / relative)))
    if not path.is_relative_to(root_abs):
        raise ValueError(f"evidence path escaped its root: {relative}")
    if not os.path.exists(filesystem_path(path)):
        raise FileNotFoundError(path)
    root_real = os.path.realpath(filesystem_path(root_abs))
    path_real = os.path.realpath(filesystem_path(path))
    if os.name == "nt":
        root_real = root_real.removeprefix("\\\\?\\")
        path_real = path_real.removeprefix("\\\\?\\")
    if not Path(path_real).is_relative_to(Path(root_real)):
        raise ValueError(f"evidence path resolves outside its root: {relative}")
    return path


def limited_text(value: Any, limit: int = 16_000) -> str:
    text = value if isinstance(value, str) else ""
    if len(text) > limit:
        return text[:limit] + "\n[truncated; complete evidence remains in the evaluation artifact]"
    return text


def report_test(report_path: Path | None, nodeid: str) -> dict[str, Any] | None:
    if report_path is None or not is_file(report_path):
        return None
    report = read_json(report_path)
    item = next((row for row in report.get("tests", []) if row.get("nodeid") == nodeid), None)
    if item is None:
        return None
    phases: dict[str, Any] = {}
    for phase_name in ("setup", "call", "teardown"):
        phase = item.get(phase_name)
        if not isinstance(phase, dict):
            continue
        crash = phase.get("crash") if isinstance(phase.get("crash"), dict) else {}
        phases[phase_name] = {
            "outcome": phase.get("outcome"),
            "crash_message": limited_text(crash.get("message"), 8_000),
            "traceback": phase.get("traceback", []),
            "longrepr": limited_text(phase.get("longrepr"), 12_000),
        }
    return {
        "nodeid": nodeid,
        "outcome": item.get("outcome"),
        "phases": phases,
        "longrepr": limited_text(item.get("longrepr"), 16_000),
    }


def resolve_test_source(task: str, nodeid: str, suffix: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Resolve the exact C1 frozen test and its method-neutral C1 pass evidence."""
    source_rel = nodeid.split("::", 1)[0].replace("\\", "/")
    source_parts = PurePosixPath(source_rel).parts
    if not source_parts or source_parts[0] != "base_tests" or ".." in source_parts:
        raise ValueError(f"unexpected frozen test path in node ID: {nodeid}")
    relative_test = Path(*source_parts[1:])

    generation_dir = Path(os.path.abspath(RESULT_ROOT / task / "C1" / suffix))
    if not is_dir(generation_dir):
        raise FileNotFoundError(generation_dir)
    frozen = read_json(generation_dir / "frozen_test_suite.json")
    suite_relative = PurePosixPath(frozen["suite_path"])
    if suite_relative.is_absolute() or ".." in suite_relative.parts or "\\" in frozen["suite_path"]:
        raise ValueError(f"unsafe frozen suite path for {task}")
    suite_root = Path(os.path.abspath(generation_dir.joinpath(*suite_relative.parts)))
    if not suite_root.is_relative_to(generation_dir):
        raise ValueError(f"frozen suite escaped its generation directory for {task}")
    if not is_dir(suite_root):
        raise FileNotFoundError(suite_root)

    candidates: list[Path] = []
    base_candidate = suite_root / "base_tests" / relative_test
    if is_file(base_candidate):
        candidates.append(base_candidate)
    for generated in frozen.get("generated_test_paths", []):
        path = safe_child(generation_dir, generated)
        if path.name == relative_test.name:
            candidates.append(path)
    unique_candidates = list(dict.fromkeys(candidates))
    if len(unique_candidates) != 1:
        raise ValueError(f"expected exactly one frozen test source for {task}/{nodeid}; found {len(unique_candidates)}")
    test_path = unique_candidates[0]

    baseline = read_json(BASELINE_PATH)
    baseline_record = next((row for row in baseline["records"] if row.get("task_id") == task), None)
    if baseline_record is None:
        raise ValueError(f"missing C1 baseline record for {task}")
    stable_pass_ids = set(baseline_record.get("stable_pass_test_ids", []))
    if nodeid in stable_pass_ids:
        pass_runs = []
        for repetition in baseline_record.get("repetitions", []):
            outcomes = [test.get("outcome") for test in repetition.get("tests", []) if test.get("nodeid") == nodeid]
            if len(outcomes) != 1:
                raise ValueError(f"C1 baseline repetition lacks a unique outcome for {task}/{nodeid}")
            pass_runs.append({"repetition": repetition.get("repetition"), "outcome": outcomes[0]})
        source_validation = {
            "checkpoint": "C1",
            "status": "stable_pass",
            "repeat_count": len(pass_runs),
            "runs": pass_runs,
        }
    else:
        validation_path = safe_child(generation_dir, frozen["validation_log_path"])
        validation_rows = read_jsonl(validation_path)
        retained = [
            row for row in validation_rows
            if row.get("status") == "retained" and nodeid in row.get("collection_ids", [])
        ]
        if len(retained) != 1:
            raise ValueError(f"missing unique retained C1 validation for {task}/{nodeid}")
        record = retained[0]
        pass_runs = [
            {"repetition": run.get("repetition"), "outcome": "passed" if run.get("passed") is True else "failed"}
            for run in record.get("runs", [])
        ]
        source_validation = {
            "checkpoint": "C1",
            "status": "stable_pass" if pass_runs and all(run["outcome"] == "passed" for run in pass_runs) else "not_stable_pass",
            "repeat_count": len(pass_runs),
            "runs": pass_runs,
        }
    if source_validation["status"] != "stable_pass" or source_validation["repeat_count"] != 3:
        raise ValueError(f"C1 pass validation is incomplete for {task}/{nodeid}: {source_validation}")
    return test_path, source_validation, frozen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation-dir", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    evaluation_dir = args.evaluation_dir.resolve(strict=True)
    allowed_root = RESULT_ROOT.resolve(strict=True)
    if not evaluation_dir.is_relative_to(allowed_root):
        raise ValueError("evaluation directory must remain under the RQ1 result root")
    output_dir = Path(os.path.abspath(args.output_dir)) if args.output_dir else evaluation_dir / "blinded_adjudication"
    if not output_dir.is_relative_to(evaluation_dir) or os.path.exists(filesystem_path(output_dir)):
        raise ValueError("adjudication output must be a new directory inside the evaluation directory")

    summary_input = read_json(evaluation_dir / "evaluation_summary.json")
    suffix = summary_input.get("guided_suite_suffix")
    if not isinstance(suffix, str) or not suffix:
        raise ValueError("evaluation summary has no frozen Guided suite suffix")
    observations = read_jsonl(evaluation_dir / "candidate_failure_observations.jsonl")
    reproducible = []
    for row in observations:
        repro = row.get("reproducibility", {})
        status = repro.get("status")
        if status == "stable_failure" and repro.get("repeat_count") == 3 and repro.get("repeated_failure_count") == 3:
            reproducible.append(row)
        elif status == "stable_error" and repro.get("repeat_count") == 3 and repro.get("repeated_error_count") == 3:
            reproducible.append(row)

    prepared: list[tuple[dict[str, Any], dict[str, Any], Path]] = []
    preflight_issues: list[str] = []
    for index, observation in enumerate(reproducible, 1):
        task = observation["task_id"]
        nodeid = observation["test_nodeid"]
        try:
            test_path, source_validation, _frozen = resolve_test_source(task, nodeid, suffix)
            failure_dir = safe_child(ROOT, observation["evidence_run_directory"])
            initial_result = report_test(failure_dir / "pytest-report.json", nodeid)
            if initial_result is None:
                raise ValueError("initial pytest report has no matching test outcome")
            repeat_summaries = []
            for run in observation["reproducibility"]["runs"]:
                report_rel = run.get("pytest_report_path")
                report_path = safe_child(failure_dir, report_rel) if report_rel else None
                repeated_result = report_test(report_path, nodeid)
                if repeated_result is None:
                    raise ValueError(f"repeat {run.get('repetition')} has no matching pytest report result")
                repeat_summaries.append({
                    "repetition": run["repetition"],
                    "run_status": run["run_status"],
                    "exit_code": run["exit_code"],
                    "test_result": repeated_result,
                })

            source_spec = safe_child(evaluation_dir, observation["source_specification_path"])
            target_spec = safe_child(evaluation_dir, observation["target_specification_path"])
            diff = safe_child(evaluation_dir, observation["workspace_diff_path"])
            evidence_manifest = safe_child(evaluation_dir, observation["evidence_manifest_path"])
            source_agent_manifest = safe_child(evaluation_dir, observation["source_agent_run_manifest_path"]) if observation.get("source_agent_run_manifest_path") else None
            target_agent_manifest = safe_child(evaluation_dir, observation["target_agent_run_manifest_path"]) if observation.get("target_agent_run_manifest_path") else None

            case_id = f"case-{index:04d}"
            case = {
                "case_id": case_id,
                "task_id": task,
                "trajectory_id": observation["trajectory_id"],
                "source_checkpoint": observation["source_checkpoint"],
                "target_checkpoint": observation["target_checkpoint"],
                "test_nodeid": nodeid,
                "frozen_test_source": read_text(test_path),
                "frozen_test_sha256": sha256_file(test_path),
                "source_checkpoint_validation": source_validation,
                "target_observation_class": observation["reproducibility"]["status"],
                "source_specification": read_text(source_spec),
                "target_specification": read_text(target_spec),
                "workspace_diff": read_text(diff, errors="replace"),
                "checkpoint_evidence": read_json(evidence_manifest),
                "source_agent_run_manifest": read_json(source_agent_manifest) if source_agent_manifest else None,
                "target_agent_run_manifest": read_json(target_agent_manifest) if target_agent_manifest else None,
                "initial_target_result": initial_result,
                "repeat_runs": repeat_summaries,
            }
            prepared.append((observation, case, failure_dir))
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            preflight_issues.append(f"{task}/{observation.get('target_checkpoint')}/{nodeid}: {error}")

    if preflight_issues:
        raise RuntimeError("adjudication evidence preflight failed:\n" + "\n".join(preflight_issues[:40]))
    if not prepared:
        raise ValueError("no reproducible candidate observations met the frozen 3/3 rule")

    cases_dir = output_dir / "cases"
    Path(filesystem_path(cases_dir)).mkdir(parents=True)
    mapping: list[dict[str, Any]] = []
    stable_failure_count = 0
    stable_error_count = 0
    for observation, case, _failure_dir in prepared:
        case_id = case["case_id"]
        observation_class = case["target_observation_class"]
        stable_failure_count += observation_class == "stable_failure"
        stable_error_count += observation_class == "stable_error"
        case_path = cases_dir / f"{case_id}.json"
        write_text(case_path, json.dumps(case, ensure_ascii=False, indent=2) + "\n")
        mapping.append({
            "case_id": case_id,
            "observation_id": observation["observation_id"],
            "task_id": observation["task_id"],
            "target_checkpoint": observation["target_checkpoint"],
            "test_nodeid": observation["test_nodeid"],
            "target_observation_class": observation_class,
            "methods_detected": observation.get("methods_detected", []),
            "suite_sha256": observation.get("suite_sha256"),
            "case_sha256": sha256_file(case_path),
        })
    write_text(output_dir / "private_case_mapping.json", json.dumps(mapping, ensure_ascii=False, indent=2) + "\n")
    summary = {
        "format": "rq1-blinded-adjudication-input/v1",
        "evaluation_dir": str(evaluation_dir.relative_to(ROOT)),
        "source_observation_count": len(observations),
        "stable_failure_case_count": stable_failure_count,
        "stable_error_diagnostic_case_count": stable_error_count,
        "case_count": len(prepared),
        "excluded_nonreproducible_observation_count": len(observations) - len(prepared),
        "method_labels_in_cases": False,
        "all_cases_include_c1_test_source_and_validation": True,
        "all_cases_include_target_failure_traces_and_repeats": True,
        "cases_directory": "cases",
        "private_mapping_path": "private_case_mapping.json",
    }
    write_text(output_dir / "summary.json", json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Build immutable all-task MG suites from v1 and accepted v2 tests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_rq1_later_checkpoint_evaluation as evaluation  # noqa: E402
from validate_checkpoint_package import sha256_tree as package_tree_hash  # noqa: E402
from validate_and_freeze_tests import hash_tree as frozen_suite_hash  # noqa: E402

V1_ROOT = ROOT / "artifacts" / "results" / "rq1_full_36x1"
V2_ROOT = ROOT / "artifacts" / "results" / "rq1_recovery_v2"
PACKAGE_ROOT = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
UNIT_PATH = ROOT / "config" / "rq1_full_36x1_candidate_units.json"
BASELINE_PATH = V1_ROOT / "c1_base_audit_current_sanitized_20261116" / "c1_base_suite_audit.json"
RECONCILIATION_PATH = V2_ROOT / "mg_target_kill_reconciliation_summary.json"
V1_SUFFIX = evaluation.DEFAULT_GUIDED
COMPOSITE_SUFFIX = "mutation_guided_composite_v2"


def read_json(path: Path) -> Any:
    with open(evaluation.filesystem_path(path), "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(evaluation.filesystem_path(path), "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_test_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or not value.endswith(".py"):
        raise ValueError(f"unsafe generated test path: {value!r}")
    if not path.name.startswith("test_"):
        raise ValueError(f"generated test file must start with test_: {value!r}")
    return path


def remap_ids(ids: list[str], filename: str) -> list[str]:
    mapped: list[str] = []
    for nodeid in ids:
        file_part, separator, suffix = str(nodeid).partition("::")
        if not separator:
            raise ValueError(f"malformed collected pytest node ID: {nodeid!r}")
        if not file_part.startswith("base_tests/"):
            raise ValueError(f"collected test is outside base_tests: {nodeid!r}")
        mapped.append(f"base_tests/{filename}::{suffix}")
    return mapped


def v1_tests(task: str) -> list[dict[str, Any]]:
    generation_dir = V1_ROOT / task / "C1" / V1_SUFFIX
    manifest_path = generation_dir / "frozen_test_suite.json"
    frozen = read_json(manifest_path)
    if frozen.get("task_id") != task or frozen.get("method") != "mutation_guided":
        raise ValueError(f"{task}: v1 MG frozen suite identity mismatch")
    suite_root = generation_dir / frozen["suite_path"]
    if frozen_suite_hash(suite_root) != frozen.get("suite_sha256"):
        raise ValueError(f"{task}: v1 MG frozen suite hash mismatch")
    validation = evaluation.read_jsonl(generation_dir / frozen["validation_log_path"])
    retained = [row for row in validation if row.get("status") == "retained"]
    expected_paths = {safe_test_path(value).name for value in frozen.get("generated_test_paths", [])}
    by_name = {str(row.get("candidate_path", "")): row for row in retained}
    if expected_paths != set(by_name):
        raise ValueError(f"{task}: v1 retained files differ from its validation log")
    response = read_json(generation_dir / "candidate_response.json")
    candidates = {row["relative_path"]: row for row in response.get("tests", [])}
    result: list[dict[str, Any]] = []
    for path_value in frozen.get("generated_test_paths", []):
        relative = safe_test_path(path_value)
        row = by_name[relative.name]
        candidate = candidates.get(relative.name)
        if candidate is None:
            raise ValueError(f"{task}: retained v1 candidate source is absent from candidate_response.json")
        content = str(candidate["content"])
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if content_hash != row.get("content_sha256"):
            raise ValueError(f"{task}: v1 retained candidate hash does not match its validation record")
        if row.get("correctness_review", {}).get("verdict") != "correct":
            raise ValueError(f"{task}: v1 retained candidate lacks a correct specification review")
        if not any(check.get("killed") is True for check in row.get("target_mutant_results", [])):
            raise ValueError(f"{task}: v1 retained candidate lacks a recorded target-mutant kill")
        result.append({
            "version": "v1",
            "source": str((generation_dir / "candidate_response.json").relative_to(ROOT).as_posix()),
            "source_sha256": content_hash,
            "content": content,
            "source_path": relative.as_posix(),
            "source_candidate_path": relative.name,
            "collection_ids": list(row.get("collection_ids", [])),
            "validation_record": row,
        })
    return result


def v2_tests(reconciliation: dict[str, Any], task: str) -> list[dict[str, Any]]:
    task_records = [row for row in reconciliation["records"] if row["task_id"] == task]
    result: list[dict[str, Any]] = []
    for slot_record in task_records:
        slot = int(slot_record["call_slot"].rsplit("_", 1)[-1])
        generation_dir = V2_ROOT / task / "C1" / "mutation_guided" / f"call_{slot:02d}"
        if not (generation_dir / "candidate_response.json").is_file():
            generation_dir = generation_dir / "model_invocation_after_preflight_fix"
        response = read_json(generation_dir / "candidate_response.json")
        response_by_path = {row["relative_path"]: row for row in response["tests"]}
        validation = evaluation.read_jsonl(generation_dir / "validation.jsonl")
        validation_by_path = {row["candidate_path"]: row for row in validation}
        review = read_json(generation_dir / "correctness_review.json")
        review_by_path = {row["candidate_path"]: row for row in review.get("reviews", [])}
        for decision in slot_record.get("records", []):
            if not decision["decision"].startswith("retained"):
                continue
            candidate_path = decision["candidate_path"]
            candidate = response_by_path[candidate_path]
            row = validation_by_path[candidate_path]
            if hashlib.sha256(candidate["content"].encode("utf-8")).hexdigest() != decision["candidate_content_sha256"]:
                raise ValueError(f"{task}/{slot_record['call_slot']}/{candidate_path}: candidate hash changed")
            if review_by_path.get(candidate_path, {}).get("verdict") != "correct":
                raise ValueError(f"{task}/{slot_record['call_slot']}/{candidate_path}: correctness review is not correct")
            if row.get("status") == "retained":
                pass
            elif decision["decision"] == "retained_at_least_one_target_killed":
                stable_kill = any(
                    check.get("killed") is True
                    and check.get("repeat_runs") == 3
                    and len(check.get("repetition_results", [])) == 3
                    and all(run.get("status") == "killed" for run in check["repetition_results"])
                    for check in decision.get("target_checks", [])
                )
                if not stable_kill:
                    raise ValueError(f"{task}/{slot_record['call_slot']}/{candidate_path}: v2 at-least-one kill evidence missing")
            elif decision["decision"] == "retained_direct_diff_fallback":
                evidence_path = ROOT / decision["direct_diff_record_path"]
                evidence = read_json(evidence_path)
                if evidence.get("candidate_content_sha256") != decision["candidate_content_sha256"]:
                    raise ValueError(f"{task}/{slot_record['call_slot']}/{candidate_path}: direct-diff evidence hash mismatch")
                if not any(
                    target.get("status") == "killed"
                    and len(target.get("runs", [])) == 3
                    and all(run.get("status") == "killed" for run in target["runs"])
                    for target in evidence.get("target_checks", [])
                ):
                    raise ValueError(f"{task}/{slot_record['call_slot']}/{candidate_path}: exact-diff kill evidence missing")
            else:
                raise ValueError(f"{task}/{slot_record['call_slot']}/{candidate_path}: unknown retention decision")
            if not row.get("collection_ids"):
                raise ValueError(f"{task}/{slot_record['call_slot']}/{candidate_path}: no validated collection IDs")
            result.append({
                "version": "v2",
                "source": str((generation_dir / "candidate_response.json").relative_to(ROOT).as_posix()),
                "source_sha256": decision["candidate_content_sha256"],
                "content": candidate["content"],
                "source_path": candidate_path,
                "source_candidate_path": candidate_path,
                "call_slot": slot_record["call_slot"],
                "decision": decision["decision"],
                "decision_record": decision,
                "collection_ids": list(row["collection_ids"]),
                "validation_record": row,
            })
    return result


def unique_filename(test: dict[str, Any], used: set[str], index: int) -> str:
    original = safe_test_path(test["source_candidate_path"]).name
    if original.casefold() not in used:
        used.add(original.casefold())
        return original
    stem = original[:-3]
    prefix = "test_mg_v1_" if test["version"] == "v1" else f"test_mg_v2c{int(test['call_slot'].rsplit('_', 1)[-1]):02d}_"
    filename = f"{prefix}{stem[5:]}_{index:02d}.py"
    if filename.casefold() in used:
        raise ValueError(f"could not make a unique generated test filename: {filename}")
    used.add(filename.casefold())
    return filename


def build_task(task: str, baseline_by_task: dict[str, Any], reconciliation: dict[str, Any]) -> dict[str, Any]:
    package_root = (PACKAGE_ROOT / task / "C1").resolve(strict=True)
    package = read_json(package_root / "package.json")
    baseline_record = baseline_by_task[task]
    base_source = evaluation.package_file(package, package_root, "base_tests_path")
    if package_tree_hash(base_source) != package.get("base_tests_sha256"):
        raise ValueError(f"{task}: sanitized package base tests hash mismatch")

    v1_manifest_path = V1_ROOT / task / "C1" / V1_SUFFIX / "frozen_test_suite.json"
    v1_manifest = read_json(v1_manifest_path)
    tests = v1_tests(task) + v2_tests(reconciliation, task)
    output_dir = V2_ROOT / task / "C1" / COMPOSITE_SUFFIX
    suite_root = output_dir / "frozen_suite"
    if output_dir.exists():
        build_manifest_path = output_dir / "composite_build_manifest.json"
        if build_manifest_path.is_file():
            prior = read_json(build_manifest_path)
            expected_inputs = {
                "task_id": task,
                "v1_frozen_suite_sha256": v1_manifest["suite_sha256"],
                "v2_reconciliation_summary_sha256": sha256_file(RECONCILIATION_PATH),
                "baseline_audit_sha256": sha256_file(BASELINE_PATH),
                "package_json_sha256": sha256_file(package_root / "package.json"),
                "base_tests_sha256": package["base_tests_sha256"],
            }
            if any(prior.get(key) != value for key, value in expected_inputs.items()):
                raise FileExistsError(f"existing composite suite has different inputs: {output_dir}")
            if frozen_suite_hash(suite_root) != prior.get("composite_suite_sha256"):
                raise ValueError(f"existing composite suite content hash mismatch: {output_dir}")
            frozen_existing = read_json(output_dir / "frozen_test_suite.json")
            provenance_existing = read_json(output_dir / "composite_provenance.json")
            if (
                frozen_existing.get("suite_sha256") != prior.get("composite_suite_sha256")
                or len(frozen_existing.get("generated_test_paths", [])) != len(tests)
                or len(provenance_existing.get("tests", [])) != len(tests)
                or prior.get("generated_test_count") != len(tests)
            ):
                raise ValueError(f"existing composite suite manifest differs from current accepted tests: {output_dir}")
            return {
                "task_id": task,
                "v1_test_count": prior.get("v1_generated_test_count", 0),
                "v2_test_count": prior.get("v2_generated_test_count", 0),
                "generated_test_count": prior.get("generated_test_count", 0),
                "suite_sha256": prior["composite_suite_sha256"],
                "composite_dir": str(output_dir.relative_to(ROOT).as_posix()),
                "baseline_record_status": baseline_record.get("status"),
            }
        final_metadata = [
            output_dir / "frozen_test_suite.json",
            output_dir / "composite_provenance.json",
            output_dir / "validation.jsonl",
        ]
        if any(path.exists() for path in final_metadata):
            raise FileExistsError(f"composite output contains unmanifested artifacts: {output_dir}")
        allowed_partial = suite_root / "base_tests"
        for item in output_dir.rglob("*"):
            if item.is_file() and not item.is_relative_to(allowed_partial):
                raise FileExistsError(f"composite output contains unexpected partial files: {item}")
    base_destination = suite_root / "base_tests"
    generated_destination = suite_root / "generated_tests"
    base_destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        evaluation.filesystem_path(base_source),
        evaluation.filesystem_path(base_destination),
        dirs_exist_ok=True,
    )
    if package_tree_hash(base_destination) != package.get("base_tests_sha256"):
        raise ValueError(f"{task}: composite copy differs from sanitized package base tests")

    used = {path.name.casefold() for path in base_destination.rglob("*.py")}
    generated_paths: list[str] = []
    validation_rows: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    for index, test in enumerate(tests, 1):
        filename = unique_filename(test, used, index)
        target = generated_destination / filename
        os.makedirs(evaluation.filesystem_path(target.parent), exist_ok=True)
        with open(evaluation.filesystem_path(target), "w", encoding="utf-8", newline="") as handle:
            handle.write(test["content"])
        relative = target.relative_to(output_dir).as_posix()
        generated_paths.append(relative)
        row = dict(test["validation_record"])
        row["candidate_path"] = filename
        row["collection_ids"] = remap_ids(test["collection_ids"], filename)
        row["status"] = "retained"
        row["composite_source_version"] = test["version"]
        row["composite_source_path"] = test["source_path"]
        if test["version"] == "v2":
            row["recovery_v2_call_slot"] = test["call_slot"]
            row["recovery_v2_retention_decision"] = test["decision"]
        validation_rows.append(row)
        provenance.append({
            "filename": filename,
            "version": test["version"],
            "source": test["source"],
            "source_path": test["source_path"],
            "source_sha256": test["source_sha256"],
            "composite_sha256": sha256_file(target),
            "collection_ids": row["collection_ids"],
            "call_slot": test.get("call_slot"),
            "retention_decision": test.get("decision", "v1_frozen"),
        })

    suite_hash = frozen_suite_hash(suite_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    validation_path = output_dir / "validation.jsonl"
    evaluation.write_jsonl(validation_path, validation_rows)
    metadata = dict(v1_manifest.get("metadata", {}))
    metadata.update({
        "recovery_v2_composite": True,
        "v1_frozen_suite_path": str(v1_manifest_path.relative_to(ROOT).as_posix()),
        "v1_frozen_suite_sha256": v1_manifest["suite_sha256"],
        "v2_reconciliation_summary_path": str(RECONCILIATION_PATH.relative_to(ROOT).as_posix()),
        "v2_reconciliation_summary_sha256": sha256_file(RECONCILIATION_PATH),
        "baseline_audit_path": str(BASELINE_PATH.relative_to(ROOT).as_posix()),
        "baseline_audit_sha256": sha256_file(BASELINE_PATH),
        "runtime_manifest_path": v1_manifest["metadata"]["runtime_manifest_path"],
        "composite_test_count": len(tests),
        "v1_test_count": sum(item["version"] == "v1" for item in tests),
        "v2_test_count": sum(item["version"] == "v2" for item in tests),
    })
    frozen = {
        "schema_version": "1.0",
        "task_id": task,
        "trajectory_id": package["trajectory_id"],
        "source_checkpoint": "C1",
        "method": "mutation_guided",
        "suite_path": "frozen_suite",
        "base_suite_path": "frozen_suite/base_tests",
        "generated_test_paths": generated_paths,
        "validation_log_path": "validation.jsonl",
        "suite_sha256": suite_hash,
        "model_config_id": "rq1-recovery-v2-mg-composite",
        "budget_id": "rq1-recovery-v2-mg-only-composite-of-frozen-v1-and-v2",
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "metadata": metadata,
    }
    write_json(output_dir / "frozen_test_suite.json", frozen)
    write_json(output_dir / "composite_provenance.json", {
        "format": "rq1-recovery-v2-mg-composite-provenance/v1",
        "task_id": task,
        "base_tests_sha256": package["base_tests_sha256"],
        "suite_sha256": suite_hash,
        "reconciliation_sha256": sha256_file(RECONCILIATION_PATH),
        "tests": provenance,
    })
    write_json(output_dir / "composite_build_manifest.json", {
        "format": "rq1-recovery-v2-mg-composite-build/v1",
        "task_id": task,
        "v1_frozen_suite_sha256": v1_manifest["suite_sha256"],
        "v2_reconciliation_summary_sha256": sha256_file(RECONCILIATION_PATH),
        "baseline_audit_sha256": sha256_file(BASELINE_PATH),
        "package_json_sha256": sha256_file(package_root / "package.json"),
        "base_tests_sha256": package["base_tests_sha256"],
        "composite_suite_sha256": suite_hash,
        "generated_test_count": len(tests),
        "v1_generated_test_count": sum(item["version"] == "v1" for item in tests),
        "v2_generated_test_count": sum(item["version"] == "v2" for item in tests),
    })
    return {
        "task_id": task,
        "v1_test_count": sum(item["version"] == "v1" for item in tests),
        "v2_test_count": sum(item["version"] == "v2" for item in tests),
        "generated_test_count": len(tests),
        "suite_sha256": suite_hash,
        "composite_dir": str(output_dir.relative_to(ROOT).as_posix()),
        "baseline_record_status": baseline_record.get("status"),
    }


def main() -> int:
    baseline = read_json(BASELINE_PATH)
    if baseline.get("status") != "complete" or len(baseline.get("records", [])) != 36:
        raise ValueError("the sanitized 36-task baseline audit is incomplete")
    reconciliation = read_json(RECONCILIATION_PATH)
    if reconciliation.get("candidate_count") != 49 or reconciliation.get("slots_reconciled") != 31:
        raise ValueError("MG recovery target reconciliation is incomplete")
    if sum(reconciliation.get("decision_counts", {}).get(key, 0) for key in (
        "retained_strict_validation", "retained_at_least_one_target_killed", "retained_direct_diff_fallback"
    )) != reconciliation.get("retained_count"):
        raise ValueError("MG recovery retention decisions do not sum to the retained count")
    baseline_by_task = {row["task_id"]: row for row in baseline["records"]}
    units = read_json(UNIT_PATH)["units"]
    if len(units) != 36 or len(baseline_by_task) != 36:
        raise ValueError("the candidate cohort is not exactly 36 tasks")
    records = [build_task(unit["task_id"], baseline_by_task, reconciliation) for unit in units]
    summary = {
        "format": "rq1-recovery-v2-mg-composite-suite-summary/v1",
        "task_count": len(records),
        "v1_generated_test_count": sum(row["v1_test_count"] for row in records),
        "v2_generated_test_count": sum(row["v2_test_count"] for row in records),
        "composite_generated_test_count": sum(row["generated_test_count"] for row in records),
        "tasks_with_v1_tests": sum(row["v1_test_count"] > 0 for row in records),
        "tasks_with_v2_tests": sum(row["v2_test_count"] > 0 for row in records),
        "tasks_with_any_mg_tests": sum(row["generated_test_count"] > 0 for row in records),
        "records": records,
    }
    write_json(V2_ROOT / "mg_composite_suite_summary.json", summary)
    print(json.dumps({key: summary[key] for key in (
        "task_count", "v1_generated_test_count", "v2_generated_test_count",
        "composite_generated_test_count", "tasks_with_v1_tests", "tasks_with_v2_tests",
        "tasks_with_any_mg_tests",
    )}, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

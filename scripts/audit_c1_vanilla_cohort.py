"""Integrity audit for frozen Vanilla test suites in the RQ1 C1 cohort."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from validate_and_freeze_tests import filesystem_path, hash_tree, read_json, sha256_file


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
RESULT_ROOT = ROOT / "artifacts" / "results" / "rq1_full_36x1"
COHORT_NAMES = (
    "vanilla_formal_v5_20261116",
    "vanilla_formal_v4_llm_review_20261114",
    "vanilla_formal_v4_20261019",
    "vanilla_iso_v3_20261019",
    "vanilla_cohort_gpt6luna_medium_isolation_revalidated_v3_20261019",
    "vanilla_cohort_gpt6luna_medium_revalidated_20261019",
    "vanilla_cohort_gpt6luna_medium_20261019",
    "vanilla_cohort_gpt6luna_medium_20261018",
)
OLD_BASE_AUDIT = RESULT_ROOT / "c1_base_audit_final_20261018" / "c1_base_suite_audit.json"
FINAL_BASE_AUDIT = RESULT_ROOT / "c1_base_audit_current_sanitized_20261116" / "c1_base_suite_audit.json"
STUDY_BUDGET = ROOT / "config" / "rq1_full_study_budget_v1.json"
OUTPUT = RESULT_ROOT / "c1_vanilla_cohort_audit_formal_v1_20261117.json"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSONL at {path}:{line_number}") from error
    return rows


def walk_files(root: Path) -> list[tuple[str, Path]]:
    root_ext = filesystem_path(root)
    result: list[tuple[str, Path]] = []
    for current, directories, names in os.walk(root_ext):
        directories.sort()
        for name in sorted(names):
            absolute = os.path.join(current, name)
            relative = os.path.relpath(absolute, root_ext).replace(os.sep, "/")
            result.append((relative, Path(absolute)))
    return result


def package_tree_hash(root: Path) -> str:
    """Match the checkpoint-package hash format used by the exporter."""
    digest = hashlib.sha256()
    root_ext = filesystem_path(root)
    files: list[tuple[str, str]] = []
    for current, directories, names in os.walk(root_ext):
        directories.sort()
        for name in names:
            absolute = os.path.join(current, name)
            files.append((os.path.relpath(absolute, root_ext).replace(os.sep, "/"), absolute))
    for relative, absolute in sorted(files):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with open(absolute, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def audit_baseline_links(frozen: dict[str, Any], task: str, final: dict[str, Any]) -> None:
    metadata = frozen.get("metadata", {})
    actual_hash = metadata.get("baseline_audit_sha256")
    baseline_path = metadata.get("baseline_audit_path")
    path = Path(baseline_path) if baseline_path else None
    if path is None or not path.is_file() or sha256_file(path) != actual_hash:
        raise ValueError(f"{task}: frozen suite baseline audit path/hash cannot be verified")
    referenced = read_json(path)
    referenced_record = next(item for item in referenced["records"] if item["task_id"] == task and item["checkpoint_id"] == "C1")
    final_record = next(item for item in final["records"] if item["task_id"] == task and item["checkpoint_id"] == "C1")
    keys = ("base_tests_sha256", "status", "stable_pass_test_ids", "stable_fail_test_ids", "unstable_test_ids")
    if any(referenced_record.get(key) != final_record.get(key) for key in keys):
        raise ValueError(f"{task}: referenced C1 baseline record differs from the final base-suite audit")


def selected_cohort_dir(task: str) -> tuple[str, Path]:
    for cohort_name in COHORT_NAMES:
        path = RESULT_ROOT / task / "C1" / cohort_name
        if (path / "frozen_test_suite.json").is_file():
            return cohort_name, path
    raise FileNotFoundError(f"{task}: no frozen Vanilla cohort directory found")


def audit_task(task: str, final: dict[str, Any]) -> dict[str, Any]:
    cohort_name, cohort_dir = selected_cohort_dir(task)
    package_dir = PACKAGE_ROOT / task / "C1"
    generation = read_json(cohort_dir / "generation_run.json")
    inputs = read_json(cohort_dir / "input_manifest.json")
    candidates = read_json(cohort_dir / "candidate_response.json")
    frozen = read_json(cohort_dir / "frozen_test_suite.json")
    package = read_json(package_dir / "package.json")
    validation = read_jsonl(cohort_dir / "validation.jsonl")

    expected_prompt = {
        "path": "prompts/vanilla_gap_v4.md",
        "sha256": sha256_file(ROOT / "prompts" / "vanilla_gap_v4.md"),
    }
    expected_budget = {
        "path": "config/generation_budget_rq1_c1_cohort_formal.json",
        "sha256": sha256_file(ROOT / "config" / "generation_budget_rq1_c1_cohort_formal.json"),
    }
    expected_study_budget = {
        "path": "config/rq1_full_study_budget_v1.json",
        "sha256": sha256_file(STUDY_BUDGET),
    }
    if generation.get("prompt_template_version") != "c1_shared_gap_v4_cohort_v1":
        raise ValueError(f"{task}: selected Vanilla run does not record the shared v4 prompt")
    if inputs.get("prompt_template") != expected_prompt:
        raise ValueError(f"{task}: selected Vanilla run did not use the current v4 prompt template")
    if inputs.get("budget_config") != expected_budget:
        raise ValueError(f"{task}: selected Vanilla run did not use the formal budget configuration")
    if inputs.get("study_budget_config") != expected_study_budget:
        raise ValueError(f"{task}: selected Vanilla run did not bind the formal full-study budget")
    if frozen.get("metadata", {}).get("study_budget_config") != expected_study_budget:
        raise ValueError(f"{task}: frozen suite does not retain the formal full-study budget binding")
    usage = generation.get("actual_usage", {})
    budget = generation.get("budget", {})
    if int(usage.get("calls", 0)) > 3 or int(usage.get("output_tokens", 0)) > 12_288:
        raise ValueError(f"{task}: selected Vanilla generation exceeded the per-task formal budget")
    if int(generation.get("candidate_count", 0)) > 8 or int(budget.get("max_candidates", 0)) != 8:
        raise ValueError(f"{task}: selected Vanilla candidate-file budget differs from the formal protocol")

    emitted = candidates.get("tests", [])
    if len(emitted) != generation.get("candidate_count"):
        raise ValueError(f"{task}: candidate count mismatch between response and generation run")
    if len(validation) != len(emitted):
        raise ValueError(f"{task}: validation record count does not match emitted candidates")
    retained = sum(item.get("status") == "retained" for item in validation)
    if retained != generation.get("retained_count"):
        raise ValueError(f"{task}: retained count mismatch between validation and generation run")
    if frozen.get("method") != "vanilla_llm" or frozen.get("task_id") != task or frozen.get("source_checkpoint") != "C1":
        raise ValueError(f"{task}: frozen suite manifest does not identify the C1 Vanilla unit")
    if frozen.get("budget_id") != generation.get("budget_id") or frozen.get("model_config_id") != generation.get("model_config_id"):
        raise ValueError(f"{task}: frozen suite generation metadata mismatch")
    if frozen.get("metadata", {}).get("candidate_files_emitted") != len(emitted):
        raise ValueError(f"{task}: frozen manifest emitted-candidate count mismatch")
    if frozen.get("metadata", {}).get("candidate_files_retained") != retained:
        raise ValueError(f"{task}: frozen manifest retained-candidate count mismatch")

    suite_root = cohort_dir / "frozen_suite"
    if hash_tree(suite_root) != frozen.get("suite_sha256"):
        raise ValueError(f"{task}: frozen suite hash mismatch")
    package_base = package_dir / package["base_tests_path"]
    frozen_base = suite_root / "base_tests"
    base_hash = package_tree_hash(frozen_base)
    if (
        package_tree_hash(package_base) != package.get("base_tests_sha256")
        or base_hash != package.get("base_tests_sha256")
    ):
        raise ValueError(f"{task}: frozen base suite differs from the sanitized C1 package")
    expected_base_files = {
        relative: sha256_file(path)
        for relative, path in walk_files(package_base)
    }
    frozen_base_ext = filesystem_path(frozen_base)
    actual_base_files: dict[str, str] = {}
    for current, directories, names in os.walk(frozen_base_ext):
        directories.sort()
        for name in sorted(names):
            absolute = os.path.join(current, name)
            relative = os.path.relpath(absolute, frozen_base_ext).replace(os.sep, "/")
            actual_base_files[relative] = sha256_file(Path(absolute))
    if actual_base_files != expected_base_files:
        raise ValueError(f"{task}: frozen base file set differs from the sanitized C1 package")
    generator_base_files = {
        item["path"].removeprefix("base_tests/"): item["sha256"]
        for item in inputs.get("input_files", [])
        if item.get("path", "").startswith("base_tests/")
    }
    if any(expected_base_files.get(path) != digest for path, digest in generator_base_files.items()):
        raise ValueError(f"{task}: recorded Vanilla prompt includes a file that differs from the sanitized package")
    if len(generator_base_files) != len(expected_base_files):
        raise ValueError(
            f"{task}: Vanilla prompt omitted {len(expected_base_files) - len(generator_base_files)} legal base-test files"
        )
    generated_root = suite_root / "generated_tests"
    generated_ext = filesystem_path(generated_root)
    actual_generated: dict[str, str] = {}
    for current, directories, names in os.walk(generated_ext):
        directories.sort()
        for name in sorted(names):
            absolute = os.path.join(current, name)
            relative = os.path.relpath(absolute, generated_ext).replace(os.sep, "/")
            actual_generated[relative] = sha256_file(Path(absolute))
    expected_generated = {
        item["candidate_path"]: item["content_sha256"]
        for item in validation
        if item.get("status") == "retained"
    }
    declared_generated = sorted(
        item.removeprefix("frozen_suite/generated_tests/")
        for item in frozen.get("generated_test_paths", [])
    )
    if actual_generated != expected_generated or sorted(actual_generated) != declared_generated or len(actual_generated) != retained:
        raise ValueError(f"{task}: generated test files do not match retained candidates")

    if set(inputs.get("generator_visible_roots", [])) != {"workspace", "base_tests"}:
        raise ValueError(f"{task}: unexpected generator-visible roots")
    forbidden = {"hidden_tests", "future_tests", "excluded_tests", "reference", "reference_implementation"}
    visible_paths = [item.get("path", "").lower().replace("\\", "/") for item in inputs.get("input_files", [])]
    if any(forbidden.intersection(path.split("/")) for path in visible_paths):
        raise ValueError(f"{task}: generator input manifest contains a forbidden data path")
    audit_baseline_links(frozen, task, final)

    return {
        "task_id": task,
        "cohort_directory": cohort_name,
        "checkpoint_id": "C1",
        "generation_run_sha256": sha256_file(cohort_dir / "generation_run.json"),
        "input_manifest_sha256": sha256_file(cohort_dir / "input_manifest.json"),
        "source_package_sha256": inputs.get("input_package_sha256"),
        "base_tests_sha256": base_hash,
        "historical_generation_base_tests_sha256": inputs.get("base_tests_sha256"),
        "candidate_count": len(emitted),
        "prompt_template_version": generation.get("prompt_template_version"),
        "model_config_id": generation.get("model_config_id"),
        "actual_usage": generation.get("actual_usage"),
        "retained_count": retained,
        "legal_base_file_count": len(expected_base_files),
        "vanilla_prompt_base_file_count": len(generator_base_files),
        "vanilla_prompt_base_file_coverage": len(generator_base_files) / max(1, len(expected_base_files)),
        "rejected_count": len(emitted) - retained,
        "candidate_validation_status_counts": dict(sorted(Counter(item.get("status", "missing") for item in validation).items())),
        "frozen_suite_sha256": frozen["suite_sha256"],
        "baseline_audit_sha256": frozen.get("metadata", {}).get("baseline_audit_sha256"),
        "status": "complete",
    }


def main() -> int:
    final = read_json(FINAL_BASE_AUDIT)
    tasks = sorted(
        path.name
        for path in RESULT_ROOT.iterdir()
        if any((path / "C1" / name / "frozen_test_suite.json").is_file() for name in COHORT_NAMES)
    )
    errors: list[dict[str, str]] = []
    records = []
    for task in tasks:
        try:
            records.append(audit_task(task, final))
        except (OSError, KeyError, ValueError, json.JSONDecodeError, StopIteration) as error:
            errors.append({"task_id": task, "error": str(error)})

    retained_total = sum(item["retained_count"] for item in records)
    candidate_total = sum(item["candidate_count"] for item in records)
    report = {
        "format": "rq1-c1-vanilla-cohort-audit/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "cohort": "rq1_full_36x1/C1",
        "status": "complete" if len(records) == 36 and not errors else "incomplete",
        "task_count": len(records),
        "expected_task_count": 36,
        "candidate_count": candidate_total,
        "retained_count": retained_total,
        "rejected_count": candidate_total - retained_total,
        "empty_generation_task_count": sum(item["candidate_count"] == 0 for item in records),
        "old_base_audit_sha256": sha256_file(OLD_BASE_AUDIT) if OLD_BASE_AUDIT.is_file() else None,
        "final_base_audit_sha256": sha256_file(FINAL_BASE_AUDIT),
        "records": records,
        "errors": errors,
    }
    OUTPUT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "task_count", "candidate_count", "retained_count", "rejected_count", "empty_generation_task_count", "errors")}, ensure_ascii=False))
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())

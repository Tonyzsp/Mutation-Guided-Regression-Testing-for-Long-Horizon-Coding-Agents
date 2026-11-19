"""Assemble a hash-audited C1 triage cohort with contaminated attempts excluded."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESULT_ROOT = ROOT / "artifacts" / "results" / "rq1_full_36x1"
SURVIVOR_MANIFEST = RESULT_ROOT / "survivor_only_input_v1" / "manifest.json"
POLICY_SHA256 = "ab12f34c7601edc36f1454481a44c2a707e8977d61d62f37f3d892e825eb7eb2"
REFERENCE_SHA256 = "270c4779355f0c7a3440e46405ff0db0b9948d7583c4c11bcc5638d8b12e832a"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def logical_prompt_sha(path: Path) -> str:
    return sha256_bytes(path.read_text(encoding="utf-8").encode("utf-8"))


def verify_input_bundle(task_id: str, task_dir: Path, require_call_link: bool) -> dict[str, Any]:
    prompt_path = task_dir / "triage_prompt_inline_v2.md"
    manifest_path = task_dir / "triage_input_manifest_inline_v2.json"
    selection_path = task_dir / "family_selection.json"
    selected_path = task_dir / "selected_representatives.json"
    for path in (prompt_path, manifest_path, selection_path, selected_path):
        if not path.is_file():
            raise FileNotFoundError(f"{task_id}: missing triage input file: {path}")
    prompt = read_json(manifest_path)
    if prompt.get("task_id") != task_id or prompt.get("prompt_sha256") != logical_prompt_sha(prompt_path):
        raise ValueError(f"{task_id}: current prompt text does not match its input manifest")
    visible = {row.get("path") for row in prompt.get("visible_files", [])}
    if "base_tests/solutions.py" in visible:
        raise ValueError(f"{task_id}: future-reference solutions.py appears in the model-visible file list")
    if task_id == "test_translator":
        if prompt.get("model_input_exclusion_policy_id") != "rq1_c1_future_reference_exclusion_v1":
            raise ValueError("test_translator: sanitized model-input policy is not recorded")
        if not any(row.get("path") == "base_tests/solutions.py" and row.get("sha256") == REFERENCE_SHA256 for row in prompt.get("excluded_model_inputs", [])):
            raise ValueError("test_translator: excluded reference file and hash are not attested")
    elif "base_tests/solutions.py" in visible:
        raise ValueError(f"{task_id}: unexpected reference implementation is model-visible")

    call_path = task_dir / "triage_call_manifest.json"
    call = read_json(call_path) if call_path.is_file() else {}
    actual_prompt_file_sha = sha256_file(prompt_path)
    actual_manifest_file_sha = sha256_file(manifest_path)
    linked = (
        call.get("calls", 0) == 0
        or (
            call.get("prompt_sha256") == actual_prompt_file_sha
            and call.get("triage_input_manifest_sha256") == actual_manifest_file_sha
        )
    )
    if require_call_link and not linked:
        raise ValueError(f"{task_id}: call prompt/input hashes do not match the archived files")
    return {
        "task_id": task_id,
        "prompt_file_sha256": actual_prompt_file_sha,
        "prompt_logical_sha256": prompt["prompt_sha256"],
        "input_manifest_file_sha256": actual_manifest_file_sha,
        "selection_sha256": sha256_file(selection_path),
        "selected_representatives_sha256": sha256_file(selected_path),
        "call_status": call.get("status", "prepared_only"),
        "calls": call.get("calls", 0),
        "attempts": len(call.get("attempts", [])),
        "call_inputs_hash_linked": linked,
    }


def copy_attempt_evidence(source: Path, destination: Path) -> None:
    patterns = (
        "triage_response_attempt_*.json",
        "codex_events_attempt_*.jsonl",
        "codex_stderr_attempt_*.log",
    )
    for pattern in patterns:
        for source_path in source.glob(pattern):
            shutil.copy2(source_path, destination / source_path.name)


def invalidated_attempt_manifest(task_id: str, source: Path, destination: Path, current_study_budget_sha: str) -> dict[str, Any]:
    old_call = read_json(source / "triage_call_manifest.json")
    shutil.copy2(source / "triage_response.json", destination / "triage_response_prior_input.json")
    copy_attempt_evidence(source, destination)
    old_attempts = []
    for attempt in old_call.get("attempts", []):
        old_attempts.append({
            **attempt,
            "input_prompt_sha256": old_call.get("prompt_sha256"),
            "input_manifest_sha256": old_call.get("triage_input_manifest_sha256"),
            "excluded_from_current_triage": True,
        })
    selected_path = destination / "selected_representatives.json"
    selection = read_json(destination / "family_selection.json")
    response_path = destination / "triage_response.json"
    write_json(response_path, {"decisions": []})
    call = {
        "task_id": task_id,
        "status": "prior_attempts_invalidated_input_hash_mismatch",
        "calls": old_call.get("calls", len(old_attempts)),
        "study_budget_sha256": current_study_budget_sha,
        "selected_representatives_sha256": sha256_file(selected_path),
        "family_selection_sha256": selection["selection_manifest_sha256"],
        "prompt_sha256": None,
        "triage_input_manifest": "triage_input_manifest_inline_v2.json",
        "triage_input_manifest_sha256": None,
        "response_sha256": sha256_file(response_path),
        "attempts": old_attempts,
        "invalidated_prior_prompt_sha256": old_call.get("prompt_sha256"),
        "invalidated_prior_input_manifest_sha256": old_call.get("triage_input_manifest_sha256"),
    }
    write_json(destination / "triage_call_manifest.json", call)
    return call


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-root", type=Path, default=RESULT_ROOT / "mutant_family_triage_formal_v1")
    parser.add_argument("--reconstructed-root", type=Path, default=RESULT_ROOT / "mutant_family_triage_reaudit_20261117")
    parser.add_argument("--clean-task-root", type=Path, default=RESULT_ROOT / "mutant_family_triage_test_translator_clean_v1_20261117" / "test_translator")
    parser.add_argument("--output-root", type=Path, default=RESULT_ROOT / "mutant_family_triage_formal_v1_sanitized_20261117")
    args = parser.parse_args()

    old_root = args.old_root.resolve(strict=True)
    reconstructed_root = args.reconstructed_root.resolve(strict=True)
    clean_task_root = args.clean_task_root.resolve(strict=True)
    output_root = args.output_root.resolve()
    if not output_root.is_relative_to(RESULT_ROOT.resolve(strict=True)):
        raise ValueError("assembled triage root must remain under formal results")
    if output_root.exists():
        raise FileExistsError(f"refusing to overwrite existing assembled root: {output_root}")

    survivor_manifest = read_json(SURVIVOR_MANIFEST)
    task_ids = sorted(survivor_manifest["tasks"])
    if len(task_ids) != 36:
        raise ValueError(f"expected 36 tasks, found {len(task_ids)}")
    old_run = read_json(old_root / "run_manifest.json")
    reconstructed_run = read_json(reconstructed_root / "run_manifest.json")
    clean_run = read_json(clean_task_root.parent / "run_manifest.json")
    if old_run.get("task_count") != 36 or reconstructed_run.get("task_count") != 35 or clean_run.get("task_count") != 1:
        raise ValueError("triage source manifests do not have expected 36/35/1 task counts")
    study_budget_sha = old_run["study_budget_sha256"]
    if reconstructed_run.get("study_budget_sha256") != study_budget_sha or clean_run.get("study_budget_sha256") != study_budget_sha:
        raise ValueError("triage source manifests use different locked study budgets")
    policy_path = ROOT / "config" / "rq1_model_input_exclusions_v1.json"
    if sha256_file(policy_path) != POLICY_SHA256:
        raise ValueError("model-input exclusion policy hash differs from the locked value")

    old_task_dirs = {task_id: old_root / task_id for task_id in task_ids}
    trusted: set[str] = set()
    invalidated: set[str] = set()
    no_call: set[str] = set()
    pre_audit: dict[str, dict[str, Any]] = {}
    for task_id in task_ids:
        if task_id == "test_translator":
            pre_audit[task_id] = verify_input_bundle(task_id, clean_task_root, require_call_link=True)
            continue
        old_dir = old_task_dirs[task_id]
        call_path = old_dir / "triage_call_manifest.json"
        call = read_json(call_path)
        if call.get("calls", 0) == 0:
            no_call.add(task_id)
            continue
        try:
            pre_audit[task_id] = verify_input_bundle(task_id, old_dir, require_call_link=True)
            trusted.add(task_id)
        except ValueError:
            invalidated.add(task_id)

    if invalidated != {"cfgpipe", "circuit_eval"} or no_call != {"eve_route_planner"}:
        raise ValueError(f"unexpected prompt provenance audit result: invalidated={sorted(invalidated)}, no_call={sorted(no_call)}")
    if len(trusted) != 32 or trusted | invalidated | no_call | {"test_translator"} != set(task_ids):
        raise ValueError(f"expected 32 reusable, 2 invalidated, 1 no-call, 1 regenerated; got {len(trusted)} reusable")

    output_root.mkdir(parents=True)
    task_audit: list[dict[str, Any]] = []
    task_sources: dict[str, Path] = {}
    for task_id in task_ids:
        if task_id == "test_translator":
            source = clean_task_root
            shutil.copytree(source, output_root / task_id)
            row = verify_input_bundle(task_id, output_root / task_id, require_call_link=True)
            row["source_kind"] = "regenerated_clean_model_input"
        elif task_id in trusted:
            source = old_task_dirs[task_id]
            shutil.copytree(source, output_root / task_id)
            row = verify_input_bundle(task_id, output_root / task_id, require_call_link=True)
            row["source_kind"] = "reused_exact_archived_model_input"
        else:
            source = reconstructed_root / task_id
            shutil.copytree(source, output_root / task_id)
            if task_id in invalidated:
                call = invalidated_attempt_manifest(task_id, old_task_dirs[task_id], output_root / task_id, study_budget_sha)
                row = verify_input_bundle(task_id, output_root / task_id, require_call_link=False)
                row.update({
                    "call_status": call["status"],
                    "calls": call["calls"],
                    "attempts": len(call["attempts"]),
                    "call_inputs_hash_linked": False,
                    "source_kind": "reprepared_input_prior_model_attempts_invalidated",
                })
            else:
                # This task had no surviving mutants and made no model call.
                row = verify_input_bundle(task_id, output_root / task_id, require_call_link=False)
                selection = read_json(output_root / task_id / "family_selection.json")
                if selection.get("survivor_count") != 0 or selection.get("selected_representative_ids"):
                    raise ValueError(f"{task_id}: expected an empty survivor selection")
                response_path = output_root / task_id / "triage_response.json"
                write_json(response_path, {"decisions": []})
                call = {
                    "task_id": task_id,
                    "status": "no_selected_hints",
                    "calls": 0,
                    "study_budget_sha256": study_budget_sha,
                    "selected_representatives_sha256": sha256_file(output_root / task_id / "selected_representatives.json"),
                    "family_selection_sha256": selection["selection_manifest_sha256"],
                    "prompt_sha256": sha256_file(output_root / task_id / "triage_prompt_inline_v2.md"),
                    "triage_input_manifest": "triage_input_manifest_inline_v2.json",
                    "triage_input_manifest_sha256": sha256_file(output_root / task_id / "triage_input_manifest_inline_v2.json"),
                    "response_sha256": sha256_file(response_path),
                    "attempts": [],
                }
                write_json(output_root / task_id / "triage_call_manifest.json", call)
                row.update({"call_status": call["status"], "calls": 0, "attempts": 0, "call_inputs_hash_linked": True})
                row["source_kind"] = "reprepared_empty_survivor_task_no_model_call"
        row["task_id"] = task_id
        task_audit.append(row)
        task_sources[task_id] = source

    # The reconstructed inputs are the manifest basis for the two invalidated tasks and the no-mutant task.
    merged_run = dict(reconstructed_run)
    merged_tasks = dict(reconstructed_run["tasks"])
    for task_id in trusted:
        merged_tasks[task_id] = old_run["tasks"][task_id]
    merged_tasks["test_translator"] = clean_run["tasks"]["test_translator"]
    merged_run.update({
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "task_count": 36,
        "tasks": dict(sorted(merged_tasks.items())),
        "input_sanitization": {
            "policy_id": "rq1_c1_future_reference_exclusion_v1",
            "policy_path": policy_path.relative_to(ROOT).as_posix(),
            "policy_sha256": POLICY_SHA256,
            "reused_exact_input_tasks": 32,
            "reprepared_tasks": 3,
            "regenerated_clean_triage_tasks": 1,
        },
    })
    write_json(output_root / "run_manifest.json", merged_run)

    old_ledger = [json.loads(line) for line in (old_root / "triage_call_ledger.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    clean_ledger = [json.loads(line) for line in (clean_task_root.parent / "triage_call_ledger.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    valid_ledger = [row for row in old_ledger if row.get("task_id") != "test_translator"] + clean_ledger
    starts = [row for row in valid_ledger if row.get("event") == "call_started"]
    if len(starts) > 72:
        raise ValueError(f"combined triage attempts exceed locked limit: {len(starts)}>72")
    with (output_root / "triage_call_ledger.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for row in valid_ledger:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    old_test_manifest = read_json(old_task_dirs["test_translator"] / "triage_input_manifest_inline_v2.json")
    attestation = {
        "format": "rq1-sanitized-mutant-family-triage-assembly/v2",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "task_count": 36,
        "reused_exact_input_tasks": 32,
        "reprepared_tasks": 3,
        "regenerated_clean_triage_tasks": 1,
        "prior_call_started_events_in_valid_ledger": len(starts),
        "invalidated_prior_test_translator": {
            "source_root": str(old_root.relative_to(ROOT).as_posix()),
            "reason": "The previous model-visible manifest listed base_tests/solutions.py, which contains later-checkpoint reference implementations.",
            "contaminated_prompt_logical_sha256": old_test_manifest.get("prompt_sha256"),
            "contaminated_input_manifest_file_sha256": sha256_file(old_task_dirs["test_translator"] / "triage_input_manifest_inline_v2.json"),
            "original_artifacts_preserved": True,
        },
        "invalidated_prior_prompt_hash_mismatches": {
            task_id: {
                "prior_prompt_sha256": read_json(old_task_dirs[task_id] / "triage_call_manifest.json").get("prompt_sha256"),
                "prior_input_manifest_sha256": read_json(old_task_dirs[task_id] / "triage_call_manifest.json").get("triage_input_manifest_sha256"),
                "valid_structured_response_reused": False,
                "prior_attempts_preserved_in_task_directory": True,
            }
            for task_id in sorted(invalidated)
        },
        "tasks": task_audit,
    }
    write_json(output_root / "triage_reuse_attestation.json", attestation)
    print(json.dumps({
        "output_root": output_root.relative_to(ROOT).as_posix(),
        "task_count": 36,
        "reused_exact_input_tasks": len(trusted),
        "reprepared_stale_prompt_tasks": len(invalidated),
        "reprepared_no_mutant_tasks": len(no_call),
        "regenerated_clean_triage_tasks": 1,
        "prior_call_started_events": len(starts),
        "status": "assembled; run triage finalization next",
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

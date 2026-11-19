"""Prepare exact, hash-linked MG recovery-v2 review prompts locally; make no model calls."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "artifacts" / "results"
V2_ROOT = RESULTS / "rq1_recovery_v2"
PACKAGE_ROOT = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
STUDY_BUDGET = ROOT / "config" / "rq1_recovery_v2_study_budget.json"
INPUT_ROOT = V2_ROOT / "candidate_review_inputs"
sys.path.insert(0, str(ROOT / "scripts"))
from review_generated_candidates import render_prompt  # noqa: E402


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    summary = read_json(V2_ROOT / "mg_review_bundle_summary.json")
    budget = read_json(STUDY_BUDGET)
    study_sha = sha256_file(STUDY_BUDGET)
    requested_tokens = int(budget["candidate_review"]["requested_output_tokens_per_call"])
    rows: list[dict[str, Any]] = []
    for task_row in summary["records"]:
        task = task_row["task_id"]
        count = int(task_row["candidate_count"])
        if count == 0:
            continue
        package_root = PACKAGE_ROOT / task / "C1"
        package = read_json(package_root / "package.json")
        bundle_dir = V2_ROOT / task / "C1" / "mutation_guided" / "candidate_review_bundle"
        response_path = bundle_dir / "candidate_response.json"
        response = read_json(response_path)
        if len(response.get("tests", [])) != count:
            raise ValueError(f"{task}: candidate count differs from the reviewed bundle summary")
        prompt, excluded, exclusion_policy_sha = render_prompt(
            package_root, package, response["tests"], requested_tokens
        )
        task_input_dir = INPUT_ROOT / task
        prompt_path = task_input_dir / "review_prompt.md"
        manifest_path = task_input_dir / "input_manifest.json"
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        manifest = {
            "format": "rq1-recovery-v2-mg-candidate-review-input/v1",
            "task_id": task,
            "checkpoint_id": "C1",
            "method_label_in_prompt": False,
            "candidate_count": count,
            "source_generation_directory": str(bundle_dir.relative_to(ROOT).as_posix()),
            "source_candidate_response_sha256": sha256_file(response_path),
            "source_package_json_sha256": sha256_file(package_root / "package.json"),
            "source_specification_sha256": sha256_file(package_root / package["specification_path"]),
            "source_base_tests_tree_sha256": package["base_tests_sha256"],
            "study_budget_id": budget["experiment_id"],
            "study_budget_sha256": study_sha,
            "model_input_exclusion_policy_sha256": exclusion_policy_sha,
            "excluded_model_inputs": excluded,
            "prompt_sha256": prompt_hash,
            "prompt_path": str(prompt_path.relative_to(ROOT).as_posix()),
            "prompt_bytes": len(prompt.encode("utf-8")),
            "requested_output_tokens": requested_tokens,
            "llm_calls_made": 0,
            "status": "prepared_locally_not_sent",
            "payload_scope": [
                "C1 public specification",
                "legal C1 base-test inventory after locked exclusions",
                "the generated candidate pytest files and their stated specification basis",
            ],
            "excluded_payload": [
                "mutant IDs and diffs",
                "Original/Vanilla method labels",
                "hidden, future, and excluded tests",
                "later checkpoints and regression outcomes",
            ],
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        if task_input_dir.exists():
            prior_manifest = read_json(manifest_path)
            prior_prompt = prompt_path.read_text(encoding="utf-8")
            if prior_manifest.get("prompt_sha256") != prompt_hash or prior_prompt != prompt:
                raise FileExistsError(f"{task_input_dir} already contains a different prepared prompt")
        else:
            task_input_dir.mkdir(parents=True)
            prompt_path.write_text(prompt, encoding="utf-8")
            write_json(manifest_path, manifest)
        rows.append({
            "task_id": task,
            "candidate_count": count,
            "prompt_path": str(prompt_path.relative_to(ROOT).as_posix()),
            "prompt_sha256": prompt_hash,
            "prompt_bytes": len(prompt.encode("utf-8")),
            "excluded_model_inputs": excluded,
            "status": "prepared_locally_not_sent",
        })

    report = {
        "format": "rq1-recovery-v2-mg-review-prompt-preparation/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "study_budget_id": budget["experiment_id"],
        "study_budget_sha256": study_sha,
        "task_count": len(rows),
        "candidate_count": sum(row["candidate_count"] for row in rows),
        "total_prompt_bytes": sum(row["prompt_bytes"] for row in rows),
        "llm_calls_made": 0,
        "status": "prepared_locally_not_sent",
        "records": rows,
    }
    report_path = V2_ROOT / "mg_review_prompt_preparation.json"
    write_json(report_path, report)
    print(json.dumps({
        "task_count": report["task_count"],
        "candidate_count": report["candidate_count"],
        "total_prompt_bytes": report["total_prompt_bytes"],
        "llm_calls_made": 0,
        "status": report["status"],
        "report": str(report_path.relative_to(ROOT).as_posix()),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

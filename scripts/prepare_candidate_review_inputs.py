"""Prepare review prompts and provenance locally without invoking an LLM."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "artifacts" / "results" / "rq1_full_36x1"
PACKAGE_ROOT = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
UNITS = ROOT / "config" / "rq1_full_36x1_candidate_units.json"
STUDY_BUDGET = ROOT / "config" / "rq1_full_study_budget_v1.json"
DEFAULT_SUFFIX = "vanilla_formal_v1_20261117"
DEFAULT_OUTPUT = RESULTS / "candidate_review_inputs_vanilla_formal_v1_20261117"
sys.path.insert(0, str(ROOT / "scripts"))
from review_generated_candidates import render_prompt  # noqa: E402
from model_input_policy import POLICY_PATH, sha256_file as policy_sha256_file  # noqa: E402


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation-suffix", default=DEFAULT_SUFFIX)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if Path(args.generation_suffix).name != args.generation_suffix or args.generation_suffix in {".", ".."}:
        raise ValueError("generation suffix must be one path component")
    output_root = args.output.resolve()
    if not output_root.is_relative_to(RESULTS.resolve(strict=True)):
        raise ValueError("prepared review prompts must remain under the RQ1 result directory")
    if output_root.exists():
        raise FileExistsError(f"refusing to overwrite prepared review inputs: {output_root}")
    output_root.mkdir(parents=True)
    budget = read_json(STUDY_BUDGET)
    study_sha = sha256(STUDY_BUDGET.read_bytes())
    requested_tokens = int(budget["candidate_review"]["requested_output_tokens_per_call"])
    records: list[dict[str, Any]] = []
    no_generation_count = 0
    for unit in read_json(UNITS)["units"]:
        task = unit["task_id"]
        package_dir = PACKAGE_ROOT / task / "C1"
        package = read_json(package_dir / "package.json")
        generation_dir = RESULTS / task / "C1" / args.generation_suffix
        response_path = generation_dir / "candidate_response.json"
        if not response_path.is_file():
            if args.generation_suffix.startswith("mutation_guided"):
                triage_path = RESULTS / "mutant_family_triage_formal_v1_sanitized_20261117" / task / "mutant_triage.json"
                if triage_path.is_file() and read_json(triage_path).get("selected_for_generation_count", 0) == 0:
                    no_generation_count += 1
                    records.append({
                        "task_id": task,
                        "status": "no_generation_no_eligible_hints",
                        "candidate_count": 0,
                        "llm_calls": 0,
                    })
                    continue
            raise FileNotFoundError(f"{task}: generation response is missing for a task expected to have a suite")
        response = read_json(response_path)
        candidates = response.get("tests")
        if not isinstance(candidates, list):
            raise ValueError(f"{task}: candidate response has no tests array")
        if not candidates:
            records.append({"task_id": task, "status": "no_candidates", "candidate_count": 0, "llm_calls": 0})
            continue
        prompt, excluded_model_inputs, exclusion_policy_sha = render_prompt(
            package_dir, package, candidates, requested_tokens
        )
        task_dir = output_root / task
        task_dir.mkdir()
        prompt_path = task_dir / "review_prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        manifest = {
            "format": "rq1-candidate-review-input/v1",
            "task_id": task,
            "checkpoint_id": "C1",
            "method_label_in_prompt": False,
            "candidate_count": len(candidates),
            "source_generation_directory": str(generation_dir.relative_to(ROOT)),
            "source_candidate_response_sha256": sha256(response_path.read_bytes()),
            "source_package_json_sha256": sha256((package_dir / "package.json").read_bytes()),
            "source_specification_sha256": sha256((package_dir / package["specification_path"]).read_bytes()),
            "source_base_tests_tree_sha256": package["base_tests_sha256"],
            "study_budget_id": budget["experiment_id"],
            "study_budget_sha256": study_sha,
            "model_input_exclusion_policy_sha256": exclusion_policy_sha,
            "excluded_model_inputs": excluded_model_inputs,
            "prompt_sha256": sha256(prompt.encode("utf-8")),
            "prompt_path": str(prompt_path.relative_to(ROOT)),
            "llm_calls_made": 0,
            "status": "prepared_locally_not_sent",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        (task_dir / "input_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        records.append({
            "task_id": task,
            "status": "prepared_locally_not_sent",
            "candidate_count": len(candidates),
            "prompt_path": str(prompt_path.relative_to(ROOT)),
            "prompt_sha256": manifest["prompt_sha256"],
            "llm_calls": 0,
            "excluded_model_inputs": excluded_model_inputs,
        })

    report = {
        "format": "rq1-candidate-review-input-preparation/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "generation_suffix": args.generation_suffix,
        "study_budget_id": budget["experiment_id"],
        "study_budget_sha256": study_sha,
        "exclusion_policy_path": str(POLICY_PATH.relative_to(ROOT)),
        "exclusion_policy_sha256": policy_sha256_file(POLICY_PATH),
        "task_count": len(records),
        "candidate_count": sum(row["candidate_count"] for row in records),
        "nonempty_review_prompt_count": sum(row["status"] == "prepared_locally_not_sent" for row in records),
        "no_candidate_count": sum(row["status"] == "no_candidates" for row in records),
        "no_generation_no_eligible_hint_count": no_generation_count,
        "llm_calls_made": 0,
        "status": "prepared_locally_not_sent",
        "records": records,
    }
    report_path = output_root / "preparation_manifest.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("task_count", "candidate_count", "nonempty_review_prompt_count", "no_candidate_count", "llm_calls_made", "status")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

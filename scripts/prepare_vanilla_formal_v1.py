"""Build the new formal Vanilla artifact tree from exact-prompt reusable runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "artifacts" / "results" / "rq1_full_36x1"
PACKAGE_ROOT = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
DEFAULT_AUDIT = RESULTS / "vanilla_generation_reuse_audit_formal_v1_20261117.json"
DEFAULT_BUDGET = ROOT / "config" / "rq1_full_study_budget_v1.json"
GENERATION_BUDGET = ROOT / "config" / "generation_budget_rq1_c1_cohort_formal.json"
DEFAULT_SUFFIX = "vanilla_formal_v1_20261117"
COPY_PATTERNS = (
    "candidate_response.json",
    "candidate_response_attempt_*.json",
    "codex-events_attempt_*.jsonl",
    "codex-stderr_attempt_*.log",
    "generation_attempts.jsonl",
    "input_manifest.json",
    "package_audit.txt",
    "prompt_archive.md",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--study-budget", type=Path, default=DEFAULT_BUDGET)
    parser.add_argument("--output-suffix", default=DEFAULT_SUFFIX)
    args = parser.parse_args()
    if Path(args.output_suffix).name != args.output_suffix or args.output_suffix in {".", ".."}:
        raise ValueError("output suffix must be one path component")

    audit = read_json(args.audit.resolve(strict=True))
    budget_path = args.study_budget.resolve(strict=True)
    budget = read_json(budget_path)
    budget_sha = sha256_file(budget_path)
    generation_budget = read_json(GENERATION_BUDGET)
    generation_budget_sha = sha256_file(GENERATION_BUDGET)
    if audit.get("study_budget_sha256") != budget_sha:
        raise ValueError("reuse audit and study budget hashes differ")
    if budget["generation"].get("budget_source_sha256") != generation_budget_sha:
        raise ValueError("generation budget hash differs from the locked study budget")
    adopted: list[dict[str, Any]] = []
    for row in audit.get("records", []):
        if row.get("status") != "reuse_generation_and_revalidate":
            continue
        task = row["task_id"]
        source_dir = ROOT / row["generation_directory"]
        target_dir = RESULTS / task / "C1" / args.output_suffix
        if target_dir.exists():
            raise FileExistsError(f"formal output already exists; refusing to overwrite: {target_dir}")
        source_run_path = source_dir / "generation_run.json"
        source_manifest_path = source_dir / "input_manifest.json"
        source_prompt_path = source_dir / "prompt_archive.md"
        source_response_path = source_dir / "candidate_response.json"
        source_run = read_json(source_run_path)
        if source_run.get("method") != "vanilla_llm" or source_run.get("codex_exit_code") != 0:
            raise ValueError(f"{task}: historical run is not a successful Vanilla generation")
        if source_run.get("model_config_id") != generation_budget["model"]["model_config_id"]:
            raise ValueError(f"{task}: historical model configuration differs from the formal budget")
        if source_run.get("budget_id") != generation_budget["budget_id"]:
            raise ValueError(f"{task}: historical generation budget differs from the formal budget")
        if source_run.get("candidate_count", 0) > generation_budget["budget"]["max_candidates"]:
            raise ValueError(f"{task}: historical candidate count exceeds the formal budget")
        if source_run.get("actual_usage", {}).get("calls", 0) > generation_budget["budget"]["max_calls"]:
            raise ValueError(f"{task}: historical call count exceeds the formal budget")
        if sha256_file(source_run_path) != row.get("generation_run_sha256"):
            raise ValueError(f"{task}: generation run changed after the reuse audit")
        if sha256_file(source_manifest_path) != row.get("input_manifest_sha256"):
            raise ValueError(f"{task}: input manifest changed after the reuse audit")
        if sha256_file(source_response_path) != row.get("candidate_response_sha256"):
            raise ValueError(f"{task}: candidate response changed after the reuse audit")

        target_dir.mkdir(parents=True)
        copied: list[str] = []
        for pattern in COPY_PATTERNS:
            for source in sorted(source_dir.glob(pattern)):
                destination = target_dir / source.name
                shutil.copy2(source, destination)
                copied.append(source.name)
        required = {"candidate_response.json", "generation_attempts.jsonl", "input_manifest.json", "prompt_archive.md", "package_audit.txt"}
        if not required.issubset(copied):
            raise ValueError(f"{task}: missing required source-generation artifacts: {sorted(required - set(copied))}")

        shutil.copy2(source_run_path, target_dir / "generation_run_source.json")
        derived_run = dict(source_run)
        derived_run.update({
            "study_budget_id": budget["experiment_id"],
            "study_budget_sha256": budget_sha,
            "formal_eligibility": True,
            "generation_execution_kind": "verified_historical_reuse",
            "generation_run_source_path": "generation_run_source.json",
            "generation_run_source_sha256": sha256_file(source_run_path),
            "formal_adoption_at_utc": datetime.now(timezone.utc).isoformat(),
            "retained_count": 0,
        })
        (target_dir / "generation_run.json").write_text(
            json.dumps(derived_run, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        attestation = {
            "format": "rq1-vanilla-generation-reuse-attestation/v1",
            "task_id": task,
            "checkpoint_id": "C1",
            "study_budget_id": budget["experiment_id"],
            "study_budget_sha256": budget_sha,
            "reuse_audit_path": str(args.audit.resolve(strict=True).relative_to(ROOT)),
            "reuse_audit_sha256": sha256_file(args.audit.resolve(strict=True)),
            "source_generation_directory": str(source_dir.resolve(strict=True).relative_to(ROOT)),
            "source_generation_run_sha256": sha256_file(source_run_path),
            "source_input_manifest_sha256": sha256_file(source_manifest_path),
            "source_prompt_sha256": sha256_file(source_prompt_path),
            "source_candidate_response_sha256": sha256_file(source_response_path),
            "generation_budget_id": source_run["budget_id"],
            "model_config_id": source_run["model_config_id"],
            "actual_generation_calls": source_run.get("actual_usage", {}).get("calls"),
            "candidate_count": source_run.get("candidate_count"),
            "reuse_basis": "The audit reconstructed the current C1 prompt byte-for-byte from the sanitized package, current gap brief, prompt template, and locked generation budget; the historical archived prompt matched exactly.",
            "copied_artifacts": copied,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        (target_dir / "generation_reuse_attestation.json").write_text(
            json.dumps(attestation, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        adopted.append({"task_id": task, "source": str(source_dir.relative_to(ROOT)), "output": str(target_dir.relative_to(ROOT))})

    report = {
        "format": "rq1-vanilla-formal-reuse-preparation/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "study_budget_id": budget["experiment_id"],
        "study_budget_sha256": budget_sha,
        "reused_task_count": len(adopted),
        "regeneration_task_ids": [
            row["task_id"] for row in audit.get("records", []) if row.get("status") != "reuse_generation_and_revalidate"
        ],
        "tasks": adopted,
    }
    report_path = RESULTS / f"vanilla_formal_reuse_preparation_{args.output_suffix}.json"
    if report_path.exists():
        raise FileExistsError(f"refusing to overwrite preparation report: {report_path}")
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"reused_task_count": len(adopted), "regeneration_task_ids": report["regeneration_task_ids"], "report": str(report_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

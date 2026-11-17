"""Build a corrected mutant record bundle using direct C1 subprocess reclassification."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from c1_test_selection import stable_pass_selection

from validate_and_freeze_tests import read_json, sha256_file


ROOT = Path(__file__).resolve().parents[1]
RESULTS_ROOT = ROOT / "artifacts" / "results"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True, help="Completed raw mutmut/direct-classification workspace")
    parser.add_argument("--trajectory-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True, help="New mutation bundle directory under artifacts/results")
    parser.add_argument("--baseline-audit", type=Path, help="Final repeated C1 baseline audit used by direct classification")
    args = parser.parse_args()

    runtime = args.runtime_dir.resolve(strict=True)
    output = args.output_dir.resolve()
    if not runtime.is_relative_to((ROOT / "tmp").resolve(strict=True)):
        raise ValueError("runtime directory must remain under tmp")
    if not output.is_relative_to(RESULTS_ROOT.resolve(strict=True)):
        raise ValueError("output directory must remain under artifacts/results")
    if output.exists() and not output.is_dir():
        raise ValueError("output path exists and is not a directory")
    if (output / "mutation_summary.json").is_file():
        raise ValueError("output directory already contains a completed mutation bundle")

    raw_summary_path = runtime / "mutation_summary.json"
    raw_summary = read_json(raw_summary_path)
    raw_records_path = runtime / raw_summary["records_path"]
    direct_summary_path = runtime / "direct_mutant_classification_summary.json"
    direct_records_path = runtime / "direct_mutant_classification.jsonl"
    direct_config_path = runtime / "direct_mutant_classification_config.json"
    direct_summary = read_json(direct_summary_path)
    direct_config = read_json(direct_config_path)
    mutation_run_manifest = read_json(runtime / "mutmut_run_manifest.json")
    if raw_summary.get("task_id") != direct_summary.get("task_id") or raw_summary.get("checkpoint_id") != direct_summary.get("checkpoint_id"):
        raise ValueError("raw mutmut and direct-classification summaries identify different checkpoints")
    if direct_summary.get("complete") is not True:
        raise ValueError("direct classification is incomplete; refusing to normalize the mutant record set")
    if direct_summary.get("classification_algorithm_version") != "4":
        raise ValueError("direct classification uses an obsolete algorithm; rerun it in a fresh mutmut runtime")
    if direct_summary.get("classification_config_sha256") != direct_config.get("classification_config_sha256"):
        raise ValueError("direct classification summary and config signatures do not match")
    if (
        direct_config.get("mutation_protocol_version") != "rq1-mutmut-v4"
        or direct_config.get("mutation_run_config_sha256") != mutation_run_manifest.get("mutation_config_sha256")
    ):
        raise ValueError("direct classification was produced from a different or obsolete mutmut run")
    if direct_summary.get("source_sha256") != raw_summary.get("source_sha256"):
        raise ValueError("direct classification source hashes differ from the raw mutmut bundle")

    baseline_hash = direct_summary.get("baseline_audit_sha256")
    if args.baseline_audit:
        baseline_path = args.baseline_audit.resolve(strict=True)
        if not baseline_path.is_relative_to(RESULTS_ROOT.resolve(strict=True)):
            raise ValueError("baseline audit must remain under artifacts/results")
        baseline_audit = read_json(baseline_path)
        matches = [
            row for row in baseline_audit.get("records", [])
            if row.get("task_id") == raw_summary["task_id"]
            and row.get("checkpoint_id") == raw_summary["checkpoint_id"]
        ]
        if len(matches) != 1:
            raise ValueError("final baseline audit must contain exactly one matching C1 record")
        baseline_record = matches[0]
        runtime_manifest = read_json(runtime / "runtime_manifest.json")
        if baseline_record.get("base_tests_sha256") != runtime_manifest.get("base_tests_sha256"):
            raise ValueError("final baseline hash differs from the direct-classification runtime")
        if stable_pass_selection(baseline_record) != direct_config.get("stable_test_ids"):
            raise ValueError("final stable C1 passing tests differ from the direct-classification test selection")
        baseline_hash = sha256_file(baseline_path)

    raw_records = read_jsonl(raw_records_path)
    direct_records = read_jsonl(direct_records_path)
    raw_by_id = {item["mutant_id"]: item for item in raw_records}
    direct_by_id = {item["mutant_id"]: item for item in direct_records}
    if len(raw_by_id) != len(raw_records) or len(direct_by_id) != len(direct_records) or set(raw_by_id) != set(direct_by_id):
        raise ValueError("raw and direct mutant IDs do not form the same unique set")

    output.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    direct_counts: Counter[str] = Counter()
    reachability_counts: Counter[str] = Counter()
    for raw in raw_records:
        direct = direct_by_id[raw["mutant_id"]]
        status = direct.get("direct_status")
        if status not in {"killed", "survived", "not_tested", "error", "timeout"}:
            raise ValueError(f"{raw['mutant_id']}: direct status is not terminal: {status!r}")
        merged = dict(raw)
        merged["normalized_status"] = (
            "inconclusive" if status in {"not_tested", "error", "timeout"} else status
        )
        reached_function = direct.get("target_function_reached_in_baseline")
        merged["target_function_reached_in_baseline"] = reached_function
        reachability_counts[
            "reached" if reached_function is True else "not_observed" if reached_function is False else "unknown"
        ] += 1
        merged["review_status"] = (
            "needs_review"
            if status in {"survived", "not_tested", "error", "timeout"}
            else "not_reviewed"
        )
        if status == "not_tested":
            merged["review_rationale"] = (
                "mutmut reported no relevant tests and no matching subprocess hit was recorded; "
                "the mutant was not directly applied or tested, so it is not counted as a survivor"
            )
        elif status == "error":
            merged["review_rationale"] = (
                "direct application or test execution ended with an error; retain the evidence, "
                "exclude this mutant from the survivor pool and mutation-score denominator"
            )
        elif status == "timeout":
            merged["review_rationale"] = (
                "direct test execution timed out; retain the evidence, exclude this mutant from "
                "the survivor pool and mutation-score denominator"
            )
        else:
            merged["review_rationale"] = None
        evidence = merged.get("evidence_path", "")
        merged["evidence_path"] = ";".join(part for part in (evidence, "direct_mutant_classification.jsonl") if part)
        records.append(merged)
        direct_counts[status] += 1

    records_path = output / "mutant_records.jsonl"
    records_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
        encoding="utf-8",
    )
    for name in (
        "mutant_records.jsonl",
        "mutmut-results.txt",
        "mutmut-subprocess-hits.jsonl",
        "mutmut_run_manifest.json",
        "direct_mutant_classification.jsonl",
        "direct_mutant_classification.log",
        "direct_mutant_classification_attempts.jsonl",
        "direct_mutant_classification_config.json",
        "direct_mutant_classification_summary.json",
    ):
        source = runtime / name
        if source.is_file() and source != records_path:
            destination = output / ("raw_mutant_records.jsonl" if name == "mutant_records.jsonl" else name)
            shutil.copy2(source, destination)
            if name == "mutmut_run_manifest.json":
                copied_manifest = read_json(destination)
                copied_manifest["direct_classification_test_selection"] = direct_config["stable_test_ids"]
                copied_manifest["direct_classification_summary_path"] = "direct_mutant_classification_summary.json"
                (destination).write_text(json.dumps(copied_manifest, indent=2) + "\n", encoding="utf-8")

    denominator = direct_counts["killed"] + direct_counts["survived"]
    summary = {
        "format": "rq1-mutmut-export/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "task_id": raw_summary["task_id"],
        "trajectory_id": args.trajectory_id,
        "checkpoint_id": raw_summary["checkpoint_id"],
        "runtime_directory": str(runtime),
        "source_sha256": raw_summary["source_sha256"],
        "base_tests_sha256": read_json(runtime / "runtime_manifest.json").get("base_tests_sha256"),
        "mutation_test_selection": direct_config["stable_test_ids"],
        "mutant_count": len(records),
        "status_counts": dict(sorted(direct_counts.items())),
        "test_error_kill_count": direct_summary.get("test_error_kill_count", 0),
        "raw_mutmut_status_counts": raw_summary.get("status_counts", {}),
        "complete": len(records) == raw_summary.get("mutant_count"),
        "mutation_score": direct_counts["killed"] / denominator if denominator else None,
        "mutation_score_definition": "directly killed / (directly killed + directly survived), using per-mutant mutmut apply followed by stable passing C1 pytest IDs; not-tested, error, and timeout mutants are reported as inconclusive and excluded",
        "not_tested_count": direct_counts["not_tested"],
        "direct_error_count": direct_counts["error"],
        "direct_timeout_count": direct_counts["timeout"],
        "inconclusive_count": sum(direct_counts[key] for key in ("not_tested", "error", "timeout")),
        "all_direct_attempts_resolved": direct_summary.get("all_attempts_resolved"),
        "direct_classification_algorithm_version": direct_summary["classification_algorithm_version"],
        "direct_classification_config_sha256": direct_summary["classification_config_sha256"],
        "base_suite_target_function_reachability": dict(sorted(reachability_counts.items())),
        "records_path": records_path.name,
        "raw_summary_sha256": sha256_file(raw_summary_path),
        "raw_record_set_sha256": sha256_file(raw_records_path),
        "direct_classification_summary_sha256": sha256_file(direct_summary_path),
        "direct_classification_summary_path": "direct_mutant_classification_summary.json",
        "direct_classification_attempts_path": (
            "direct_mutant_classification_attempts.jsonl"
            if (runtime / "direct_mutant_classification_attempts.jsonl").is_file()
            else None
        ),
        "direct_classification_attempts_sha256": (
            sha256_file(runtime / "direct_mutant_classification_attempts.jsonl")
            if (runtime / "direct_mutant_classification_attempts.jsonl").is_file()
            else None
        ),
        "baseline_audit_sha256": baseline_hash,
        "direct_classification_baseline_audit_sha256": direct_summary.get("baseline_audit_sha256"),
    }
    (output / "mutation_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("task_id", "checkpoint_id", "mutant_count", "status_counts", "mutation_score", "complete")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

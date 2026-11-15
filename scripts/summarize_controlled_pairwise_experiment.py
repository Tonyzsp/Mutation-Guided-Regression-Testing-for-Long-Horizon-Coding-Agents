"""Summarize the controlled paired generation and validation funnel."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "artifacts" / "results" / "controlled_pairwise_v1_20261019" / "sample_manifest.json"
DEFAULT_REPORT = ROOT / "reports" / "controlled_pairwise_experiment_v1_20261019.md"


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def generation_dir(root: Path, pair: dict[str, Any], method: str) -> Path:
    relative = pair.get("output_relpath")
    if relative is not None:
        parts = Path(relative)
        if parts.is_absolute() or ".." in parts.parts:
            raise ValueError("pair output_relpath must stay under the output root")
        return root / parts / method
    return root / pair["task_id"] / slug(pair["mutant_id"]) / method


def all_true(values: list[Any]) -> bool:
    return bool(values) and all(value is True for value in values)


def summarize_candidate(run_dir: Path) -> dict[str, Any]:
    generation = read_json(run_dir / "generation_run.json", {})
    response = read_json(run_dir / "candidate_response.json", {})
    tests = response.get("tests", []) if isinstance(response, dict) else []
    review = read_json(run_dir / "correctness_review.json", {"reviews": []})
    verdicts = [row.get("verdict") for row in review.get("reviews", []) if isinstance(row, dict)]
    validation = read_jsonl(run_dir / "validation.jsonl")
    record = validation[0] if validation else {}
    target_results = record.get("target_mutant_results", [])
    baseline_runs = record.get("runs", [])
    original_pass = all_true([item.get("passed") for item in baseline_runs]) and len(baseline_runs) == 3
    target_stable = bool(target_results) and all(
        item.get("status") == "killed"
        and item.get("killed") is True
        and item.get("repeat_runs") == 3
        and len(item.get("repetition_results", [])) == 3
        and all(run.get("status") == "killed" for run in item.get("repetition_results", []))
        for item in target_results
    )
    collected = bool(record.get("collection_ids")) and record.get("static_check") == "passed"
    return {
        "generation_status": "complete" if generation else "missing",
        "candidate_count": len(tests) if isinstance(tests, list) else None,
        "generation_assessment": response.get("assessment") if isinstance(response, dict) else None,
        "generation_run": generation,
        "blind_review_verdicts": verdicts,
        "spec_correct": bool(verdicts) and all(v == "correct" for v in verdicts),
        "spec_uncertain": any(v == "uncertain" for v in verdicts),
        "validation_status": record.get("status"),
        "rejection_reason": record.get("rejection_reason"),
        "collected": collected,
        "collection_ids": record.get("collection_ids", []),
        "original_pass_3x": original_pass,
        "original_runs": baseline_runs,
        "target_killed_3x": target_stable,
        "target_mutant_results": target_results,
        "retained": record.get("status") == "retained",
        "candidate_sha256": record.get("content_sha256"),
    }


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(str(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    manifest_path = args.manifest.resolve(strict=True)
    manifest = read_json(manifest_path)
    output_root = args.output_root.resolve() if args.output_root else manifest_path.parent.resolve()
    if not output_root.is_relative_to((ROOT / "artifacts" / "results").resolve()):
        raise ValueError("experiment output root must remain under artifacts/results")

    pair_rows = []
    for pair in manifest.get("pairs", []):
        for method in ("vanilla_llm", "mutation_guided"):
            run_dir = generation_dir(output_root, pair, method)
            summary = summarize_candidate(run_dir)
            pair_rows.append({
                "pair_id": pair["pair_id"],
                "task_id": pair["task_id"],
                "mutant_id": pair["mutant_id"],
                "method": method,
                "run_dir": str(run_dir.relative_to(ROOT).as_posix()),
                **summary,
            })

    denominators = len(manifest.get("pairs", []))
    funnel = {}
    for method in ("vanilla_llm", "mutation_guided"):
        rows = [row for row in pair_rows if row["method"] == method]
        funnel[method] = {
            "eligible_mutants": denominators,
            "generation_calls": sum(
                (row["generation_run"].get("actual_usage", {}).get("calls") or 0)
                for row in rows if isinstance(row.get("generation_run"), dict)
            ),
            "responses_complete": sum(row["generation_status"] == "complete" for row in rows),
            "candidate_produced": sum(row["candidate_count"] is not None and row["candidate_count"] > 0 for row in rows),
            "blind_spec_correct": sum(row["spec_correct"] for row in rows),
            "collected": sum(row["collected"] for row in rows),
            "passed_original_3x": sum(row["original_pass_3x"] for row in rows),
            "killed_target_3x": sum(row["target_killed_3x"] for row in rows),
            "valid_targeted_test": sum(row["retained"] for row in rows),
            "review_uncertain": sum(row["spec_uncertain"] for row in rows),
            "candidate_files": sum(row["candidate_count"] or 0 for row in rows),
            "input_tokens_reported": sum(
                (row["generation_run"].get("actual_usage", {}).get("input_tokens") or 0)
                for row in rows if isinstance(row.get("generation_run"), dict)
            ),
            "output_tokens_reported": sum(
                (row["generation_run"].get("actual_usage", {}).get("output_tokens") or 0)
                for row in rows if isinstance(row.get("generation_run"), dict)
            ),
            "token_usage_complete_calls": sum(
                1 for row in rows
                if isinstance(row.get("generation_run"), dict)
                and row["generation_run"].get("actual_usage", {}).get("token_usage_available") is True
            ),
        }

    paired = []
    by_id: dict[str, dict[str, dict[str, Any]]] = {}
    for row in pair_rows:
        by_id.setdefault(row["mutant_id"], {})[row["method"]] = row
    for mutant_id, methods in sorted(by_id.items()):
        vanilla = methods.get("vanilla_llm", {})
        guided = methods.get("mutation_guided", {})
        paired.append({
            "mutant_id": mutant_id,
            "task_id": vanilla.get("task_id", guided.get("task_id")),
            "vanilla_candidates": vanilla.get("candidate_count"),
            "vanilla_spec": (vanilla.get("blind_review_verdicts") or [None])[0],
            "vanilla_original_pass": vanilla.get("original_pass_3x"),
            "vanilla_target_kill_3x": vanilla.get("target_killed_3x"),
            "vanilla_valid": vanilla.get("retained"),
            "guided_candidates": guided.get("candidate_count"),
            "guided_spec": (guided.get("blind_review_verdicts") or [None])[0],
            "guided_original_pass": guided.get("original_pass_3x"),
            "guided_target_kill_3x": guided.get("target_killed_3x"),
            "guided_valid": guided.get("retained"),
        })

    result = {
        "schema_version": "controlled-pairwise-results/v1",
        "experiment_id": manifest.get("experiment_id"),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "protocol_path": manifest.get("protocol_path"),
        "sample_manifest_sha256": __import__("hashlib").sha256(manifest_path.read_bytes()).hexdigest(),
        "eligible_mutants": denominators,
        "funnel": funnel,
        "pairs": paired,
        "method_rows": pair_rows,
        "interpretation_limit": "Diagnostic same-checkpoint targeted test generation only; not a later-checkpoint regression detection result and not an RQ1 answer.",
    }
    results_path = output_root / "controlled_pairwise_results.json"
    results_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    funnel_rows = []
    for label, key in [
        ("Eligible target mutants", "eligible_mutants"),
        ("Generation calls", "generation_calls"),
        ("Completed responses", "responses_complete"),
        ("Candidate produced", "candidate_produced"),
        ("Blind review: spec-correct", "blind_spec_correct"),
        ("Collected/executable", "collected"),
        ("Passed original C1 (3/3)", "passed_original_3x"),
        ("Killed target mutant (3/3)", "killed_target_3x"),
        ("Valid targeted test", "valid_targeted_test"),
        ("Review uncertain", "review_uncertain"),
    ]:
        funnel_rows.append([label, funnel["vanilla_llm"][key], funnel["mutation_guided"][key]])
    pair_rows_md = [[
        row["mutant_id"], row["vanilla_candidates"], row["vanilla_spec"],
        row["vanilla_target_kill_3x"], row["vanilla_valid"], row["guided_candidates"],
        row["guided_spec"], row["guided_target_kill_3x"], row["guided_valid"],
    ] for row in paired]
    report = f"""# Controlled Paired Mutation-Guided Test Generation — Results

**Experiment:** `{manifest.get('experiment_id')}`

**Eligible target mutants:** {denominators}

**Report generated:** {result['generated_at_utc']}

## Validation funnel

{markdown_table(['Stage', 'Vanilla', 'Mutation-Guided'], funnel_rows)}

Primary measure: valid targeted tests divided by all eligible target mutants ({denominators}). A target counts only when a candidate is reviewed as specification-correct, collected, passes the original C1 three times, kills the paired mutant in all three fresh mutated runs, and is retained by the validator.

## Paired outcomes

{markdown_table(['Mutant', 'V candidates', 'V spec', 'V kill 3/3', 'V valid', 'G candidates', 'G spec', 'G kill 3/3', 'G valid'], pair_rows_md)}

## Budget and audit notes

- Each target has one Vanilla call and one Mutation-Guided call, each capped at one candidate and a requested 4,096 output tokens with the same model and reasoning effort.
- Token counts are summed only where the Codex CLI reports them. Missing usage is not treated as zero in the per-run artifacts; the aggregate columns in the JSON count only reported values.
- Blind review labels are absent from the generation prompts. `uncertain` is not counted as a valid test pending human resolution.
- The frozen target list and data hashes are in `artifacts/results/controlled_pairwise_v1_20261019/sample_manifest.json`.

## Interpretation limit

This is a same-checkpoint test-generation diagnostic. It measures whether mutant guidance helps produce a specification-correct test for a known survivor. It does not test later checkpoints and does not answer RQ1.

Machine-readable results: `{results_path.relative_to(ROOT).as_posix()}`
"""
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    print(f"Results JSON: {results_path}")
    print(f"Report: {args.report}")
    print(json.dumps(funnel, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

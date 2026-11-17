"""Directly reclassify C1 mutmut results for the RQ1 checkpoint cohort."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from c1_test_selection import stable_pass_selection


ROOT = Path(__file__).resolve().parents[1]
RESULT_ROOT = ROOT / "artifacts" / "results" / "rq1_full_36x1"
DEFAULT_AUDIT = RESULT_ROOT / "c1_base_audit_isolation_revalidated_20261019" / "c1_base_suite_audit.json"
DEFAULT_UNITS = ROOT / "config" / "rq1_full_36x1_candidate_units.json"
RUNTIME_CONFIG = ROOT / "config" / "checkpoint_runtime.json"
CLASSIFIER = ROOT / "scripts" / "classify_direct_mutants.py"
MERGER = ROOT / "scripts" / "merge_direct_mutant_classification.py"
OUTPUT_LOG = RESULT_ROOT / "c1_direct_mutant_cohort_v4_20261115.jsonl"
MUTATION_PROTOCOL_VERSION = "rq1-mutmut-v4"
DIRECT_CLASSIFIER_VERSION = "4"
MUTATION_BRIDGES = {
    "mutmut_subprocess_bridge.py": ROOT / "scripts" / "mutmut_subprocess_bridge.py",
    "sitecustomize.py": ROOT / "scripts" / "mutmut_subprocess_sitecustomize.py",
}


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def recompute_complete_direct_summary(
    runtime_dir: Path,
    bundle_dir: Path,
    bundle_summary: dict[str, Any],
    baseline: dict[str, Any],
    audit_path: Path,
    summary: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Upgrade a legacy summary only when the complete direct record set validates."""
    if (
        summary.get("format") != "rq1-direct-mutant-classification/v1"
        or summary.get("task_id") != bundle_summary.get("task_id")
        or summary.get("checkpoint_id") != "C1"
        or summary.get("classification_algorithm_version") != DIRECT_CLASSIFIER_VERSION
        or summary.get("classification_config_sha256") != config.get("classification_config_sha256")
        or summary.get("baseline_audit_sha256") != sha256_file(audit_path)
        or config.get("baseline_audit_sha256") != sha256_file(audit_path)
        or config.get("classification_algorithm_version") != DIRECT_CLASSIFIER_VERSION
        or config.get("stable_test_ids") != stable_pass_selection(baseline)
        or summary.get("source_sha256") != bundle_summary.get("source_sha256")
        or config.get("source_sha256") != bundle_summary.get("source_sha256")
    ):
        return None

    expected = config.get("mutants")
    if not isinstance(expected, list) or not expected:
        return None
    expected_statuses: dict[str, str] = {}
    for mutant in expected:
        mutant_id = mutant.get("mutant_id")
        if not isinstance(mutant_id, str) or mutant_id in expected_statuses:
            return None
        expected_statuses[mutant_id] = mutant.get("mutmut_status")

    raw_records_path = bundle_dir / bundle_summary.get("records_path", "mutant_records.jsonl")
    direct_records_path = runtime_dir / "direct_mutant_classification.jsonl"
    if not raw_records_path.is_file() or not direct_records_path.is_file():
        return None
    raw_records = read_jsonl(raw_records_path)
    raw_by_id = {record.get("mutant_id"): record for record in raw_records}
    direct_records = read_jsonl(direct_records_path)
    direct_by_id = {record.get("mutant_id"): record for record in direct_records}
    if (
        len(raw_by_id) != len(raw_records)
        or len(direct_by_id) != len(direct_records)
        or set(raw_by_id) != set(expected_statuses)
        or set(direct_by_id) != set(expected_statuses)
        or summary.get("mutant_count") != len(expected_statuses)
        or summary.get("classified_count") != len(expected_statuses)
    ):
        return None

    stable_ids = config["stable_test_ids"]
    mutant_test_ids = config.get("mutant_test_ids", {})
    source_paths = set(config.get("source_paths", []))
    counts: Counter[str] = Counter()
    for mutant_id, direct in direct_by_id.items():
        raw = raw_by_id[mutant_id]
        raw_status = expected_statuses[mutant_id]
        status = direct.get("direct_status")
        matched_tests = mutant_test_ids.get(mutant_id, [])
        no_test_evidence = not matched_tests and raw_status == "no tests"
        expected_tests = [] if no_test_evidence else matched_tests or stable_ids
        expected_method = (
            "mutmut_no_tests_without_subprocess_hit" if no_test_evidence
            else "subprocess_function_hits" if matched_tests
            else "full_stable_suite_fallback"
        )
        changed = direct.get("source_changed")
        if (
            direct.get("mutmut_status") != raw_status
            or raw.get("raw_status") != raw_status
            or status not in {"killed", "survived", "not_tested", "error", "timeout"}
            or direct.get("selected_test_ids") != expected_tests
            or direct.get("test_selection_method") != expected_method
            or not isinstance(changed, dict)
            or set(changed) != source_paths
            or (status in {"killed", "survived"} and (not any(changed.values()) or direct.get("apply_exit_code") != 0 or direct.get("error") is not None))
            or (status in {"error", "timeout"} and not direct.get("error"))
            or (status == "not_tested" and not no_test_evidence)
        ):
            return None
        counts[status] += 1

    if summary.get("status_counts") != dict(counts):
        return None
    repaired = dict(summary)
    repaired.update(
        {
            "complete": True,
            "all_attempts_resolved": all(status in {"killed", "survived", "not_tested"} for status in (row["direct_status"] for row in direct_records)),
            "inconclusive_count": sum(counts[status] for status in ("not_tested", "error", "timeout")),
            "summary_recomputed_at_utc": datetime.now(timezone.utc).isoformat(),
            "completion_semantics": "all expected mutants have exactly one hash-matched terminal record; error, timeout, and not-tested outcomes remain inconclusive",
        }
    )
    return repaired


def find_matching_bundle(
    task: str,
    baseline: dict[str, Any],
    max_children: int,
) -> tuple[Path, dict[str, Any]]:
    task_root = RESULT_ROOT / task / "C1"
    audit_hash = baseline.get("baseline_audit_sha256")
    matches: list[tuple[Path, dict[str, Any]]] = []
    for summary_path in task_root.glob("**/mutation_summary.json"):
        try:
            summary = read_json(summary_path)
        except (OSError, json.JSONDecodeError):
            continue
        if (
            summary.get("format") != "rq1-mutmut-export/v1"
            or summary.get("task_id") != task
            or summary.get("checkpoint_id") != "C1"
            or summary.get("base_tests_sha256") != baseline.get("base_tests_sha256")
            or set(summary.get("mutation_test_selection") or []) != set(stable_pass_selection(baseline))
        ):
            continue
        records_path = summary_path.parent / summary.get("records_path", "mutant_records.jsonl")
        if not records_path.is_file() or sum(1 for line in records_path.read_text(encoding="utf-8").splitlines() if line.strip()) != summary.get("mutant_count"):
            continue
        run_dir = Path(summary.get("runtime_directory", ""))
        if not run_dir.is_absolute():
            run_dir = ROOT / run_dir
        try:
            run_dir = run_dir.resolve(strict=True)
            if not run_dir.is_relative_to((ROOT / "tmp").resolve(strict=True)):
                continue
            runtime_manifest = read_json(run_dir / "runtime_manifest.json")
            mutation_manifest = read_json(run_dir / "mutmut_run_manifest.json")
            recorded_bridge_hashes = {
                name: sha256_file(run_dir / name) for name in MUTATION_BRIDGES
            }
            source_paths = runtime_manifest["task_runtime_profile"]["mutmut"]["source_paths"]
            runtime_sources = {
                relative: sha256_file(run_dir / Path(*relative.split("/")))
                for relative in source_paths
            }
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if runtime_manifest.get("base_tests_sha256") != baseline.get("base_tests_sha256"):
            continue
        if (
            mutation_manifest.get("mutation_protocol_version") != MUTATION_PROTOCOL_VERSION
            or not mutation_manifest.get("mutation_config_sha256")
            or mutation_manifest.get("mutation_config", {}).get("max_children") != max_children
            or mutation_manifest.get("mutation_config", {}).get("runtime_config_sha256") != sha256_file(RUNTIME_CONFIG)
            or mutation_manifest.get("mutation_config", {}).get("mutmut_version") != "3.8.0"
            or mutation_manifest.get("mutation_config", {}).get("mutant_selection") != []
            or mutation_manifest.get("bridge_sha256") != recorded_bridge_hashes
        ):
            continue
        if summary.get("source_sha256") != runtime_sources:
            continue
        if set(summary.get("mutation_test_selection") or summary.get("direct_classification_test_selection") or []) != set(stable_pass_selection(baseline)):
            continue
        # Prefer a bundle that already contains direct classification evidence.
        priority = int((summary_path.parent / "direct_mutant_classification_summary.json").is_file())
        matches.append((summary_path.parent, {"summary": summary, "runtime_dir": run_dir, "priority": priority}))
    if not matches:
        raise FileNotFoundError(f"{task}: no complete hash-matched C1 mutmut bundle found")
    matches.sort(key=lambda item: (item[1]["priority"], item[0].stat().st_mtime), reverse=True)
    return matches[0]


def process_task(
    task: str,
    baseline: dict[str, Any],
    audit_path: Path,
    timeout_seconds: int,
    output_suffix: str,
    mutant_workers: int,
    max_children: int,
) -> dict[str, Any]:
    bundle_dir, bundle = find_matching_bundle(task, baseline, max_children)
    runtime_dir = bundle["runtime_dir"]
    direct_summary_path = runtime_dir / "direct_mutant_classification_summary.json"
    direct_results_path = runtime_dir / "direct_mutant_classification.jsonl"
    direct_config_path = runtime_dir / "direct_mutant_classification_config.json"
    reuse_direct = False
    direct_summary_recomputed = False
    if direct_summary_path.is_file() and direct_results_path.is_file() and direct_config_path.is_file():
        direct_summary = read_json(direct_summary_path)
        direct_config = read_json(direct_config_path)
        if direct_summary.get("complete") is not True:
            repaired = recompute_complete_direct_summary(
                runtime_dir,
                bundle_dir,
                bundle["summary"],
                baseline,
                audit_path,
                direct_summary,
                direct_config,
            )
            if repaired is not None:
                direct_summary_path.write_text(json.dumps(repaired, indent=2) + "\n", encoding="utf-8")
                direct_summary = repaired
                direct_summary_recomputed = True
        reuse_direct = (
            direct_summary.get("complete") is True
            and direct_summary.get("classification_algorithm_version") == DIRECT_CLASSIFIER_VERSION
            and direct_summary.get("classification_config_sha256") == direct_config.get("classification_config_sha256")
            and direct_summary.get("source_sha256") == bundle["summary"].get("source_sha256")
            and direct_config.get("stable_test_ids") == stable_pass_selection(baseline)
        )
    if not reuse_direct:
        command = [
            sys.executable,
            str(CLASSIFIER),
            "--runtime-dir",
            str(runtime_dir),
            "--baseline-audit",
            str(audit_path),
            "--timeout-seconds",
            str(timeout_seconds),
            "--workers",
            str(mutant_workers),
        ]
        if direct_config_path.is_file():
            previous = read_json(direct_config_path)
            if previous.get("stable_test_ids") != stable_pass_selection(baseline):
                raise ValueError(f"{task}: partial direct-classification test selection differs from final C1 audit")
            command.append("--resume")
        elif direct_results_path.is_file():
            raise ValueError(f"{task}: direct results exist without their config; refusing to overwrite the partial classification")
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=(timeout_seconds + 30) * int(bundle["summary"].get("mutant_count", 0)) + 1200,
        )
        (bundle_dir / "cohort-direct.stdout.log").write_text(result.stdout, encoding="utf-8")
        (bundle_dir / "cohort-direct.stderr.log").write_text(result.stderr, encoding="utf-8")
        if result.returncode != 0:
            raise RuntimeError(f"{task}: direct classification failed ({result.returncode}): {result.stderr[-3000:]}")
    direct_summary = read_json(direct_summary_path)
    if direct_summary.get("complete") is not True:
        raise RuntimeError(f"{task}: direct classification is incomplete")

    final_bundle = RESULT_ROOT / task / "C1" / output_suffix
    if not (final_bundle / "mutation_summary.json").is_file():
        command = [
            sys.executable,
            str(MERGER),
            "--runtime-dir",
            str(runtime_dir),
            "--trajectory-id",
            f"rq1_full_36x1/{task}",
            "--output-dir",
            str(final_bundle),
            "--baseline-audit",
            str(audit_path),
        ]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        if result.returncode != 0:
            raise RuntimeError(f"{task}: normalized bundle merge failed ({result.returncode}): {result.stderr[-3000:]}")
    normalized_summary = read_json(final_bundle / "mutation_summary.json")
    if (
        normalized_summary.get("complete") is not True
        or normalized_summary.get("baseline_audit_sha256") != sha256_file(audit_path)
        or normalized_summary.get("status_counts") != direct_summary.get("status_counts")
    ):
        raise ValueError(f"{task}: normalized direct-mutant bundle does not match current evidence")
    return {
        "task_id": task,
        "checkpoint_id": "C1",
        "status": "complete",
        "mutation_bundle": str(final_bundle.relative_to(ROOT)),
        "runtime_directory": str(runtime_dir),
        "mutant_count": normalized_summary.get("mutant_count"),
        "status_counts": normalized_summary.get("status_counts"),
        "mutation_score": normalized_summary.get("mutation_score"),
        "direct_classification_reused": reuse_direct,
        "direct_summary_recomputed": direct_summary_recomputed,
        "baseline_audit_sha256": sha256_file(audit_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--units", type=Path, default=DEFAULT_UNITS)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--mutant-workers", type=int, default=1, help="Isolated concurrent apply-and-test workers per task")
    parser.add_argument("--max-children", type=int, default=4, help="Required Mutmut worker count for the matching raw bundle")
    parser.add_argument("--output-suffix", default="mutmut_direct_v4_20261115")
    parser.add_argument("--logs-suffix", default=OUTPUT_LOG.name, help="Cohort JSONL filename under artifacts/results/rq1_full_36x1")
    parser.add_argument("--task", action="append", help="Limit to task IDs; may be repeated")
    args = parser.parse_args()
    if args.timeout_seconds < 1 or args.workers < 1 or args.mutant_workers < 1 or args.max_children < 1:
        raise ValueError("timeout and worker count must be positive")
    logs_path = (RESULT_ROOT / args.logs_suffix).resolve()
    if not logs_path.is_relative_to(RESULT_ROOT.resolve()) or logs_path.suffix.lower() != ".jsonl":
        raise ValueError("logs suffix must be a JSONL filename under artifacts/results/rq1_full_36x1")
    audit_path = args.baseline_audit.resolve(strict=True)
    audit = read_json(audit_path)
    records = {row["task_id"]: row for row in audit["records"]}
    units = read_json(args.units.resolve(strict=True))["units"]
    available_tasks = {item["task_id"] for item in units}
    unknown_tasks = sorted(set(args.task or []) - available_tasks)
    if unknown_tasks:
        raise ValueError(f"unknown task ID(s): {unknown_tasks}")
    selected = [unit["task_id"] for unit in units if not args.task or unit["task_id"] in args.task]
    if not selected:
        raise ValueError("no C1 tasks selected")
    logs_path.parent.mkdir(parents=True, exist_ok=True)
    complete = 0
    errors = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        future_to_task = {
            pool.submit(
                process_task,
                task,
                records[task],
                audit_path,
                args.timeout_seconds,
                args.output_suffix,
                args.mutant_workers,
                args.max_children,
            ): task
            for task in selected
        }
        for future in as_completed(future_to_task):
            task = future_to_task[future]
            try:
                row = future.result()
                complete += 1
            except Exception as error:  # Keep a durable cohort record and continue independent tasks.
                row = {
                    "task_id": task,
                    "checkpoint_id": "C1",
                    "status": "error",
                    "error": f"{type(error).__name__}: {error}",
                    "baseline_audit_sha256": sha256_file(audit_path),
                }
                errors += 1
            row["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
            with logs_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(json.dumps(row, ensure_ascii=False), flush=True)
    print(f"Direct C1 mutmut classification: {complete}/{len(selected)} complete; {errors} failed. Log: {logs_path}", flush=True)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

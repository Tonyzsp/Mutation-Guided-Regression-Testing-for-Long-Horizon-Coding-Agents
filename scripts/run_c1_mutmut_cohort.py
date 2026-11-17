"""Complete or reuse same-checkpoint mutmut runs for the RQ1 C1 cohort."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from c1_test_selection import stable_pass_selection
from export_mutant_records import parse_statuses


ROOT = Path(__file__).resolve().parents[1]
UNIT_MANIFEST = ROOT / "config" / "rq1_full_36x1_candidate_units.json"
PACKAGE_ROOT = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
RUNTIME_CONFIG = ROOT / "config" / "checkpoint_runtime.json"
RUNTIME_ROOT = ROOT / "tmp" / "checkpoint-runtime"
RESULT_ROOT = ROOT / "artifacts" / "results" / "rq1_full_36x1"
DEFAULT_BASELINE = RESULT_ROOT / "c1_base_audit_isolation_revalidated_20261019" / "c1_base_suite_audit.json"
RUNNER = ROOT / "scripts" / "run_checkpoint_mutmut.py"
EXPORTER = ROOT / "scripts" / "export_mutant_records.py"
MUTATION_BRIDGES = {
    "mutmut_subprocess_bridge.py": ROOT / "scripts" / "mutmut_subprocess_bridge.py",
    "sitecustomize.py": ROOT / "scripts" / "mutmut_subprocess_sitecustomize.py",
}
MUTATION_PROTOCOL_VERSION = "rq1-mutmut-v4"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_hashes(task: str, baseline_record: dict[str, Any]) -> dict[str, str]:
    package = PACKAGE_ROOT / task / "C1"
    runtime_manifest = read_json(Path(baseline_record["runtime_directory"]) / "runtime_manifest.json")
    profile = runtime_manifest["task_runtime_profile"]["mutmut"]
    workspace = package / read_json(package / "package.json")["workspace_path"]
    return {relative: sha256_file(workspace / Path(*relative.split("/"))) for relative in profile["source_paths"]}


def current_bridge_hashes() -> dict[str, str]:
    return {name: sha256_file(path) for name, path in MUTATION_BRIDGES.items()}


def has_all_mutant_records(path: Path, summary: dict[str, Any]) -> bool:
    if not path.is_file() or not isinstance(summary.get("mutant_count"), int):
        return False
    try:
        count = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return False
    return count == summary["mutant_count"] and count > 0


def raw_run_matches(
    run_dir: Path,
    task: str,
    baseline_record: dict[str, Any],
    sources: dict[str, str],
    stable_ids: list[str],
    max_children: int,
) -> bool:
    """Accept a finished run when its saved mutant listing is complete and hash-matched."""
    try:
        run_dir = run_dir.resolve(strict=True)
        allowed_root = (RUNTIME_ROOT / task).resolve(strict=True)
        if not run_dir.is_relative_to(allowed_root):
            return False
        manifest = read_json(run_dir / "mutmut_run_manifest.json")
        results_path = run_dir / "mutmut-results.txt"
        if not results_path.is_file() or results_path.stat().st_size == 0:
            return False
        results_text = results_path.read_text(encoding="utf-8")
        statuses = parse_statuses(results_text)
        result_counts = dict(sorted(Counter(statuses.values()).items()))
        return (
            manifest.get("format") == "rq1-checkpoint-mutmut-run/v1"
            and manifest.get("task_id") == task
            and manifest.get("checkpoint_id") == "C1"
            and manifest.get("base_tests_sha256") == baseline_record["base_tests_sha256"]
            and manifest.get("source_sha256") == sources
            and manifest.get("mutation_protocol_version") == MUTATION_PROTOCOL_VERSION
            and bool(manifest.get("mutation_config_sha256"))
            and manifest.get("mutation_config", {}).get("runtime_config_sha256") == sha256_file(RUNTIME_CONFIG)
            and manifest.get("mutation_config", {}).get("max_children") == max_children
            and manifest.get("mutation_config", {}).get("mutmut_version") == "3.8.0"
            and manifest.get("mutation_config", {}).get("mutant_selection") == []
            and manifest.get("bridge_sha256") == current_bridge_hashes()
            and set(manifest.get("mutation_test_selection") or []) == set(stable_ids)
            # A complete status listing can still be unusable when the stats phase
            # failed before associating any mutant with a test.
            and set(result_counts) != {"not checked"}
            and isinstance(manifest.get("mutmut_exit_code"), int)
            and manifest.get("results_exit_code") == 0
            and result_counts == manifest.get("result_counts")
            and (
                manifest.get("results_sha256") is None
                or manifest.get("results_sha256")
                == hashlib.sha256(results_text.encode("utf-8")).hexdigest()
            )
            and bool(statuses)
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False


def matching_artifact(task: str, baseline_record: dict[str, Any], sources: dict[str, str], max_children: int) -> Path | None:
    task_root = RESULT_ROOT / task / "C1"
    for summary_path in sorted(task_root.glob("**/mutation_summary.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            summary = read_json(summary_path)
        except (OSError, json.JSONDecodeError):
            continue
        if (
            summary.get("format") == "rq1-mutmut-export/v1"
            and summary.get("task_id") == task
            and summary.get("checkpoint_id") == "C1"
            and summary.get("base_tests_sha256", baseline_record["base_tests_sha256"]) == baseline_record["base_tests_sha256"]
            and summary.get("source_sha256") == sources
        ):
            records_path = summary_path.parent / summary.get("records_path", "mutant_records.jsonl")
            if not has_all_mutant_records(records_path, summary):
                continue
            runtime_directory = Path(summary.get("runtime_directory", ""))
            run_manifest_path = summary_path.parent / "mutmut_run_manifest.json"
            if not run_manifest_path.is_file():
                run_manifest_path = runtime_directory / "mutmut_run_manifest.json"
            try:
                run_manifest = read_json(run_manifest_path)
            except (OSError, json.JSONDecodeError):
                continue
            if (
                records_path.is_file()
                and run_manifest.get("base_tests_sha256") == baseline_record.get("base_tests_sha256")
                and run_manifest.get("source_sha256") == sources
                and run_manifest.get("mutation_protocol_version") == MUTATION_PROTOCOL_VERSION
                and bool(run_manifest.get("mutation_config_sha256"))
                and run_manifest.get("mutation_config", {}).get("runtime_config_sha256") == sha256_file(RUNTIME_CONFIG)
                and run_manifest.get("mutation_config", {}).get("max_children") == max_children
                and run_manifest.get("mutation_config", {}).get("mutmut_version") == "3.8.0"
                and run_manifest.get("mutation_config", {}).get("mutant_selection") == []
                and run_manifest.get("bridge_sha256") == current_bridge_hashes()
                and set(run_manifest.get("result_counts", {})) != {"not checked"}
                and (
                    run_manifest.get("mutation_test_selection")
                    or run_manifest.get("direct_classification_test_selection")
                ) is not None
                and set(
                    run_manifest.get("mutation_test_selection")
                    or run_manifest.get("direct_classification_test_selection")
                    or []
                ) == set(stable_pass_selection(baseline_record))
            ):
                return summary_path.parent
    return None


def matching_raw_run(task: str, baseline_record: dict[str, Any], sources: dict[str, str], stable_ids: list[str], max_children: int) -> Path | None:
    matches: list[Path] = []
    for manifest_path in (RUNTIME_ROOT / task).glob("*-mutmut-*/mutmut_run_manifest.json"):
        if raw_run_matches(manifest_path.parent, task, baseline_record, sources, stable_ids, max_children):
            matches.append(manifest_path.parent)
    return max(matches, key=lambda item: item.stat().st_mtime) if matches else None


def export_raw(run_dir: Path, trajectory_id: str, diff_chunk_size: int) -> None:
    summary_path = run_dir / "mutation_summary.json"
    if summary_path.is_file():
        existing_summary = read_json(summary_path)
        existing_records = run_dir / existing_summary.get("records_path", "mutant_records.jsonl")
        if has_all_mutant_records(existing_records, existing_summary):
            return
    result = subprocess.run(
        [
            sys.executable,
            str(EXPORTER),
            "--runtime-dir",
            str(run_dir),
            "--trajectory-id",
            trajectory_id,
            "--allow-incomplete",
            "--diff-chunk-size",
            str(diff_chunk_size),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    (run_dir / "mutant-export.stdout.log").write_text(result.stdout, encoding="utf-8")
    (run_dir / "mutant-export.stderr.log").write_text(result.stderr, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(f"mutant export failed ({result.returncode}): {result.stdout}\n{result.stderr}")


def materialize_bundle(
    source_dir: Path,
    destination: Path,
    baseline_record: dict[str, Any],
    task: str,
    sources: dict[str, str],
    max_children: int,
) -> dict[str, Any]:
    if destination.exists():
        existing_summary_path = destination / "mutation_summary.json"
        existing_manifest_path = destination / "mutmut_run_manifest.json"
        if existing_summary_path.is_file() and existing_manifest_path.is_file():
            existing = read_json(existing_summary_path)
            existing_manifest = read_json(existing_manifest_path)
            if (
                existing.get("format") == "rq1-mutmut-export/v1"
                and existing.get("task_id") == task
                and existing.get("checkpoint_id") == "C1"
                and existing.get("base_tests_sha256") == baseline_record["base_tests_sha256"]
                and existing.get("source_sha256") == sources
                and existing_manifest.get("mutation_protocol_version") == MUTATION_PROTOCOL_VERSION
                and bool(existing_manifest.get("mutation_config_sha256"))
                and existing_manifest.get("mutation_config", {}).get("runtime_config_sha256") == sha256_file(RUNTIME_CONFIG)
                and existing_manifest.get("mutation_config", {}).get("max_children") == max_children
                and existing_manifest.get("mutation_config", {}).get("mutmut_version") == "3.8.0"
                and existing_manifest.get("mutation_config", {}).get("mutant_selection") == []
                and existing_manifest.get("bridge_sha256") == current_bridge_hashes()
                and (
                    existing_manifest.get("mutation_test_selection")
                    or existing_manifest.get("direct_classification_test_selection")
                ) == baseline_record.get("stable_pass_test_ids")
                and has_all_mutant_records(
                    destination / existing.get("records_path", "mutant_records.jsonl"), existing
                )
            ):
                existing["baseline_audit_sha256"] = baseline_record.get("baseline_audit_sha256")
                existing["mutation_test_selection"] = existing_manifest["mutation_test_selection"]
                (existing_summary_path).write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
                return existing
        raise FileExistsError(f"existing mutation bundle does not match the current checkpoint baseline: {destination}")
    destination.mkdir(parents=True, exist_ok=False)
    summary = read_json(source_dir / "mutation_summary.json")
    source_manifest_path = source_dir / "mutmut_run_manifest.json"
    if not source_manifest_path.is_file():
        raise FileNotFoundError(f"mutation run manifest is missing: {source_manifest_path}")
    source_manifest = read_json(source_manifest_path)
    selected_tests = source_manifest.get("mutation_test_selection") or source_manifest.get(
        "direct_classification_test_selection"
    )
    if (
        source_manifest.get("base_tests_sha256") != baseline_record.get("base_tests_sha256")
        or source_manifest.get("source_sha256") != sources
        or source_manifest.get("mutation_protocol_version") != MUTATION_PROTOCOL_VERSION
        or not source_manifest.get("mutation_config_sha256")
        or source_manifest.get("mutation_config", {}).get("runtime_config_sha256") != sha256_file(RUNTIME_CONFIG)
        or source_manifest.get("mutation_config", {}).get("max_children") != max_children
        or source_manifest.get("mutation_config", {}).get("mutmut_version") != "3.8.0"
        or source_manifest.get("mutation_config", {}).get("mutant_selection") != []
        or set(selected_tests or []) != set(stable_pass_selection(baseline_record))
    ):
        raise ValueError(f"{task}: mutmut run does not match the current source, base suite, and stable C1 test selection")
    for name in (
        summary.get("records_path", "mutant_records.jsonl"),
        "mutation_summary.json",
        "mutmut-results.txt",
        "mutmut-subprocess-hits.jsonl",
        "mutmut_run_manifest.json",
        "mutant-export.stdout.log",
        "mutant-export.stderr.log",
    ):
        src = source_dir / name
        if src.is_file():
            target_name = "mutant_records.jsonl" if name == summary.get("records_path") else name
            shutil.copy2(src, destination / target_name)
    copied = read_json(destination / "mutation_summary.json")
    copied["records_path"] = "mutant_records.jsonl"
    copied["base_tests_sha256"] = baseline_record["base_tests_sha256"]
    copied["baseline_audit_sha256"] = baseline_record.get("baseline_audit_sha256")
    copied["mutation_test_selection"] = selected_tests
    (destination / "mutation_summary.json").write_text(json.dumps(copied, indent=2) + "\n", encoding="utf-8")
    return copied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-audit", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--units", type=Path, default=UNIT_MANIFEST)
    parser.add_argument("--max-children", type=int, default=4)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument(
        "--diff-chunk-size",
        type=int,
        default=32,
        help="Mutant diffs per short-lived export container (default: 32)",
    )
    parser.add_argument("--run-suffix", default="mutmut_v4_20261115")
    parser.add_argument("--logs-suffix", default="mutmut_v4_cohort_20261115")
    parser.add_argument("--direct-output-suffix", default="mutmut_direct_v4_20261115")
    parser.add_argument(
        "--task-timeout-seconds",
        type=int,
        default=None,
        help="Maximum wall time for one complete mutmut task (default: at least 12 hours)",
    )
    parser.add_argument("--task", action="append", help="Limit to task IDs; may be repeated")
    args = parser.parse_args()
    if args.task_timeout_seconds is None:
        args.task_timeout_seconds = max(12 * 60 * 60, args.timeout_seconds * 4)
    if args.max_children < 1 or args.timeout_seconds < 1 or args.task_timeout_seconds < 1 or args.diff_chunk_size < 1:
        raise ValueError("max children, timeout values, and diff chunk size must be positive")

    audit_path = args.baseline_audit.resolve(strict=True)
    audit = read_json(audit_path)
    records = {item["task_id"]: item for item in audit["records"]}
    units = read_json(args.units.resolve(strict=True))["units"]
    available_tasks = {item["task_id"] for item in units}
    unknown_tasks = sorted(set(args.task or []) - available_tasks)
    if unknown_tasks:
        raise ValueError(f"unknown task ID(s): {unknown_tasks}")
    selected = [item for item in units if not args.task or item["task_id"] in args.task]
    if not selected:
        raise ValueError("no C1 tasks selected")
    cohort_root = RESULT_ROOT / args.logs_suffix
    cohort_root.mkdir(parents=True, exist_ok=True)
    log_path = cohort_root / "cohort_runs.jsonl"
    existing_lines = log_path.read_text(encoding="utf-8").splitlines() if log_path.is_file() else []
    completed: dict[str, dict[str, Any]] = {}
    for line in existing_lines:
        if line.strip():
            row = json.loads(line)
            if row.get("baseline_audit_sha256") == sha256_file(audit_path) and row.get("status") == "complete":
                completed[row["task_id"]] = row

    for index, unit in enumerate(selected, 1):
        task = unit["task_id"]
        baseline = records[task]
        out = RESULT_ROOT / task / "C1" / args.run_suffix
        direct_out = RESULT_ROOT / task / "C1" / args.direct_output_suffix
        if task in completed and (out.is_dir() or direct_out.is_dir()):
            print(f"[{index}/{len(selected)}] REUSE {task}: already complete", flush=True)
            continue
        if direct_out.is_dir():
            try:
                direct_summary = read_json(direct_out / "mutation_summary.json")
                direct_manifest_path = direct_out / "mutmut_run_manifest.json"
                direct_manifest = read_json(direct_manifest_path)
                direct_config = read_json(direct_out / "direct_mutant_classification_config.json")
                direct_ready = (
                    direct_summary.get("format") == "rq1-mutmut-export/v1"
                    and direct_summary.get("task_id") == task
                    and direct_summary.get("checkpoint_id") == "C1"
                    and direct_summary.get("complete") is True
                    and direct_summary.get("base_tests_sha256") == baseline.get("base_tests_sha256")
                    and direct_summary.get("source_sha256") == source_hashes(task, baseline)
                    and direct_summary.get("baseline_audit_sha256") == sha256_file(audit_path)
                    and direct_summary.get("direct_classification_algorithm_version") == "4"
                    and direct_summary.get("direct_classification_config_sha256") == direct_config.get("classification_config_sha256")
                    and direct_manifest.get("mutation_protocol_version") == MUTATION_PROTOCOL_VERSION
                    and direct_manifest.get("mutation_config", {}).get("runtime_config_sha256") == sha256_file(RUNTIME_CONFIG)
                    and direct_manifest.get("mutation_config", {}).get("max_children") == args.max_children
                    and (
                        direct_manifest.get("mutation_test_selection")
                        or direct_manifest.get("direct_classification_test_selection")
                    ) is not None
                and set(
                    direct_manifest.get("mutation_test_selection")
                    or direct_manifest.get("direct_classification_test_selection")
                    or []
                ) == set(stable_pass_selection(baseline))
                )
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                direct_ready = False
            if direct_ready:
                row = {
                    "task_id": task,
                    "checkpoint_id": "C1",
                    "status": "complete",
                    "mutation_bundle": str(direct_out.relative_to(ROOT)),
                    "mutant_count": direct_summary.get("mutant_count"),
                    "status_counts": direct_summary.get("status_counts"),
                    "baseline_audit_sha256": sha256_file(audit_path),
                    "direct_classification_reused": True,
                }
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                print(f"[{index}/{len(selected)}] REUSE {task}: direct-classified bundle", flush=True)
                continue
        if baseline.get("status") not in {"stable_pass", "stable_with_failures"} or baseline.get("unstable_test_ids"):
            row = {"task_id": task, "status": "baseline_ineligible", "error": f"baseline status {baseline.get('status')!r}"}
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({**row, "baseline_audit_sha256": sha256_file(audit_path)}) + "\n")
            print(f"[{index}/{len(selected)}] SKIP {task}: {row['error']}", flush=True)
            continue
        if not baseline.get("stable_pass_test_ids"):
            row = {"task_id": task, "status": "no_stable_passing_tests", "error": "baseline audit has no stable passing test IDs"}
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({**row, "baseline_audit_sha256": sha256_file(audit_path)}) + "\n")
            print(f"[{index}/{len(selected)}] SKIP {task}: no stable passing tests", flush=True)
            continue

        sources = source_hashes(task, baseline)
        try:
            source_dir = matching_artifact(task, baseline, sources, args.max_children)
            if source_dir is None:
                source_dir = matching_raw_run(task, baseline, sources, baseline["stable_pass_test_ids"], args.max_children)
            if source_dir is None:
                runtime_dir = Path(baseline["runtime_directory"])
                command = [
                    sys.executable, str(RUNNER),
                    "--runtime-dir", str(runtime_dir),
                    "--baseline-audit", str(audit_path),
                    "--max-children", str(args.max_children),
                ]
                print(f"[{index}/{len(selected)}] RUN {task}", flush=True)
                result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=args.task_timeout_seconds)
                (cohort_root / f"{task}.runner.stdout.log").write_text(result.stdout, encoding="utf-8")
                (cohort_root / f"{task}.runner.stderr.log").write_text(result.stderr, encoding="utf-8")
                manifest_line = next(
                    (line for line in result.stdout.splitlines() if line.startswith("Run manifest: ")),
                    None,
                )
                if manifest_line is None:
                    raise RuntimeError(
                        f"mutmut runner exited {result.returncode} without a run manifest: {result.stderr[-4000:]}"
                    )
                reported_manifest = Path(manifest_line.split(": ", 1)[1]).resolve(strict=True)
                source_dir = reported_manifest.parent
                if result.returncode != 0 and not raw_run_matches(
                    source_dir, task, baseline, sources, baseline["stable_pass_test_ids"], args.max_children
                ):
                    raise RuntimeError(
                        f"mutmut runner exited {result.returncode}; saved mutant results did not pass protocol/hash validation: "
                        f"{result.stderr[-4000:]}"
                    )
                if result.returncode == 0 and not raw_run_matches(
                    source_dir, task, baseline, sources, baseline["stable_pass_test_ids"], args.max_children
                ):
                    raise RuntimeError("mutmut runner completed but its saved results failed protocol/hash validation")
            export_raw(source_dir, unit["trajectory_id"], args.diff_chunk_size)
            baseline["baseline_audit_sha256"] = sha256_file(audit_path)
            copied = materialize_bundle(source_dir, out, baseline, task, sources, args.max_children)
            status = "complete" if copied.get("complete") is True else "incomplete_mutation_statuses"
            row = {
                "task_id": task,
                "checkpoint_id": "C1",
                "status": status,
                "mutation_bundle": str(out.relative_to(ROOT)),
                "mutant_count": copied.get("mutant_count"),
                "status_counts": copied.get("status_counts"),
                "baseline_audit_sha256": sha256_file(audit_path),
            }
        except Exception as error:
            row = {"task_id": task, "checkpoint_id": "C1", "status": "error", "error": f"{type(error).__name__}: {error}", "baseline_audit_sha256": sha256_file(audit_path)}
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"[{index}/{len(selected)}] {task}: {row['status']}", flush=True)

    print(f"C1 mutmut cohort log: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

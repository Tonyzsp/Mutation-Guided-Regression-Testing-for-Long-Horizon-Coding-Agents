"""Run missing legal C1 base-suite coverage jobs for the RQ1 cohort."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
RUNTIME_ROOT = ROOT / "tmp" / "checkpoint-runtime"
RESULT_ROOT = ROOT / "artifacts" / "results" / "rq1_full_36x1"
BASELINE_AUDIT = RESULT_ROOT / "c1_base_audit_isolation_revalidated_20261019" / "c1_base_suite_audit.json"
COVERAGE_RUNNER = ROOT / "scripts" / "run_checkpoint_coverage.py"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def package_source_hashes(task: str, record: dict[str, Any], runtime: dict[str, Any]) -> dict[str, str]:
    package = PACKAGE_ROOT / task / record["checkpoint_id"]
    workspace = package / "workspace"
    paths = runtime.get("task_runtime_profile", {}).get("mutmut", {}).get("source_paths", [])
    if not paths:
        raise ValueError(f"runtime profile has no mutation source paths for {task}")
    return {str(relative): sha256_file(workspace / Path(*str(relative).split("/"))) for relative in paths}


def find_runtime(task: str, record: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    options: list[tuple[Path, dict[str, Any]]] = []
    for manifest_path in (RUNTIME_ROOT / task).glob("C1*/runtime_manifest.json"):
        directory = manifest_path.parent
        lowered = directory.name.lower()
        if any(marker in lowered for marker in ("coverage", "mutmut", "direct", "probe", "candidate")):
            continue
        runtime = read_json(manifest_path)
        if (
            runtime.get("task_id") == task
            and runtime.get("checkpoint_id") == record["checkpoint_id"]
            and runtime.get("base_tests_sha256") == record["base_tests_sha256"]
        ):
            options.append((directory, runtime))
    if not options:
        raise ValueError(f"no staged C1 runtime matches the audited base suite for {task}")
    options.sort(key=lambda item: item[0].stat().st_mtime, reverse=True)
    for directory, runtime in options:
        expected = package_source_hashes(task, record, runtime)
        actual = {
            source: sha256_file(directory / Path(*source.split("/")))
            for source in runtime["task_runtime_profile"]["mutmut"]["source_paths"]
        }
        if actual == expected:
            return directory, runtime
    raise ValueError(f"no staged C1 runtime has source hashes matching the package for {task}")


def completed_coverage(task: str, record: dict[str, Any], source_hashes: dict[str, str]) -> Path | None:
    checkpoint_root = RESULT_ROOT / task / record["checkpoint_id"]
    for manifest_path in sorted(checkpoint_root.glob("coverage_*/coverage_run_manifest.json"), reverse=True):
        bundle = manifest_path.parent
        coverage_path = bundle / "coverage.json"
        try:
            manifest = read_json(manifest_path)
        except (OSError, json.JSONDecodeError):
            continue
        if (
            coverage_path.is_file()
            and manifest.get("format") == "rq1-checkpoint-coverage/v1"
            and manifest.get("task_id") == task
            and manifest.get("checkpoint_id") == record["checkpoint_id"]
            and manifest.get("base_tests_sha256") == record["base_tests_sha256"]
            and manifest.get("source_sha256") == source_hashes
            and manifest.get("total_line_coverage_percent") is not None
        ):
            return bundle
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--force", action="store_true", help="Re-run coverage even when a hash-matched report exists")
    args = parser.parse_args()
    audit = read_json(BASELINE_AUDIT)
    records = {item["task_id"]: item for item in audit["records"]}
    batch_records: list[dict[str, Any]] = []
    failures = 0

    for index, (task, record) in enumerate(sorted(records.items()), 1):
        runtime_dir, runtime = find_runtime(task, record)
        source_hashes = package_source_hashes(task, record, runtime)
        existing = None if args.force else completed_coverage(task, record, source_hashes)
        if existing:
            batch_records.append({
                "task_id": task,
                "checkpoint_id": record["checkpoint_id"],
                "status": "reused",
                "coverage_bundle": str(existing.relative_to(ROOT)),
                "source_sha256": source_hashes,
            })
            print(f"[{index}/{len(records)}] REUSE {task}: {existing}", flush=True)
            continue

        output_dir = RESULT_ROOT / task / record["checkpoint_id"] / "coverage_cohort_20261019"
        if output_dir.exists():
            output_dir = RESULT_ROOT / task / record["checkpoint_id"] / f"coverage_cohort_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            str(COVERAGE_RUNNER),
            "--runtime-dir",
            str(runtime_dir),
            "--output-dir",
            str(output_dir),
            "--timeout-seconds",
            str(args.timeout_seconds),
        ]
        print(f"[{index}/{len(records)}] RUN {task} using {runtime_dir.name}", flush=True)
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "cohort-runner.stdout.log").write_text(result.stdout, encoding="utf-8")
            (output_dir / "cohort-runner.stderr.log").write_text(result.stderr, encoding="utf-8")
            run_manifest_path = output_dir / "coverage_run_manifest.json"
            coverage_manifest = read_json(run_manifest_path) if run_manifest_path.is_file() else {}
            status = "complete" if (
                (output_dir / "coverage.json").is_file()
                and coverage_manifest.get("total_line_coverage_percent") is not None
            ) else "failed"
            if status == "failed":
                failures += 1
            batch_records.append({
                "task_id": task,
                "checkpoint_id": record["checkpoint_id"],
                "status": status,
                "runner_exit_code": result.returncode,
                "runtime_directory": str(runtime_dir),
                "coverage_bundle": str(output_dir.relative_to(ROOT)),
                "line_coverage_percent": coverage_manifest.get("total_line_coverage_percent"),
                "branch_coverage_percent": coverage_manifest.get("total_branch_coverage_percent"),
                "source_sha256": source_hashes,
            })
            print(f"[{index}/{len(records)}] {status.upper()} {task}: exit={result.returncode}, coverage={coverage_manifest.get('total_line_coverage_percent')}", flush=True)
        except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
            failures += 1
            batch_records.append({
                "task_id": task,
                "checkpoint_id": record["checkpoint_id"],
                "status": "failed",
                "runtime_directory": str(runtime_dir),
                "error": str(error),
            })
            print(f"[{index}/{len(records)}] FAILED {task}: {error}", flush=True)

        manifest_path = RESULT_ROOT / "c1_coverage_cohort_20261019.json"
        manifest_path.write_text(json.dumps({
            "format": "rq1-c1-coverage-cohort/v1",
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "baseline_audit_sha256": sha256_file(BASELINE_AUDIT),
            "task_count": len(records),
            "records": batch_records,
        }, indent=2) + "\n", encoding="utf-8")

    manifest_path = RESULT_ROOT / "c1_coverage_cohort_20261019.json"
    manifest_path.write_text(json.dumps({
        "format": "rq1-c1-coverage-cohort/v1",
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "baseline_audit_sha256": sha256_file(BASELINE_AUDIT),
        "task_count": len(records),
        "records": batch_records,
    }, indent=2) + "\n", encoding="utf-8")
    complete = sum(item["status"] in {"complete", "reused"} for item in batch_records)
    print(f"Coverage cohort: {complete}/{len(records)} reports available; {failures} failed.", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run each sanitized C1 base suite repeatedly and record per-test outcomes."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UNITS = REPO_ROOT / "config" / "rq1_full_36x1_candidate_units.json"
DEFAULT_PACKAGE_ROOT = REPO_ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
PREPARE_SCRIPT = REPO_ROOT / "scripts" / "prepare_checkpoint_runtime.py"
CONTAINER_WORKSPACE = "/workspace"
PYTEST_JSON_REPORT = "/workspace/rq1_baseline_report.json"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def docker_image_id(image: str) -> str:
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def prepare_runtime(package: Path, problems_root: Path) -> tuple[Path, dict[str, Any]]:
    result = subprocess.run(
        [
            sys.executable,
            str(PREPARE_SCRIPT),
            "--package",
            str(package),
            "--problems-root",
            str(problems_root),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"runtime staging failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
    marker = "Prepared isolated checkpoint context: "
    stage_line = next((line for line in result.stdout.splitlines() if line.startswith(marker)), None)
    if stage_line is None:
        raise RuntimeError("runtime staging output did not identify its staging directory")
    stage = Path(stage_line[len(marker) :]).resolve(strict=True)
    return stage, read_json(stage / "runtime_manifest.json")


def baseline_command(stage: Path, manifest: dict[str, Any], image: str) -> list[str]:
    argv = [
        "docker",
        "run",
        "--rm",
        "--workdir",
        CONTAINER_WORKSPACE,
        "--mount",
        f"type=bind,source={stage},target={CONTAINER_WORKSPACE}",
        "--env",
        f"PYTHONPATH={CONTAINER_WORKSPACE}",
    ]
    for mount in manifest.get("asset_mounts_read_only", []):
        target = str(mount["target"])
        if not target.startswith(CONTAINER_WORKSPACE.rstrip("/") + "/"):
            raise ValueError(f"asset mount escapes container workspace: {target}")
        argv.extend(["--mount", f"type=bind,source={mount['source']},target={target},readonly"])

    command = list(manifest["test_command_argv"])
    command.extend(["--json-report", f"--json-report-file={PYTEST_JSON_REPORT}"])
    shell = [shlex.join(list(setup)) for setup in manifest.get("container_setup_commands", [])]
    shell.append(shlex.join(command))
    argv.extend([image, "sh", "-lc", " && ".join(shell)])
    return argv


def run_repetition(stage: Path, manifest: dict[str, Any], image: str, timeout: int) -> dict[str, Any]:
    report_path = stage / "rq1_baseline_report.json"
    if report_path.exists():
        report_path.unlink()
    try:
        result = subprocess.run(
            baseline_command(stage, manifest, image),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        return {
            "exit_code": None,
            "timed_out": True,
            "stdout": error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or ""),
            "stderr": error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or ""),
            "tests": [],
            "report_error": "pytest execution timed out before a complete JSON report",
        }

    stdout_path = stage / "rq1_baseline.stdout.log"
    stderr_path = stage / "rq1_baseline.stderr.log"
    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    tests: list[dict[str, str]] = []
    report_error = None
    if report_path.is_file():
        try:
            report = read_json(report_path)
            tests = [
                {"nodeid": str(item["nodeid"]), "outcome": str(item["outcome"])}
                for item in report.get("tests", [])
                if "nodeid" in item and "outcome" in item
            ]
        except (OSError, KeyError, json.JSONDecodeError) as error:
            report_error = f"cannot parse pytest JSON report: {error}"
    else:
        report_error = "pytest JSON report was not produced"
    return {
        "exit_code": result.returncode,
        "timed_out": False,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "tests": tests,
        "report_error": report_error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--units", type=Path, default=DEFAULT_UNITS)
    parser.add_argument("--package-root", type=Path, default=DEFAULT_PACKAGE_ROOT)
    parser.add_argument("--problems-root", type=Path, default=REPO_ROOT / "external" / "scb-problems")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--task", action="append", help="Limit to task IDs; may be repeated")
    args = parser.parse_args()
    if args.repeats < 1 or args.timeout_seconds < 1:
        raise ValueError("repeats and timeout must be positive")

    units_manifest = read_json(args.units.resolve(strict=True))
    package_root = args.package_root.resolve(strict=True)
    problems_root = args.problems_root.resolve(strict=True)
    selected = [unit for unit in units_manifest["units"] if not args.task or unit["task_id"] in args.task]
    if not selected:
        raise ValueError("no candidate units selected")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output_dir.resolve() if args.output_dir else (
        REPO_ROOT / "artifacts" / "results" / "rq1_full_36x1" / f"c1_base_audit_{stamp}"
    )
    allowed_results = (REPO_ROOT / "artifacts" / "results").resolve(strict=True)
    if not output.is_relative_to(allowed_results) or output.exists():
        raise ValueError(f"output must be a new directory beneath {allowed_results}")
    output.mkdir(parents=True)

    records: list[dict[str, Any]] = []
    image = "rq1-b-tools:python312"
    image_id = docker_image_id(image)
    for index, unit in enumerate(selected, start=1):
        task_id = unit["task_id"]
        package = (package_root / unit["source_package"]).resolve(strict=True)
        record: dict[str, Any] = {
            "unit_id": unit["unit_id"],
            "task_id": task_id,
            "checkpoint_id": unit["source_checkpoint"],
            "package_json_sha256": unit["source_package_json_sha256"],
            "base_tests_sha256": unit["base_tests_sha256"],
            "status": "runtime_error",
            "repetitions": [],
        }
        try:
            stage, runtime = prepare_runtime(package, problems_root)
            runtime["docker_image_id"] = image_id
            (stage / "runtime_manifest.json").write_text(json.dumps(runtime, indent=2) + "\n", encoding="utf-8")
            record["runtime_directory"] = str(stage)
            repetition_records = []
            for repetition in range(1, args.repeats + 1):
                result = run_repetition(stage, runtime, image, args.timeout_seconds)
                repetition_records.append(
                    {
                        "repetition": repetition,
                        "exit_code": result["exit_code"],
                        "timed_out": result["timed_out"],
                        "tests": result["tests"],
                        "report_error": result["report_error"],
                    }
                )
                (output / f"{task_id}-run-{repetition}.stdout.log").write_text(result["stdout"], encoding="utf-8")
                (output / f"{task_id}-run-{repetition}.stderr.log").write_text(result["stderr"], encoding="utf-8")
                if (stage / "rq1_baseline_report.json").is_file():
                    (output / f"{task_id}-run-{repetition}.json").write_bytes((stage / "rq1_baseline_report.json").read_bytes())
            record["repetitions"] = repetition_records

            test_maps = [
                {item["nodeid"]: item["outcome"] for item in repetition["tests"]}
                for repetition in repetition_records
                if not repetition["report_error"] and not repetition["timed_out"]
            ]
            infra_reasons = []
            for repetition in repetition_records:
                exit_code = repetition.get("exit_code")
                if repetition.get("timed_out"):
                    infra_reasons.append(f"run {repetition['repetition']} timed out")
                elif repetition.get("report_error"):
                    infra_reasons.append(f"run {repetition['repetition']}: {repetition['report_error']}")
                elif exit_code not in {0, 1}:
                    infra_reasons.append(
                        f"run {repetition['repetition']} exited {exit_code}; expected pytest exit 0 or 1"
                    )
                elif not repetition.get("tests"):
                    infra_reasons.append(f"run {repetition['repetition']} collected no tests")

            if len(test_maps) != args.repeats or infra_reasons:
                record["status"] = "infrastructure_error"
                record["infrastructure_errors"] = infra_reasons
            else:
                all_ids = set().union(*(set(mapping) for mapping in test_maps))
                stable_pass = sorted(nodeid for nodeid in all_ids if all(mapping.get(nodeid) == "passed" for mapping in test_maps))
                stable_fail = sorted(nodeid for nodeid in all_ids if all(mapping.get(nodeid) == "failed" for mapping in test_maps))
                stable_skip = sorted(nodeid for nodeid in all_ids if all(mapping.get(nodeid) == "skipped" for mapping in test_maps))
                unstable = sorted(
                    nodeid
                    for nodeid in all_ids
                    if len({mapping.get(nodeid, "missing") for mapping in test_maps}) > 1
                )
                record["stable_pass_test_ids"] = stable_pass
                record["stable_fail_test_ids"] = stable_fail
                record["stable_skip_test_ids"] = stable_skip
                record["unstable_test_ids"] = unstable
                record["status"] = "stable_with_failures" if stable_fail and not unstable else (
                    "stable_pass" if not stable_fail and not unstable else "unstable"
                )
            records.append(record)
            print(
                f"[{index}/{len(selected)}] {task_id}: {record['status']} "
                f"(pass={len(record.get('stable_pass_test_ids', []))}, "
                f"fail={len(record.get('stable_fail_test_ids', []))}, "
                f"unstable={len(record.get('unstable_test_ids', []))})",
                flush=True,
            )
        except Exception as error:  # Preserve per-unit failures and continue the cohort audit.
            record["error"] = f"{type(error).__name__}: {error}"
            records.append(record)
            print(f"[{index}/{len(selected)}] {task_id}: runtime_error ({record['error']})", flush=True)

        snapshot = {
            "format": "rq1-c1-base-suite-audit/v1",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "dataset": units_manifest["dataset"],
            "repeats": args.repeats,
            "docker_image": image,
            "docker_image_id": image_id,
            "package_root": str(package_root),
            "problem_commit": read_json(REPO_ROOT / "config" / "benchmarks.lock.json")["benchmarks"]["slop_code_bench_problems"]["commit"],
            "records": records,
        }
        (output / "c1_base_suite_audit.json").write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    counts: dict[str, int] = {}
    for record in records:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    print(f"Audit summary: {json.dumps(counts, sort_keys=True)}")
    print(f"Audit record: {output / 'c1_base_suite_audit.json'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

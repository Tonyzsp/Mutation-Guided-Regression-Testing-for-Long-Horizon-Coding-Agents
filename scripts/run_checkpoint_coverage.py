"""Measure the checkpoint's legal base suite, including Python subprocesses."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTAINER_WORKSPACE = "/workspace"
PYTHON_BIN = "/opt/mutmut-env/bin/python"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def filesystem_path(path: Path) -> str:
    """Use Windows extended-length paths for deep benchmark fixture trees."""
    value = os.path.abspath(os.fspath(path))
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        return "\\\\?\\" + value
    return value


def copy_clean_runtime(source: Path, destination: Path) -> None:
    ignored = {
        "mutants", ".venv", ".pytest_cache", ".coverage", "coverage.json",
        "coverage-report.txt", "coverage-run.log", "coverage_run_manifest.json",
        "mutmut-run.log", "mutmut-results.txt", "mutmut_run_manifest.json",
        "mutmut-subprocess-hits.jsonl", "mutmut_subprocess_bridge.py", "sitecustomize.py",
    }

    def ignore(_: str, names: list[str]) -> set[str]:
        return {name for name in names if name in ignored or name.startswith(".coverage") or name == "__pycache__"}

    shutil.copytree(filesystem_path(source), filesystem_path(destination), ignore=ignore)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path, help="Result bundle under artifacts/results")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args()

    source_runtime = args.runtime_dir.resolve(strict=True)
    allowed_tmp = (REPO_ROOT / "tmp").resolve(strict=True)
    if not source_runtime.is_relative_to(allowed_tmp):
        raise ValueError(f"runtime directory must remain under {allowed_tmp}")
    manifest_path = source_runtime / "runtime_manifest.json"
    runtime = read_json(manifest_path)
    profile = runtime.get("task_runtime_profile")
    if not isinstance(profile, dict):
        raise ValueError("runtime manifest has no task_runtime_profile; re-stage with the current adapter")
    source_paths = profile.get("mutmut", {}).get("source_paths", [])
    if not source_paths:
        raise ValueError("runtime profile has no application source paths")
    test_command = list(runtime["test_command_argv"])
    try:
        pytest_index = test_command.index("pytest")
    except ValueError as error:
        raise ValueError("runtime test command does not invoke pytest") from error
    pytest_args = test_command[pytest_index + 1 :]
    try:
        entrypoint_index = pytest_args.index("--entrypoint")
        entry_file = str(profile["entry_file"])
    except (ValueError, KeyError) as error:
        raise ValueError("runtime profile or pytest command has no Python entrypoint") from error
    if entrypoint_index + 1 >= len(pytest_args):
        raise ValueError("pytest --entrypoint option has no value")
    coverage_entry_file_template = str(profile.get("coverage_entry_file_template", "{entry_file}"))
    coverage_entry_file = coverage_entry_file_template.format(entry_file=entry_file)
    pytest_args[entrypoint_index + 1] = shlex.join(
        [
            PYTHON_BIN,
            "-m",
            "coverage",
            "run",
            "--parallel-mode",
            "--rcfile=/workspace/.coveragerc",
            "--data-file=/workspace/.coverage",
            coverage_entry_file,
        ]
    )
    coverage_command = [
        PYTHON_BIN, "-m", "coverage", "run", "--rcfile=/workspace/.coveragerc",
        "--data-file=/workspace/.coverage",
        "-m", "pytest", *pytest_args,
    ]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stage = source_runtime.parent / f"{source_runtime.name}-coverage-{stamp}"
    if not stage.is_relative_to(allowed_tmp) or stage.exists():
        raise ValueError(f"invalid or existing coverage staging path: {stage}")
    copy_clean_runtime(source_runtime, stage)
    source_files = sorted({Path("/workspace") / Path(item) for item in source_paths})
    include_patterns = [source_file.as_posix() for source_file in source_files]
    configured_include_globs = profile.get("coverage_include_globs", [])
    if not isinstance(configured_include_globs, list) or any(
        not isinstance(pattern, str) or not pattern.strip() or ".." in Path(pattern).parts
        for pattern in configured_include_globs
    ):
        raise ValueError("coverage_include_globs must be safe non-empty path patterns")
    include_patterns.extend(configured_include_globs)
    coverage_config = (
        "[run]\n"
        "branch = True\n"
        "parallel = True\n"
        "patch = subprocess\n"
        "sigterm = True\n"
        "relative_files = True\n"
        "include =\n" + "".join(f"    {pattern}\n" for pattern in sorted(set(include_patterns)))
    )
    (stage / ".coveragerc").write_text(coverage_config, encoding="utf-8")

    image = str(runtime["docker_image"])
    command = [
        "docker", "run", "--rm", "--workdir", CONTAINER_WORKSPACE,
        "--mount", f"type=bind,source={stage},target={CONTAINER_WORKSPACE}",
        "--env", f"PYTHONPATH={CONTAINER_WORKSPACE}",
    ]
    for mount in runtime.get("asset_mounts_read_only", []):
        target = str(mount["target"])
        if not target.startswith(CONTAINER_WORKSPACE.rstrip("/") + "/"):
            raise ValueError(f"asset mount is outside workspace: {target}")
        command.extend(["--mount", f"type=bind,source={mount['source']},target={target},readonly"])
    collect_and_report = "\n".join(
        [
            "set +e",
            shlex.join(coverage_command),
            "test_status=$?",
            f"{PYTHON_BIN} -m coverage combine",
            "combine_status=$?",
            f"{PYTHON_BIN} -m coverage report -m > coverage-report.txt",
            "report_status=$?",
            f"{PYTHON_BIN} -m coverage json -o coverage.json",
            "json_status=$?",
            "if [ \"$test_status\" -ne 0 ]; then exit \"$test_status\"; elif [ \"$combine_status\" -ne 0 ]; then exit \"$combine_status\"; elif [ \"$report_status\" -ne 0 ]; then exit \"$report_status\"; else exit \"$json_status\"; fi",
        ]
    )
    container_commands = [
        *(shlex.join(list(item)) for item in runtime.get("container_setup_commands", [])),
        collect_and_report,
    ]
    command.extend([image, "sh", "-lc", " && ".join(container_commands)])
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=args.timeout_seconds,
        check=False,
    )
    (stage / "coverage-run.log").write_text(result.stdout + result.stderr, encoding="utf-8")

    if args.output_dir is None:
        output = REPO_ROOT / "artifacts" / "results" / "rq1_full_36x1" / runtime["task_id"] / runtime["checkpoint_id"] / f"coverage_{stamp}"
    else:
        output = args.output_dir.resolve()
    allowed_results = (REPO_ROOT / "artifacts" / "results").resolve(strict=True)
    if not output.is_relative_to(allowed_results) or output.exists():
        raise ValueError(f"coverage output must be a new directory under {allowed_results}")
    output.mkdir(parents=True)
    for path in stage.iterdir():
        if path.is_file() and (
            path.name == ".coveragerc"
            or path.name == "coverage.json"
            or path.name == "coverage-report.txt"
            or path.name == "coverage-run.log"
            or path.name.startswith(".coverage")
        ):
            shutil.copy2(path, output / path.name)

    coverage_data_path = stage / "coverage.json"
    coverage_totals: dict[str, Any] = {}
    if coverage_data_path.is_file():
        coverage_totals = read_json(coverage_data_path).get("totals", {})
    record = {
        "format": "rq1-checkpoint-coverage/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "task_id": runtime["task_id"],
        "checkpoint_id": runtime["checkpoint_id"],
        "source_package": runtime["source_package"],
        "workspace_revision": runtime["workspace_revision"],
        "base_tests_sha256": runtime["base_tests_sha256"],
        "docker_image": image,
        "source_paths": source_paths,
        "source_sha256": {
            relative: sha256_file(stage / Path(*relative.split("/"))) for relative in source_paths
        },
        "coverage_config_sha256": sha256_file(stage / ".coveragerc"),
        "command": coverage_command,
        "container_exit_code": result.returncode,
        "total_line_coverage_percent": coverage_totals.get("percent_covered"),
        "total_branch_coverage_percent": coverage_totals.get("percent_branches_covered"),
        "coverage_json": "coverage.json" if (output / "coverage.json").is_file() else None,
        "report": "coverage-report.txt" if (output / "coverage-report.txt").is_file() else None,
        "run_log": "coverage-run.log",
        "staging_directory": str(stage),
    }
    (output / "coverage_run_manifest.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"Coverage exit code: {result.returncode}")
    print(f"Line coverage: {record['total_line_coverage_percent']}")
    if result.returncode == 0 and record["total_line_coverage_percent"] is None:
        print("ERROR: coverage completed without a machine-readable total.", file=sys.stderr)
        return 2
    print(f"Results: {output}")
    if result.returncode != 0:
        tail = (result.stdout + result.stderr).splitlines()[-40:]
        if tail:
            print("\n".join(tail), file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

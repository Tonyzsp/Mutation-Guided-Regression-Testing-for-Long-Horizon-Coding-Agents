"""Run mutmut against a staged checkpoint with subprocess-aware hit tracking."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_CONFIG = REPO_ROOT / "config" / "checkpoint_runtime.json"
CONTAINER_WORKSPACE = "/workspace"
MUTMUT_BIN = "/opt/mutmut-env/bin/mutmut"
MUTMUT_HITS_PATH = f"{CONTAINER_WORKSPACE}/mutmut-subprocess-hits.jsonl"


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


def safe_relative_path(value: str, *, field: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(f"{field} must be a safe relative path: {value!r}")
    return path


def replace_mutmut_table(pyproject: Path, table: str) -> None:
    lines = pyproject.read_text(encoding="utf-8").splitlines()
    starts = [index for index, line in enumerate(lines) if line.strip() == "[tool.mutmut]"]
    if len(starts) > 1:
        raise ValueError(f"multiple [tool.mutmut] tables in {pyproject}")

    if starts:
        start = starts[0]
        end = start + 1
        while end < len(lines):
            candidate = lines[end].strip()
            is_table = candidate.startswith("[") and candidate.endswith("]")
            is_mutmut_subtable = candidate.startswith("[tool.mutmut.") or candidate.startswith(
                "[[tool.mutmut."
            )
            if is_table and not is_mutmut_subtable:
                break
            end += 1
        del lines[start:end]

    content = "\n".join(lines).rstrip()
    pyproject.write_text(f"{content}\n\n{table.rstrip()}\n", encoding="utf-8")


def copy_to_clean_mutmut_workspace(source: Path, destination: Path) -> None:
    ignored_names = {
        "mutants",
        "tests",  # A previous mutmut alias; use the package-declared base_tests instead.
        ".venv",
        ".coverage",
        ".coverage-bin",
        ".pytest_cache",
        "mutmut-run.log",
        "mutmut-results.txt",
        "mutmut_run_manifest.json",
        "mutmut-subprocess-hits.jsonl",
        "mutmut-subprocess-smoke.log",
        "mutmut-subprocess-smoke-module-entry.log",
        "mutmut-run-import-entry.log",
        "coverage-report.txt",
        "trial-report.md",
        "mutmut_subprocess_bridge.py",
        "sitecustomize.py",
    }

    def ignore(directory: str, names: list[str]) -> set[str]:
        del directory
        return {
            name
            for name in names
            if name in ignored_names or name.startswith(".coverage.") or name == "__pycache__"
        }

    Path(filesystem_path(destination)).mkdir(parents=True, exist_ok=False)
    shutil.copytree(
        filesystem_path(source),
        filesystem_path(destination),
        dirs_exist_ok=True,
        ignore=ignore,
    )


def docker_image_id(image: str) -> str:
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def build_mutmut_table(
    *,
    source_paths: list[str],
    test_directory: str,
    checkpoint_name: str,
    entrypoint: str,
    pytest_plugin: str,
    debug: bool,
    test_selection: list[str] | None,
) -> str:
    lines = [
        "[tool.mutmut]",
        f"source_paths = {json.dumps(source_paths)}",
        f"also_copy = {json.dumps([test_directory.rstrip('/') + '/', 'mutmut_subprocess_bridge.py'])}",
        f"debug = {str(debug).lower()}",
        "pytest_add_cli_args = "
        + json.dumps(
            [
                "-p",
                pytest_plugin,
                "--entrypoint",
                entrypoint,
                "--checkpoint",
                checkpoint_name,
            ]
        ),
        f"pytest_add_cli_args_test_selection = {json.dumps(test_selection or [test_directory])}",
    ]
    return "\n".join(lines)


def write_run_results(
    runtime_dir: Path,
    *,
    runtime_manifest: dict[str, Any],
    command: list[str],
    run_result: subprocess.CompletedProcess[str],
    results_result: subprocess.CompletedProcess[str] | None,
    source_hashes: dict[str, str],
    bridge_hashes: dict[str, str],
    test_selection: list[str] | None,
    baseline_audit_path: str | None,
) -> dict[str, Any]:
    run_log = runtime_dir / "mutmut-run.log"
    run_log.write_text(
        (run_result.stdout or "") + (run_result.stderr or ""), encoding="utf-8"
    )

    results_text = ""
    results_exit_code: int | None = None
    if results_result is not None:
        results_text = (results_result.stdout or "") + (results_result.stderr or "")
        results_exit_code = results_result.returncode
        (runtime_dir / "mutmut-results.txt").write_text(results_text, encoding="utf-8")

    statuses = re.findall(
        r": (killed|survived|not checked|no tests|skipped|suspicious|timeout|segfault)$",
        results_text,
        flags=re.MULTILINE,
    )
    status_counts: dict[str, int] = {}
    for status in statuses:
        status_counts[status] = status_counts.get(status, 0) + 1

    record = {
        "format": "rq1-checkpoint-mutmut-run/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "task_id": runtime_manifest["task_id"],
        "checkpoint_id": runtime_manifest["checkpoint_id"],
        "runtime_manifest": "runtime_manifest.json",
        "docker_image": runtime_manifest["docker_image"],
        "docker_image_id": runtime_manifest.get("docker_image_id"),
        "mutmut_version": "3.8.0",
        "source_sha256": source_hashes,
        "bridge_sha256": bridge_hashes,
        "command": command,
        "mutmut_exit_code": run_result.returncode,
        "results_exit_code": results_exit_code,
        "result_counts": status_counts,
        "run_log": run_log.name,
        "results_file": "mutmut-results.txt" if results_result is not None else None,
        "subprocess_hits_file": "mutmut-subprocess-hits.jsonl",
        "base_tests_sha256": runtime_manifest.get("base_tests_sha256"),
        "validation_suite_sha256": runtime_manifest.get("validation_suite_sha256"),
        "validation_test_path": runtime_manifest.get("validation_test_path"),
        "validation_test_sha256": runtime_manifest.get("validation_test_sha256"),
        "baseline_audit_path": baseline_audit_path,
        "mutation_test_selection": test_selection,
    }
    (runtime_dir / "mutmut_run_manifest.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-dir",
        required=True,
        type=Path,
        help="Existing staging directory created by prepare_checkpoint_runtime.py",
    )
    parser.add_argument("--max-children", type=int, default=4)
    parser.add_argument("--mutant", action="append", default=[], help="Run only this mutant; repeatable")
    parser.add_argument("--debug", action="store_true", help="Include mutmut's verbose pytest output")
    parser.add_argument(
        "--baseline-audit",
        type=Path,
        help="Optional repeated C1 baseline audit; when supplied, use only consistently passing test IDs",
    )
    args = parser.parse_args()

    source_runtime_dir = args.runtime_dir.resolve(strict=True)
    allowed_root = (REPO_ROOT / "tmp").resolve(strict=True)
    if not source_runtime_dir.is_relative_to(allowed_root):
        raise ValueError(f"runtime directory must remain under {allowed_root}")
    if args.max_children < 1:
        raise ValueError("--max-children must be positive")
    for name in args.mutant:
        if re.fullmatch(r"[A-Za-z0-9_.:-]+", name) is None:
            raise ValueError(f"invalid mutant name: {name!r}")

    runtime_manifest_path = source_runtime_dir / "runtime_manifest.json"
    runtime_manifest = read_json(runtime_manifest_path)
    if runtime_manifest.get("format") != "rq1-checkpoint-runtime/v1":
        raise ValueError(f"unsupported runtime manifest: {runtime_manifest_path}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    runtime_dir = source_runtime_dir.parent / f"{source_runtime_dir.name}-mutmut-{stamp}"
    if not runtime_dir.is_relative_to(allowed_root):
        raise ValueError(f"mutmut workspace must remain under {allowed_root}")
    copy_to_clean_mutmut_workspace(source_runtime_dir, runtime_dir)
    runtime_manifest["source_runtime_directory"] = str(source_runtime_dir)
    runtime_manifest["staging_directory"] = str(runtime_dir)
    (runtime_dir / "runtime_manifest.json").write_text(
        json.dumps(runtime_manifest, indent=2) + "\n", encoding="utf-8"
    )

    runtime_config = read_json(RUNTIME_CONFIG)
    task_profile = runtime_manifest.get("task_runtime_profile")
    if task_profile is None:
        # Backward compatibility for runtime manifests produced by the original C1 adapter.
        task_profile = runtime_config.get("tasks", {}).get(runtime_manifest["task_id"])
    if task_profile is None or "mutmut" not in task_profile:
        raise ValueError(f"no mutmut runtime profile for {runtime_manifest['task_id']!r}")
    mutmut_profile = task_profile["mutmut"]

    baseline_audit_path: str | None = None
    test_selection: list[str] | None = runtime_manifest.get("mutmut_test_selection")
    if args.baseline_audit is not None:
        audit_path = args.baseline_audit.resolve(strict=True)
        allowed_results = (REPO_ROOT / "artifacts" / "results").resolve(strict=True)
        if not audit_path.is_relative_to(allowed_results):
            raise ValueError(f"baseline audit must remain under {allowed_results}")
        audit = read_json(audit_path)
        matching = [
            record for record in audit.get("records", [])
            if record.get("task_id") == runtime_manifest["task_id"]
            and record.get("checkpoint_id") == runtime_manifest["checkpoint_id"]
            and record.get("base_tests_sha256") == runtime_manifest.get("base_tests_sha256")
        ]
        if len(matching) != 1:
            raise ValueError("baseline audit must contain exactly one matching task/checkpoint/base-suite record")
        baseline = matching[0]
        if baseline.get("status") not in {"stable_pass", "stable_with_failures"} or baseline.get("unstable_test_ids"):
            raise ValueError(f"baseline audit is not stable enough for mutation analysis: {baseline.get('status')}")
        selected = baseline.get("stable_pass_test_ids")
        if not isinstance(selected, list) or not selected:
            raise ValueError("baseline audit has no consistently passing test IDs to use for mutation analysis")
        test_selection = [str(item) for item in selected]
        baseline_audit_path = str(audit_path)
    if test_selection is not None and (
        not isinstance(test_selection, list)
        or not test_selection
        or any(not isinstance(item, str) or not item.strip() for item in test_selection)
    ):
        raise ValueError("mutmut_test_selection must be a non-empty list of test node IDs")

    container_workspace = runtime_config["container_workspace"].rstrip("/")
    if container_workspace != CONTAINER_WORKSPACE:
        raise ValueError(f"unsupported container workspace: {container_workspace!r}")

    test_directory = str(task_profile["test_directory"])
    safe_relative_path(test_directory, field="test_directory")
    tests_path = runtime_dir / Path(*PurePosixPath(test_directory).parts)
    if not tests_path.is_dir() or not any(tests_path.glob("conftest.py")):
        raise ValueError(f"base test directory or conftest.py is missing: {tests_path}")

    source_paths = list(mutmut_profile["source_paths"])
    for source_path in source_paths:
        safe = safe_relative_path(source_path, field="source_paths")
        candidate = runtime_dir.joinpath(*safe.parts).resolve(strict=True)
        if not candidate.is_file() or not candidate.is_relative_to(runtime_dir):
            raise ValueError(f"mutmut source path must be a file in the runtime directory: {source_path}")

    entry_file = str(task_profile["entry_file"])
    module = Path(entry_file).stem
    entrypoint = str(mutmut_profile["entrypoint_template"]).format(
        entry_file=entry_file, module=module
    )
    plugin_name = str(mutmut_profile["pytest_plugin"])
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", plugin_name) is None:
        raise ValueError(f"invalid pytest plugin module: {plugin_name!r}")

    checkpoint_id = str(runtime_manifest["checkpoint_id"])
    checkpoint_match = re.fullmatch(r"C([1-9][0-9]*)", checkpoint_id)
    if checkpoint_match is None:
        raise ValueError(f"unsupported checkpoint id: {checkpoint_id!r}")
    checkpoint_name = str(task_profile["checkpoint_name_prefix"]) + checkpoint_match.group(1)

    bridge_sources = {
        "mutmut_subprocess_bridge.py": REPO_ROOT / "scripts" / "mutmut_subprocess_bridge.py",
        "sitecustomize.py": REPO_ROOT / "scripts" / "mutmut_subprocess_sitecustomize.py",
    }
    bridge_hashes: dict[str, str] = {}
    for destination_name, source in bridge_sources.items():
        destination = runtime_dir / destination_name
        shutil.copy2(source, destination)
        bridge_hashes[destination_name] = sha256_file(destination)

    pyproject = runtime_dir / "pyproject.toml"
    if not pyproject.is_file():
        raise ValueError(f"staged project is missing pyproject.toml: {pyproject}")
    table = build_mutmut_table(
        source_paths=source_paths,
        test_directory=test_directory,
        checkpoint_name=checkpoint_name,
        entrypoint=entrypoint,
        pytest_plugin=plugin_name,
        debug=args.debug,
        test_selection=test_selection,
    )
    replace_mutmut_table(pyproject, table)

    hits_file = runtime_dir / "mutmut-subprocess-hits.jsonl"
    if hits_file.exists():
        hits_file.unlink()

    image = str(runtime_manifest["docker_image"])
    actual_image_id = docker_image_id(image)
    expected_image_id = runtime_manifest.get("docker_image_id")
    if expected_image_id and actual_image_id != expected_image_id:
        raise ValueError(
            f"Docker image changed since checkpoint preparation: expected {expected_image_id}, got {actual_image_id}"
        )

    command = [
        "docker",
        "run",
        "--rm",
        "--workdir",
        container_workspace,
        "--mount",
        f"type=bind,source={runtime_dir},target={container_workspace}",
        "--env",
        f"PYTHONPATH={container_workspace}",
        "--env",
        f"RQ1_MUTMUT_HITS_PATH={MUTMUT_HITS_PATH}",
        "--env",
        f"RQ1_MUTMUT_MAIN_MODULE={module}",
    ]
    for mount in runtime_manifest.get("asset_mounts_read_only", []):
        source = Path(mount["source"]).resolve(strict=True)
        target = str(mount["target"])
        if not target.startswith(container_workspace.rstrip("/") + "/"):
            raise ValueError(f"asset mount target is outside container workspace: {target!r}")
        command.extend(
            ["--mount", f"type=bind,source={source},target={target},readonly"]
        )

    run_mutmut_command = [MUTMUT_BIN, "run", "--max-children", str(args.max_children), *args.mutant]
    setup_commands = runtime_manifest.get("container_setup_commands", [])
    shell_commands = [shlex.join(list(item)) for item in setup_commands]
    shell_commands.append(shlex.join(run_mutmut_command))
    command.extend([image, "sh", "-lc", " && ".join(shell_commands)])
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    results_command = command[: command.index(image) + 1] + [
        MUTMUT_BIN,
        "results",
        "--all",
        "true",
    ]
    results = subprocess.run(
        results_command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    record = write_run_results(
        runtime_dir,
        runtime_manifest=runtime_manifest,
        command=command,
        run_result=result,
        results_result=results,
        source_hashes={
            source_path: sha256_file(runtime_dir.joinpath(*PurePosixPath(source_path).parts))
            for source_path in source_paths
        },
        bridge_hashes=bridge_hashes,
        test_selection=test_selection,
        baseline_audit_path=baseline_audit_path,
    )
    record["docker_image_id"] = actual_image_id
    (runtime_dir / "mutmut_run_manifest.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )

    print(f"Mutmut exit code: {result.returncode}")
    print(f"Result counts: {json.dumps(record['result_counts'], sort_keys=True)}")
    print(f"Mutmut log: {runtime_dir / 'mutmut-run.log'}")
    print(f"Mutmut results: {runtime_dir / 'mutmut-results.txt'}")
    print(f"Run manifest: {runtime_dir / 'mutmut_run_manifest.json'}")
    if result.returncode != 0:
        tail = result.stdout.splitlines()[-30:] + result.stderr.splitlines()[-30:]
        if tail:
            print("\n".join(tail), file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

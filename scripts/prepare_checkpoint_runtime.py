"""Prepare an isolated, read-only-asset Docker context for a checkpoint package."""

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
BENCHMARK_LOCK = REPO_ROOT / "config" / "benchmarks.lock.json"
CHECKPOINT_ID = re.compile(r"C([1-9][0-9]*)$")


def filesystem_path(path: Path) -> str:
    """Return a Windows extended-length path when needed for deep benchmark trees."""
    absolute = os.path.abspath(os.fspath(path))
    if os.name == "nt" and not absolute.startswith("\\\\?\\"):
        return "\\\\?\\" + absolute
    return absolute


def walk_files(root: Path) -> list[tuple[str, Path]]:
    root_ext = filesystem_path(root)
    result: list[tuple[str, Path]] = []
    for current, directories, names in os.walk(root_ext):
        directories.sort()
        for name in sorted(names):
            absolute = os.path.join(current, name)
            relative = os.path.relpath(absolute, root_ext).replace(os.sep, "/")
            result.append((relative, Path(absolute)))
    return result


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def package_path(package_root: Path, value: str, field: str) -> Path:
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or "\\" in value:
        raise ValueError(f"{field} must be a package-relative path: {value!r}")
    result = package_root.joinpath(*relative.parts).resolve(strict=True)
    if not result.is_relative_to(package_root.resolve()):
        raise ValueError(f"{field} escapes its checkpoint package: {value!r}")
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_problem_runtime(problem_config: Path, workspace: Path) -> tuple[str, list[tuple[str, str]], list[str]]:
    """Read the small, stable subset of problem_config.yaml needed by the adapter."""
    lines = problem_config.read_text(encoding="utf-8").splitlines()
    entry_match = re.search(r"(?m)^entry_file:\s*[\"']?([^\s\"'#]+)", "\n".join(lines))
    if entry_match is None:
        raise ValueError(f"problem_config.yaml has no entry_file: {problem_config}")
    entry_value = entry_match.group(1)
    entry_path = PurePosixPath(entry_value)
    if entry_path.is_absolute() or ".." in entry_path.parts or "\\" in entry_value:
        raise ValueError(f"unsafe entry_file in problem_config.yaml: {entry_value!r}")
    if entry_path.suffix != ".py":
        entry_path = entry_path.with_suffix(".py")
    resolved_entry = workspace.joinpath(*entry_path.parts).resolve(strict=True)
    if not resolved_entry.is_file() or not resolved_entry.is_relative_to(workspace.resolve(strict=True)):
        raise ValueError(f"entry_file does not name a Python source file in the workspace: {entry_value!r}")

    assets: list[tuple[str, str]] = []
    dependencies: list[str] = []
    section: str | None = None
    current_asset: str | None = None
    for line in lines:
        if re.match(r"^static_assets:\s*(?:#.*)?$", line):
            section = "assets"
            current_asset = None
            continue
        if re.match(r"^test_dependencies:\s*(?:#.*)?$", line):
            section = "dependencies"
            current_asset = None
            continue
        root_dependency_item = section == "dependencies" and re.match(r"^\s*-\s*", line)
        if (
            section is not None
            and line
            and not line[0].isspace()
            and not line.startswith("#")
            and not root_dependency_item
        ):
            section = None
            current_asset = None
        if section == "assets":
            key_match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
            if key_match:
                current_asset = key_match.group(1)
                continue
            path_match = re.match(r"^    path:\s*[\"']?([^\s\"'#]+)", line)
            if path_match and current_asset:
                assets.append((current_asset, path_match.group(1)))
        elif section == "dependencies":
            dependency_match = re.match(r"^\s*-\s*[\"']?([^\s\"'#]+)", line)
            if dependency_match:
                dependencies.append(dependency_match.group(1))

    return entry_path.as_posix(), assets, dependencies


def git_output(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def verify_pinned_checkout(
    problems_root: Path, problem_dir: Path | None, expected_commit: str
) -> None:
    problems_root = problems_root.resolve(strict=True)
    if not problems_root.is_dir():
        raise ValueError("--problems-root must be a directory")
    if problem_dir is not None:
        problem_dir = problem_dir.resolve(strict=True)
        if not problem_dir.is_dir() or not problem_dir.is_relative_to(problems_root):
            raise ValueError("--problem-dir must be a directory inside --problems-root")

    checkout_root = Path(git_output("rev-parse", "--show-toplevel", cwd=problems_root)).resolve()
    if checkout_root != problems_root:
        raise ValueError(f"--problems-root is not the repository root: {checkout_root}")
    actual_commit = git_output("rev-parse", "HEAD", cwd=problems_root)
    if actual_commit != expected_commit:
        raise ValueError(
            f"problem checkout commit mismatch: expected {expected_commit}, got {actual_commit}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path, help="Sanitized <task>/C<n> package")
    parser.add_argument(
        "--problems-root",
        required=True,
        type=Path,
        help="Root of the pinned scb-problems Git checkout",
    )
    parser.add_argument(
        "--problem-dir",
        type=Path,
        help="Optional task directory inside --problems-root; inferred from the task ID when present",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="New staging directory (default: tmp/checkpoint-runtime/<task>/<checkpoint>-<UTC timestamp>)",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Run the staged base suite in Docker after preparation; omitted by default",
    )
    args = parser.parse_args()

    package_root = args.package.resolve(strict=True)
    audit = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "validate_checkpoint_package.py"),
            "--package",
            str(package_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if audit.returncode != 0:
        raise ValueError(
            "checkpoint package preflight failed:\n"
            + audit.stdout
            + audit.stderr
        )
    package = load_json(package_root / "package.json")
    workspace = package_path(package_root, package["workspace_path"], "workspace_path")
    base_tests = package_path(package_root, package["base_tests_path"], "base_tests_path")
    if not workspace.is_dir() or not base_tests.is_dir():
        raise ValueError("workspace_path and base_tests_path must be directories")
    runtime = load_json(RUNTIME_CONFIG)
    benchmark_lock = load_json(BENCHMARK_LOCK)

    task_id = package["task_id"]
    checkpoint_id = package["checkpoint_id"]
    container_workspace = runtime["container_workspace"]
    checkpoint_match = CHECKPOINT_ID.fullmatch(checkpoint_id)
    if not checkpoint_match:
        raise ValueError(f"unsupported checkpoint id: {checkpoint_id!r}")
    task_runtime = dict(runtime["default_task_profile"])
    task_runtime.update(runtime.get("tasks", {}).get(task_id, {}))
    if package_root.name != checkpoint_id or package_root.parent.name != task_id:
        raise ValueError("package path must end in <task_id>/<checkpoint_id>")

    expected_commit = benchmark_lock["benchmarks"]["slop_code_bench_problems"]["commit"]
    expected_repository = benchmark_lock["benchmarks"]["slop_code_bench_problems"]["repository"]
    if runtime["benchmark_problem_commit"] != expected_commit:
        raise ValueError("checkpoint_runtime.json commit does not match benchmarks.lock.json")
    if runtime["benchmark_problem_repository"] != expected_repository:
        raise ValueError("checkpoint_runtime.json repository does not match benchmarks.lock.json")
    problems_root = args.problems_root.resolve(strict=True)
    if args.problem_dir is not None:
        problem_dir: Path | None = args.problem_dir.resolve(strict=True)
    else:
        inferred_problem_dir = problems_root / task_id
        problem_dir = inferred_problem_dir if inferred_problem_dir.is_dir() else None
    verify_pinned_checkout(problems_root, problem_dir, expected_commit)
    task_config = problem_dir / "config.yaml" if problem_dir is not None else None
    if task_config is not None and not task_config.is_file():
        raise ValueError("the pinned task directory must contain config.yaml")

    package_config = package_root / "problem_config.yaml"
    if not package_config.is_file():
        raise ValueError("checkpoint package is missing problem_config.yaml")
    packaged_entry_file, packaged_assets, packaged_test_dependencies = parse_problem_runtime(
        package_config, workspace
    )
    problem_config_source = "checkpoint_package"
    if task_config is not None:
        config_name = re.search(
            r"(?m)^name:\s*['\"]?([A-Za-z0-9_-]+)",
            task_config.read_text(encoding="utf-8"),
        )
        if config_name is None or config_name.group(1) != task_id:
            raise ValueError("pinned config.yaml name does not match package task_id")
        pinned_entry_file, pinned_assets, pinned_test_dependencies = parse_problem_runtime(
            task_config, workspace
        )
        if (pinned_entry_file, pinned_assets, pinned_test_dependencies) != (
            packaged_entry_file, packaged_assets, packaged_test_dependencies
        ):
            raise ValueError("packaged problem_config.yaml differs from the pinned task runtime fields")
        problem_config_source = "pinned_scb_problems_checkout"
    elif packaged_assets:
        raise ValueError(
            f"pinned scb-problems checkout has no task directory for {task_id!r}, "
            "but the package declares static assets"
        )

    asset_records: list[dict[str, str]] = []
    entry_file = packaged_entry_file
    discovered_assets = packaged_assets
    test_dependencies = packaged_test_dependencies
    test_directory = PurePosixPath(package["base_tests_path"])
    if test_directory.is_absolute() or ".." in test_directory.parts:
        raise ValueError("base_tests_path must be a safe relative path")
    task_runtime["entry_file"] = entry_file
    task_runtime["test_directory"] = test_directory.as_posix()
    source_paths = sorted(
        relative
        for relative, path in walk_files(workspace)
        if Path(relative).suffix == ".py"
    )
    if not source_paths:
        raise ValueError(f"no Python source files found in workspace: {workspace}")
    task_runtime["mutmut"] = {
        "source_paths": source_paths,
        "entrypoint_template": task_runtime["entrypoint_template"],
        "pytest_plugin": task_runtime["pytest_plugin"],
    }
    setup_commands = [list(command) for command in task_runtime["setup_commands"]]
    if test_dependencies:
        setup_commands.append(
            ["uv", "pip", "install", "--python", "/opt/mutmut-env/bin/python", *test_dependencies]
        )
    task_runtime["setup_commands"] = setup_commands

    for asset_name, source_relative in discovered_assets:
        source_path = PurePosixPath(source_relative)
        if source_path.is_absolute() or ".." in source_path.parts or "\\" in source_relative:
            raise ValueError(f"unsafe static asset path in problem_config.yaml: {source_relative!r}")
        if problem_dir is None:
            raise ValueError(f"no pinned task directory exists to provide static asset {asset_name!r}")
        source = problem_dir.joinpath(*source_path.parts).resolve(strict=True)
        if not source.is_dir() or not source.is_relative_to(problem_dir):
            raise ValueError(f"benchmark asset source must be a directory inside the pinned task: {source}")
        target_relative = PurePosixPath(
            task_runtime["asset_target_template"].format(
                test_directory=test_directory.as_posix(), asset_name=asset_name
            )
        )
        if target_relative.is_absolute() or ".." in target_relative.parts:
            raise ValueError(f"unsafe asset mount target: {target_relative}")
        files = walk_files(source)
        if not files:
            raise ValueError(f"benchmark asset directory is empty: {source}")
        for relative, required in files:
            with open(filesystem_path(required), "rb") as handle:
                if handle.read(64).startswith(b"version https://git-lfs.github.com/spec/v1"):
                    raise ValueError(f"Git LFS pointer found instead of asset data: {required}")
            asset_records.append(
                {
                    "asset": asset_name,
                    "path": relative,
                    "sha256": sha256_file(required),
                }
            )

    if args.output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = REPO_ROOT / "tmp" / "checkpoint-runtime" / task_id / f"{checkpoint_id}-{stamp}"
    else:
        output = args.output.resolve()
    allowed_output_root = (REPO_ROOT / "tmp").resolve()
    if not output.is_relative_to(allowed_output_root):
        raise ValueError(f"staging output must remain under {allowed_output_root}")
    if output.exists():
        raise ValueError(f"staging output already exists; choose a new directory: {output}")

    output.mkdir(parents=True)
    shutil.copytree(filesystem_path(workspace), filesystem_path(output), dirs_exist_ok=True)
    shutil.copytree(
        filesystem_path(base_tests),
        filesystem_path(output / task_runtime["test_directory"]),
    )
    pyproject = output / "pyproject.toml"
    if not pyproject.is_file():
        pyproject.write_text(
            "[project]\n"
            f"name = \"rq1-{task_id.replace('_', '-')}\"\n"
            "version = \"0.0.0\"\n"
            "requires-python = \">=3.12\"\n",
            encoding="utf-8",
        )
    for metadata_name in (
        "package.json",
        package["specification_path"],
        package.get("agent_run_manifest_path", "agent_run_manifest.json"),
        "problem_config.yaml",
    ):
        metadata = package_root / metadata_name
        if metadata.is_file():
            destination = output / "_package_metadata" / Path(metadata_name).name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(metadata, destination)

    asset_mounts: list[dict[str, str]] = []
    for asset_name, source_relative in discovered_assets:
        if problem_dir is None:
            raise ValueError(f"no pinned task directory exists to provide static asset {asset_name!r}")
        source = (problem_dir / source_relative).resolve(strict=True)
        target_relative = PurePosixPath(
            task_runtime["asset_target_template"].format(
                test_directory=test_directory.as_posix(), asset_name=asset_name
            )
        )
        target = output.joinpath(*target_relative.parts)
        target.mkdir(parents=True, exist_ok=True)
        asset_mounts.append(
            {
                "asset": asset_name,
                "source": str(source),
                "target": container_workspace.rstrip("/") + "/" + target_relative.as_posix(),
            }
        )

    checkpoint_name = task_runtime["checkpoint_name_prefix"] + checkpoint_match.group(1)
    format_values = {
        "entry_file": task_runtime["entry_file"],
        "checkpoint_name": checkpoint_name,
        "test_directory": task_runtime["test_directory"],
    }
    test_command = [part.format(**format_values) for part in task_runtime["test_command_template"]]
    shell_command = " && ".join(
        shlex.join(command)
        for command in [*task_runtime["setup_commands"], test_command]
    )
    manifest = {
        "format": "rq1-checkpoint-runtime/v1",
        "task_id": task_id,
        "checkpoint_id": checkpoint_id,
        "source_package": str(package_root),
        "package_audit": audit.stdout.strip(),
        "workspace_revision": package["workspace_revision"],
        "base_tests_sha256": package["base_tests_sha256"],
        "problem_repository": runtime["benchmark_problem_repository"],
        "problem_commit": expected_commit,
        "runner_repository": benchmark_lock["benchmarks"]["slop_code_bench_runner"]["repository"],
        "runner_commit": benchmark_lock["benchmarks"]["slop_code_bench_runner"]["commit"],
        "problem_checkout": str(problems_root),
        "problem_config_source": problem_config_source,
        "packaged_problem_config_sha256": sha256_file(package_config),
        "docker_image": runtime["docker_image"],
        "dockerfile": runtime["dockerfile"],
        "staging_directory": str(output),
        "asset_mounts_read_only": asset_mounts,
        "asset_files": asset_records,
        "test_command_argv": test_command,
        "container_setup_commands": task_runtime["setup_commands"],
        "task_runtime_profile": task_runtime,
        "container_command": ["sh", "-lc", shell_command],
        "source_package_modified": False,
    }
    manifest_path = output / "runtime_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"Prepared isolated checkpoint context: {output}")
    print(f"Pinned problem commit: {expected_commit}")
    print(f"Docker image: {runtime['docker_image']}")
    print("Read-only asset mounts:")
    for mount in asset_mounts:
        print(f"  {mount['source']} -> {mount['target']}")
    print(f"Base-suite command in container: {shell_command}")
    print(f"Runtime manifest: {manifest_path}")

    if args.run:
        image_check = subprocess.run(
            ["docker", "image", "inspect", runtime["docker_image"]],
            check=False,
            capture_output=True,
            text=True,
        )
        if image_check.returncode != 0:
            raise ValueError(
                f"Docker image {runtime['docker_image']!r} is not available locally; "
                f"build it with: docker build --tag {runtime['docker_image']} "
                f"--file {runtime['dockerfile']} ."
            )
        image_id = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", runtime["docker_image"]],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        manifest["docker_image_id"] = image_id
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        docker_argv = ["docker", "run", "--rm", "--workdir", container_workspace]
        docker_argv.extend(
            ["--mount", f"type=bind,source={output},target={container_workspace}"]
        )
        for mount in asset_mounts:
            docker_argv.extend(
                [
                    "--mount",
                    f"type=bind,source={mount['source']},target={mount['target']},readonly",
                ]
            )
        docker_argv.extend([runtime["docker_image"], "sh", "-lc", shell_command])
        result = subprocess.run(docker_argv, check=False, capture_output=True, text=True)
        (output / "container.stdout.log").write_text(result.stdout, encoding="utf-8")
        (output / "container.stderr.log").write_text(result.stderr, encoding="utf-8")
        manifest["run"] = {
            "exit_code": result.returncode,
            "stdout_log": "container.stdout.log",
            "stderr_log": "container.stderr.log",
        }
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        return result.returncode
    print("Not run. Pass --run only after reviewing the staged paths and Docker image.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

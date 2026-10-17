"""Export data-isolated RQ1 checkpoint packages from a completed SlopCodeBench run.

Each output package contains exactly one agent workspace snapshot, that checkpoint's
specification, and only the official tests available at that checkpoint.  It never
copies evaluation output, agent messages, reference solutions, or later checkpoint
tests into the package.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path


CHECKPOINT_TEST = re.compile(r"test_checkpoint_(\d+)\.py$")
REQUIRED_PACKAGE_FIELDS = {
    "task_id",
    "trajectory_id",
    "checkpoint_id",
    "workspace_path",
    "workspace_revision",
    "specification_path",
    "base_tests_path",
    "test_command",
    "runtime_environment_id",
    "agent_run_manifest_path",
}


def sha256_tree(root: Path) -> str:
    """Hash a directory deterministically, including relative filenames."""
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def copy_visible_tests(source: Path, destination: Path, checkpoint_number: int) -> None:
    """Copy shared helpers plus test_checkpoint_1..N, excluding future tests."""
    for item in source.rglob("*"):
        if not item.is_file():
            continue
        match = CHECKPOINT_TEST.search(item.name)
        if match and int(match.group(1)) > checkpoint_number:
            continue
        target = destination / item.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)


def package_checkpoint(
    task: str,
    checkpoint_number: int,
    run_dir: Path,
    problem_root: Path,
    output_root: Path,
) -> dict[str, str]:
    checkpoint_dir = run_dir / task / f"checkpoint_{checkpoint_number}"
    snapshot = checkpoint_dir / "snapshot"
    problem_dir = problem_root / task
    package_dir = output_root / task / f"C{checkpoint_number}"

    if not snapshot.is_dir():
        raise FileNotFoundError(f"Missing snapshot: {snapshot}")
    specification = problem_dir / f"checkpoint_{checkpoint_number}.md"
    if not specification.is_file():
        raise FileNotFoundError(f"Missing specification: {specification}")
    if not (problem_dir / "tests").is_dir():
        raise FileNotFoundError(f"Missing tests: {problem_dir / 'tests'}")

    shutil.copytree(snapshot, package_dir / "workspace", dirs_exist_ok=False)
    shutil.copy2(specification, package_dir / "specification.md")
    shutil.copy2(problem_dir / "config.yaml", package_dir / "problem_config.yaml")
    copy_visible_tests(problem_dir / "tests", package_dir / "base_tests", checkpoint_number)

    source_hash = sha256_tree(package_dir / "workspace")
    tests_hash = sha256_tree(package_dir / "base_tests")
    run_manifest = {
        "run_id": "rq1_full_36x1",
        "trajectory_id": f"rq1_full_36x1/{task}",
        "agent": {"type": "opencode", "version": "1.19.0"},
        "model": "gpt-6-luna",
        "thinking": "medium",
        "continuation": "The workspace was continued from C1 through this checkpoint.",
    }
    (package_dir / "agent_run_manifest.json").write_text(
        json.dumps(run_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    package = {
        "task_id": task,
        "trajectory_id": run_manifest["trajectory_id"],
        "checkpoint_id": f"C{checkpoint_number}",
        "workspace_path": "workspace",
        "workspace_revision": source_hash,
        "specification_path": "specification.md",
        "base_tests_path": "base_tests",
        "test_command": "uv run pytest -q base_tests",
        "runtime_environment_id": "docker-python3.12-uv",
        "agent_run_manifest_path": "agent_run_manifest.json",
        "base_tests_sha256": tests_hash,
        "notes": "Only tests legally visible at this checkpoint are included.",
    }
    missing = REQUIRED_PACKAGE_FIELDS - package.keys()
    if missing:
        raise ValueError(f"Schema-required fields missing: {sorted(missing)}")
    (package_dir / "package.json").write_text(
        json.dumps(package, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "task_id": task,
        "checkpoint_id": package["checkpoint_id"],
        "package_path": str(package_dir.relative_to(output_root)),
        "workspace_revision": source_hash,
        "base_tests_sha256": tests_hash,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--problem-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.output.exists():
        raise SystemExit(f"Refusing to overwrite existing output: {args.output}")
    args.output.mkdir(parents=True)
    records: list[dict[str, str]] = []
    for task_dir in sorted(path for path in args.run_dir.iterdir() if path.is_dir()):
        checkpoint_numbers = sorted(
            int(path.name.split("_", 1)[1])
            for path in task_dir.glob("checkpoint_*")
            if path.name.split("_", 1)[1].isdigit()
        )
        for checkpoint_number in checkpoint_numbers:
            records.append(
                package_checkpoint(
                    task_dir.name,
                    checkpoint_number,
                    args.run_dir,
                    args.problem_root,
                    args.output,
                )
            )

    index = {
        "format": "rq1-checkpoint-package-index/v1",
        "run_id": "rq1_full_36x1",
        "package_count": len(records),
        "records": records,
    }
    (args.output / "index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Exported {len(records)} checkpoint packages to {args.output}")


if __name__ == "__main__":
    main()

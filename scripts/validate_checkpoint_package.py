"""Audit sanitized checkpoint packages using only the Python standard library."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath


REQUIRED_FIELDS = {
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
    "base_tests_sha256",
}
OPTIONAL_FIELDS = {"notes"}
SHA256 = re.compile(r"^[a-f0-9]{64}$")
CHECKPOINT_COMPONENT = re.compile(r"^(?:test_)?checkpoint_(\d+)(?:$|[_\-.])")


def filesystem_path(path: Path) -> str:
    value = os.path.abspath(os.fspath(path))
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        return "\\\\?\\" + value
    return value


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


def sha256_tree(root: Path) -> str:
    """Hash a directory deterministically, matching the Part A exporter."""
    digest = hashlib.sha256()
    for relative, path in sorted(walk_files(root), key=lambda item: item[0]):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with open(filesystem_path(path), "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def package_path(package_root: Path, value: object, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty package-relative path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or "\\" in value:
        raise ValueError(f"{field} must stay within the package: {value!r}")
    path = package_root.joinpath(*relative.parts).resolve(strict=True)
    if not path.is_relative_to(package_root.resolve()):
        raise ValueError(f"{field} resolves outside the package: {value!r}")
    return path


def audit_package(package_root: Path) -> dict[str, str]:
    package_file = package_root / "package.json"
    if not package_file.is_file():
        raise ValueError("missing package.json")
    package = json.loads(package_file.read_text(encoding="utf-8"))

    keys = set(package)
    missing = REQUIRED_FIELDS - keys
    unknown = keys - REQUIRED_FIELDS - OPTIONAL_FIELDS
    if missing or unknown:
        raise ValueError(f"missing fields={sorted(missing)}, unknown fields={sorted(unknown)}")
    for key in REQUIRED_FIELDS - {"base_tests_sha256"}:
        if not isinstance(package[key], str) or not package[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if not SHA256.fullmatch(package["workspace_revision"]):
        raise ValueError("workspace_revision is not a lowercase SHA-256 digest")
    if not SHA256.fullmatch(package["base_tests_sha256"]):
        raise ValueError("base_tests_sha256 is not a lowercase SHA-256 digest")

    checkpoint_match = re.fullmatch(r"C([1-9][0-9]*)", package["checkpoint_id"])
    if not checkpoint_match:
        raise ValueError(f"invalid checkpoint_id: {package['checkpoint_id']!r}")
    checkpoint_number = int(checkpoint_match.group(1))
    if package["task_id"] != package_root.parent.name:
        raise ValueError("task_id does not match package directory")
    if package["checkpoint_id"] != package_root.name:
        raise ValueError("checkpoint_id does not match package directory")

    workspace = package_path(package_root, package["workspace_path"], "workspace_path")
    specification = package_path(
        package_root, package["specification_path"], "specification_path"
    )
    base_tests = package_path(package_root, package["base_tests_path"], "base_tests_path")
    run_manifest = package_path(
        package_root, package["agent_run_manifest_path"], "agent_run_manifest_path"
    )
    for field, path in (
        ("workspace_path", workspace),
        ("base_tests_path", base_tests),
    ):
        if not path.is_dir():
            raise ValueError(f"{field} is not a directory")
    for field, path in (
        ("specification_path", specification),
        ("agent_run_manifest_path", run_manifest),
    ):
        if not path.is_file():
            raise ValueError(f"{field} is not a file")

    observed_workspace_hash = sha256_tree(workspace)
    if observed_workspace_hash != package["workspace_revision"]:
        raise ValueError(
            f"workspace hash mismatch: expected {package['workspace_revision']}, "
            f"got {observed_workspace_hash}"
        )
    observed_tests_hash = sha256_tree(base_tests)
    if observed_tests_hash != package["base_tests_sha256"]:
        raise ValueError(
            f"base tests hash mismatch: expected {package['base_tests_sha256']}, "
            f"got {observed_tests_hash}"
        )

    for relative, path in walk_files(base_tests):
        parts = {part.casefold() for part in PurePosixPath(relative).parts}
        if "hidden" in parts:
            raise ValueError(f"hidden test file remains in generator input: {path}")
        if package["task_id"] == "file_backup" and "errors" in parts:
            raise ValueError(f"excluded file_backup error test remains: {path}")
        for part in PurePosixPath(relative).parts:
            match = CHECKPOINT_COMPONENT.match(part.casefold())
            if match and int(match.group(1)) > checkpoint_number:
                raise ValueError(f"future checkpoint test file remains: {path}")

    return {
        "task_id": package["task_id"],
        "checkpoint_id": package["checkpoint_id"],
        "workspace_revision": observed_workspace_hash,
        "base_tests_sha256": observed_tests_hash,
    }


def audit_manifest(root: Path, checked: list[dict[str, str]], *, require_full: bool = True) -> None:
    manifest_path = root.parent / "ISOLATION_MANIFEST.json"
    if not manifest_path.is_file():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = {
        record["package_path"]: record
        for record in manifest.get("packages", [])
        if isinstance(record, dict) and "package_path" in record
    }
    if require_full and manifest.get("package_count") != len(checked):
        raise ValueError(
            f"isolation manifest count {manifest.get('package_count')} "
            f"does not match audited package count {len(checked)}"
        )
    for record in checked:
        relative = f"{record['task_id']}/{record['checkpoint_id']}"
        expected = records.get(relative)
        if expected is None:
            raise ValueError(f"package absent from isolation manifest: {relative}")
        if expected.get("filtered_base_tests_sha256") != record["base_tests_sha256"]:
            raise ValueError(f"manifest base-test digest mismatch: {relative}")

    index_path = root / "index.json"
    if index_path.is_file():
        index = json.loads(index_path.read_text(encoding="utf-8"))
        if require_full and index.get("package_count") != len(checked):
            raise ValueError("index package_count does not match audited package count")
        index_records = {
            record["package_path"]: record for record in index.get("records", [])
        }
        for record in checked:
            relative = f"{record['task_id']}/{record['checkpoint_id']}"
            indexed = index_records.get(relative)
            if indexed is None:
                raise ValueError(f"package absent from index.json: {relative}")
            if indexed.get("base_tests_sha256") != record["base_tests_sha256"]:
                raise ValueError(f"index base-test digest mismatch: {relative}")
            if indexed.get("workspace_revision") != record["workspace_revision"]:
                raise ValueError(f"index workspace digest mismatch: {relative}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "data"
        / "checkpoints"
        / "checkpoint_packages_rq1_full_36x1",
        help="Root containing <task>/C<n>/package.json packages",
    )
    parser.add_argument(
        "--package",
        type=Path,
        help="Audit one <task>/C<n> package against the isolation manifest",
    )
    args = parser.parse_args()
    if args.package is not None:
        package_dir = args.package.resolve(strict=True)
        root = package_dir.parent.parent
        record = audit_package(package_dir)
        audit_manifest(root, [record], require_full=False)
        print(f"OK package {record['task_id']}/{record['checkpoint_id']}")
        print("OK workspace and base-test hashes match package metadata and isolation manifest")
        print("OK no hidden, deferred file_backup/errors, or future test files in generator inputs")
        return
    root = args.root.resolve(strict=True)
    package_dirs = sorted(path.parent for path in root.glob("*/*/package.json"))
    if not package_dirs:
        raise SystemExit(f"no checkpoint packages found under {root}")

    checked: list[dict[str, str]] = []
    failures: list[str] = []
    for package_dir in package_dirs:
        try:
            checked.append(audit_package(package_dir))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            failures.append(f"{package_dir.relative_to(root)}: {error}")
    if not failures:
        try:
            audit_manifest(root, checked)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            failures.append(f"ISOLATION_MANIFEST.json: {error}")

    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        raise SystemExit(1)
    tasks = sorted({record["task_id"] for record in checked})
    print(f"OK {len(checked)} packages across {len(tasks)} tasks")
    print("OK workspace and base-test hashes match package metadata")
    print("OK no hidden, deferred file_backup/errors, or future test files in generator inputs")
    print("OK package base-test hashes match the isolation manifest")


if __name__ == "__main__":
    main()

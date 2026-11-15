"""Build a hash-pinned candidate source/target manifest from the sanitized RQ1 data."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACKAGE_ROOT = REPO_ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
DEFAULT_OUTPUT = REPO_ROOT / "config" / "rq1_full_36x1_candidate_units.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path, default=DEFAULT_PACKAGE_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    package_root = args.package_root.resolve(strict=True)
    audit = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "validate_checkpoint_package.py"),
            "--root",
            str(package_root),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if audit.returncode:
        raise SystemExit(f"Checkpoint package audit failed:\n{audit.stdout}{audit.stderr}")

    by_task: dict[str, list[Path]] = {}
    for package_file in sorted(package_root.glob("*/C[0-9]*/package.json")):
        package_dir = package_file.parent
        by_task.setdefault(package_dir.parent.name, []).append(package_dir)

    units: list[dict[str, Any]] = []
    for task_id, package_dirs in sorted(by_task.items()):
        package_dirs.sort(key=lambda path: int(path.name[1:]))
        source = next((path for path in package_dirs if path.name == "C1"), None)
        if source is None:
            continue
        package_file = source / "package.json"
        package = read_json(package_file)
        units.append(
            {
                "unit_id": f"{task_id}/{source.name}",
                "task_id": task_id,
                "trajectory_id": package["trajectory_id"],
                "source_checkpoint": source.name,
                "source_package": source.relative_to(package_root).as_posix(),
                "source_package_json_sha256": sha256_file(package_file),
                "workspace_revision": package["workspace_revision"],
                "base_tests_sha256": package["base_tests_sha256"],
                "target_checkpoints": [path.name for path in package_dirs if int(path.name[1:]) > 1],
                "source_checkpoint_only_inputs": True,
                "status": "prepared_not_evaluated",
            }
        )

    output = args.output.resolve()
    if not output.is_relative_to(REPO_ROOT):
        raise ValueError(f"output must remain in the repository: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format": "rq1-candidate-unit-manifest/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset": "rq1_full_36x1",
        "selection_rule": "Use C1 as the single source checkpoint for each trajectory; list every later checkpoint as a potential target. Generation inputs are restricted to that C1 package. This prepares the full 36-trajectory long-horizon comparison without opening future checkpoint contents.",
        "package_root": str(package_root),
        "package_audit": audit.stdout.strip(),
        "package_count": sum(len(paths) for paths in by_task.values()),
        "task_count": len(by_task),
        "candidate_unit_count": len(units),
        "units": units,
        "evaluation_run": False,
    }
    output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(units)} candidate source units across {len(by_task)} tasks: {output}")
    print("Each unit uses only C1 package hashes; later checkpoint IDs are listed as potential targets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

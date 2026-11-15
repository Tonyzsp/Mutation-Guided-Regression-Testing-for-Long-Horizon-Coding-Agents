"""Merge partial C1 baseline audits after checking task and package hashes."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UNITS = ROOT / "config" / "rq1_full_36x1_candidate_units.json"
DEFAULT_PACKAGE_ROOT = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
DEFAULT_OUTPUT = (
    ROOT
    / "artifacts"
    / "results"
    / "rq1_full_36x1"
    / "c1_base_audit_isolation_revalidated_20261019"
    / "c1_base_suite_audit.json"
)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--units", type=Path, default=DEFAULT_UNITS)
    parser.add_argument("--package-root", type=Path, default=DEFAULT_PACKAGE_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    units_path = args.units.resolve(strict=True)
    units = read_json(units_path)["units"]
    expected_tasks = [unit["task_id"] for unit in units]
    expected_set = set(expected_tasks)
    package_root = args.package_root.resolve(strict=True)
    inputs = [path.resolve(strict=True) for path in args.input]
    audits = [read_json(path) for path in inputs]
    compatibility_fields = (
        "format",
        "dataset",
        "docker_image",
        "docker_image_id",
        "problem_commit",
        "package_root",
        "repeats",
    )
    reference = audits[0]
    for path, audit in zip(inputs[1:], audits[1:]):
        for field in compatibility_fields:
            if audit.get(field) != reference.get(field):
                raise ValueError(f"audit metadata mismatch for {field}: {path}")

    by_task: dict[str, dict[str, Any]] = {}
    for path, audit in zip(inputs, audits):
        for record in audit.get("records", []):
            task = record.get("task_id")
            if task not in expected_set:
                raise ValueError(f"unexpected task in audit input: {task!r}")
            previous = by_task.get(task)
            if previous is not None:
                if previous.get("status") != "infrastructure_error":
                    raise ValueError(f"duplicate task is not replacing an infrastructure error: {task}")
                if record.get("status") in {"infrastructure_error", "runtime_error"}:
                    raise ValueError(f"replacement audit record is still unusable: {task}")
            by_task[task] = {**record, "audit_source": str(path.relative_to(ROOT))}

    missing = expected_set - set(by_task)
    if missing:
        raise ValueError(f"missing task audit records: {sorted(missing)}")

    for unit in units:
        task = unit["task_id"]
        package = package_root / task / "C1"
        package_file = package / "package.json"
        package_meta = read_json(package_file)
        record = by_task[task]
        if sha256_file(package_file) != unit["source_package_json_sha256"]:
            raise ValueError(f"candidate unit manifest is stale for {task}")
        if record.get("package_json_sha256") != unit["source_package_json_sha256"]:
            raise ValueError(f"baseline record package hash is stale for {task}")
        if record.get("base_tests_sha256") != package_meta.get("base_tests_sha256"):
            raise ValueError(f"baseline record base-test hash is stale for {task}")
        if record.get("checkpoint_id") != "C1":
            raise ValueError(f"baseline record is not C1 for {task}")

    ordered = [by_task[task] for task in expected_tasks]
    status_counts: dict[str, int] = {}
    for record in ordered:
        status = str(record.get("status", "missing_status"))
        status_counts[status] = status_counts.get(status, 0) + 1
    unusable = [
        record["task_id"]
        for record in ordered
        if record.get("status") in {"infrastructure_error", "runtime_error", "unstable"}
    ]
    output = args.output.resolve()
    if not output.is_relative_to(ROOT):
        raise ValueError(f"output must remain in the repository: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    merged = {
        **reference,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "records": ordered,
        "task_count": len(ordered),
        "expected_task_count": len(expected_tasks),
        "status_counts": status_counts,
        "status": "complete" if len(ordered) == len(expected_tasks) and not unusable else "incomplete",
        "unusable_tasks": unusable,
        "merge_inputs": [str(path.relative_to(ROOT)) for path in inputs],
        "candidate_units_sha256": sha256_file(units_path),
    }

    if output.is_file():
        backup = output.with_name("c1_base_suite_audit.partial_before_merge.json")
        if not backup.exists():
            backup.write_bytes(output.read_bytes())
    output.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "status": merged["status"],
        "task_count": merged["task_count"],
        "expected_task_count": merged["expected_task_count"],
        "status_counts": status_counts,
        "unusable_tasks": unusable,
    }, ensure_ascii=False))
    return 0 if merged["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())

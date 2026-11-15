"""Rebuild source-checkpoint-only gap briefs for the sanitized C1 cohort."""

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
RESULT_ROOT = ROOT / "artifacts" / "results" / "rq1_full_36x1"
PACKAGE_ROOT = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
UNITS_PATH = ROOT / "config" / "rq1_full_36x1_candidate_units.json"
BUILDER = ROOT / "scripts" / "build_c1_gap_brief.py"
BRIEF_SUFFIX = "gap_opportunities_isolation_revalidated_v3.md"
OUTPUT = RESULT_ROOT / "c1_gap_brief_cohort_isolation_revalidated_20261019.json"


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
    parser.add_argument("--task", action="append", help="Limit to task IDs; may be repeated")
    args = parser.parse_args()
    units_manifest = read_json(UNITS_PATH)
    units = units_manifest["units"]
    selected = [unit for unit in units if not args.task or unit["task_id"] in args.task]
    unknown = sorted(set(args.task or []) - {unit["task_id"] for unit in units})
    if unknown:
        raise ValueError(f"unknown task IDs: {unknown}")
    if not selected:
        raise ValueError("no C1 units selected")

    records: list[dict[str, Any]] = []
    for index, unit in enumerate(selected, 1):
        task = unit["task_id"]
        package = PACKAGE_ROOT / task / "C1"
        brief = RESULT_ROOT / task / "C1" / BRIEF_SUFFIX
        manifest_path = brief.with_name("opportunity_brief_manifest.json")
        status = "failed"
        error = ""
        if brief.is_file() and manifest_path.is_file():
            manifest = read_json(manifest_path)
            if manifest.get("base_tests_sha256") == unit["base_tests_sha256"] and manifest.get("brief_sha256") == sha256_file(brief):
                status = "reused"
            else:
                raise ValueError(f"existing brief has stale provenance for {task}: {brief}")
        else:
            result = subprocess.run(
                [
                    sys.executable,
                    str(BUILDER),
                    "--package",
                    str(package),
                    "--output",
                    str(brief),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if result.returncode == 0 and brief.is_file() and manifest_path.is_file():
                manifest = read_json(manifest_path)
                if (
                    manifest.get("task_id") == task
                    and manifest.get("checkpoint_id") == "C1"
                    and manifest.get("base_tests_sha256") == unit["base_tests_sha256"]
                    and manifest.get("brief_sha256") == sha256_file(brief)
                ):
                    status = "complete"
                else:
                    error = "generated brief manifest does not match the current C1 package"
            else:
                error = result.stderr.strip() or result.stdout.strip() or f"builder exit code {result.returncode}"
        record = {
            "task_id": task,
            "checkpoint_id": "C1",
            "status": status,
            "brief_path": str(brief.relative_to(ROOT)),
            "brief_sha256": sha256_file(brief) if brief.is_file() else None,
            "base_tests_sha256": unit["base_tests_sha256"],
            "error": error,
        }
        records.append(record)
        print(f"[{index}/{len(selected)}] {task}: {status}", flush=True)

    complete = sum(record["status"] in {"complete", "reused"} for record in records)
    report = {
        "format": "rq1-c1-gap-brief-cohort/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "complete" if complete == len(selected) else "incomplete",
        "task_count": len(selected),
        "completed_count": complete,
        "candidate_units_sha256": sha256_file(UNITS_PATH),
        "records": records,
    }
    output = OUTPUT if not args.task else OUTPUT.with_name("c1_gap_brief_cohort_selected_20261019.json")
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "task_count": len(selected), "completed_count": complete, "output": str(output)}, ensure_ascii=False))
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Report checkpoint and core/hidden test counts without reading case contents.

Use this as a Gate 1 check against a pinned scb-problems checkout. It deliberately
reports only directory metadata so it is safe to retain in the project repository;
this project excludes the reported ``hidden`` groups from all experiment flows.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def checkpoint_directories(problem_dir: Path) -> list[Path]:
    data_dir = problem_dir / "tests" / "data"
    if not data_dir.is_dir():
        return []
    return sorted(
        (path for path in data_dir.iterdir() if path.is_dir() and path.name.startswith("checkpoint_")),
        key=lambda path: path.name,
    )


def group_case_count(checkpoint_dir: Path, group: str) -> int:
    group_dir = checkpoint_dir / group
    if not group_dir.is_dir():
        return 0
    return sum(1 for path in group_dir.iterdir() if path.is_dir())


def audit(problem_dir: Path) -> list[dict[str, object]]:
    if not (problem_dir / "config.yaml").is_file():
        raise ValueError(f"Not a SCBench problem directory: {problem_dir}")
    return [
        {
            "task_id": problem_dir.name,
            "source_checkpoint": checkpoint_dir.name,
            "core_cases": group_case_count(checkpoint_dir, "core"),
            "hidden_cases": group_case_count(checkpoint_dir, "hidden"),
            "errors_cases": group_case_count(checkpoint_dir, "errors"),
        }
        for checkpoint_dir in checkpoint_directories(problem_dir)
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--problems-root",
        type=Path,
        default=Path("external/scb-problems"),
        help="Pinned local scb-problems checkout.",
    )
    parser.add_argument(
        "--task",
        action="append",
        required=True,
        help="Problem directory name; repeat for multiple tasks.",
    )
    parser.add_argument("--csv", type=Path, help="Optional report destination.")
    args = parser.parse_args()

    rows = [row for task in args.task for row in audit(args.problems_root / task)]
    if not rows:
        raise SystemExit("No checkpoint data directories found.")

    columns = list(rows[0])
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", newline="", encoding="utf-8") as report:
            writer = csv.DictWriter(report, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
    else:
        writer = csv.DictWriter(__import__("sys").stdout, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

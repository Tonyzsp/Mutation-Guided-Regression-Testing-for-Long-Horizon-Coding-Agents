"""Export mutmut statuses and diffs from a completed staged checkpoint run."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_CONFIG = REPO_ROOT / "config" / "checkpoint_runtime.json"
MUTMUT_BIN = "/opt/mutmut-env/bin/mutmut"
PYTHON_BIN = "/opt/mutmut-env/bin/python"
STATUS_LINE = re.compile(r"^\s*(\S+):\s*(killed|survived|not checked|no tests|skipped|suspicious|timeout|segfault)\s*$")
HUNK_LINE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_relative(value: str) -> Path:
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts or any(part in {".", ".."} for part in pure.parts):
        raise ValueError(f"unsafe repository-relative path: {value!r}")
    return Path(*pure.parts)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_statuses(text: str) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for line in text.splitlines():
        match = STATUS_LINE.match(line)
        if match:
            mutant_id, status = match.groups()
            if mutant_id in statuses:
                raise ValueError(f"duplicate mutant status: {mutant_id}")
            statuses[mutant_id] = status
    if not statuses:
        raise ValueError("mutmut results output contained no parseable mutant statuses")
    return statuses


def invoke_mutmut_results(runtime_dir: Path, image: str) -> str:
    command = [
        "docker",
        "run",
        "--rm",
        "--workdir",
        "/workspace",
        "--mount",
        f"type=bind,source={runtime_dir},target=/workspace,readonly",
        image,
        MUTMUT_BIN,
        "results",
        "--all",
        "true",
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"mutmut results failed ({result.returncode}):\n{result.stdout}\n{result.stderr}"
        )
    return result.stdout + result.stderr


def get_diffs(runtime_dir: Path, image: str, mutant_ids: list[str]) -> dict[str, str]:
    ids_path = runtime_dir / "mutmut-export-ids.json"
    ids_path.write_text(json.dumps(mutant_ids), encoding="utf-8")
    export_program = "\n".join(
        [
            "import json",
            "from pathlib import Path",
            "import mutmut.__main__ as mutmut",
            "names = json.loads(Path('/workspace/mutmut-export-ids.json').read_text(encoding='utf-8'))",
            "diffs = {}",
            "for name in names:",
            "    item = mutmut.find_mutant(name)",
            "    diffs[name] = mutmut.get_diff_for_mutant(name, path=item.path)",
            "print(json.dumps(diffs, ensure_ascii=False))",
        ]
    )
    command = [
        "docker",
        "run",
        "--rm",
        "--workdir",
        "/workspace",
        "--mount",
        f"type=bind,source={runtime_dir},target=/workspace,readonly",
        image,
        PYTHON_BIN,
        "-c",
        export_program,
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"mutmut diff extraction failed ({result.returncode}):\n{result.stdout}\n{result.stderr}"
        )
    return json.loads(result.stdout)


def source_location(diff: str, fallback_line: int) -> int:
    current_line: int | None = None
    changed_lines: list[int] = []
    for line in diff.splitlines():
        hunk = HUNK_LINE.match(line)
        if hunk:
            current_line = int(hunk.group(1))
            continue
        if current_line is None or line.startswith(("---", "+++", "\\")):
            continue
        if line.startswith("+"):
            changed_lines.append(current_line)
            current_line += 1
        elif line.startswith("-"):
            continue
        else:
            current_line += 1
    # mutmut's diff hunks are relative to the extracted function, not the file.
    return fallback_line + min(changed_lines) - 1 if changed_lines else fallback_line


def function_start(source: str, function_name: str) -> int:
    tree = ast.parse(source)
    target = function_name.rsplit(".", 1)[-1]
    starts = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == target
    ]
    return min(starts) if starts else 1


def normalize_status(raw_status: str) -> tuple[str, str]:
    if raw_status in {"killed", "survived"}:
        return raw_status, raw_status
    if raw_status == "timeout":
        return "timeout", "inconclusive"
    if raw_status == "segfault":
        return "error", "inconclusive"
    if raw_status in {"not checked", "no tests", "skipped"}:
        return "not_run", "inconclusive"
    return "unknown", "inconclusive"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--trajectory-id", required=True)
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args()

    runtime_dir = args.runtime_dir.resolve(strict=True)
    tmp_root = (REPO_ROOT / "tmp").resolve(strict=True)
    if not runtime_dir.is_relative_to(tmp_root):
        raise ValueError(f"runtime directory must remain under {tmp_root}")

    runtime_manifest = read_json(runtime_dir / "runtime_manifest.json")
    runtime_config = read_json(RUNTIME_CONFIG)
    profile = runtime_manifest.get("task_runtime_profile")
    if profile is None:
        # Backward compatibility for runtime manifests produced by the original C1 adapter.
        profile = runtime_config.get("tasks", {}).get(runtime_manifest["task_id"])
    if profile is None or "mutmut" not in profile:
        raise ValueError(f"runtime manifest has no mutmut profile for {runtime_manifest['task_id']!r}")
    image = str(runtime_manifest["docker_image"])
    results_text = invoke_mutmut_results(runtime_dir, image)
    (runtime_dir / "mutmut-results.txt").write_text(results_text, encoding="utf-8")
    statuses = parse_statuses(results_text)
    counts = Counter(statuses.values())
    incomplete = any(status not in {"killed", "survived"} for status in statuses.values())

    summary: dict[str, Any] = {
        "format": "rq1-mutmut-export/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "task_id": runtime_manifest["task_id"],
        "trajectory_id": args.trajectory_id,
        "checkpoint_id": runtime_manifest["checkpoint_id"],
        "runtime_directory": str(runtime_dir),
        "source_sha256": {
            relative: sha256_file(runtime_dir / safe_relative(relative))
            for relative in profile["mutmut"]["source_paths"]
        },
        "mutant_count": len(statuses),
        "status_counts": dict(sorted(counts.items())),
        "complete": not incomplete,
        "mutation_score": None,
        "mutation_score_definition": "killed / (killed + survived); only reported when all mutants have a terminal killed/survived status",
    }
    if not incomplete:
        denominator = counts["killed"] + counts["survived"]
        summary["mutation_score"] = counts["killed"] / denominator if denominator else None

    summary_path = runtime_dir / "mutation_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if incomplete and not args.allow_incomplete:
        print(f"Incomplete mutmut run: {json.dumps(summary['status_counts'], sort_keys=True)}", file=sys.stderr)
        print(f"Raw results: {runtime_dir / 'mutmut-results.txt'}", file=sys.stderr)
        print(f"Summary: {summary_path}", file=sys.stderr)
        return 2

    source_path = str(profile["mutmut"]["source_paths"][0])
    source_text = (runtime_dir / safe_relative(source_path)).read_text(encoding="utf-8")
    ids_to_diff = list(statuses)
    diffs = get_diffs(runtime_dir, image, ids_to_diff)

    records_path = runtime_dir / "mutant_records.jsonl"
    with records_path.open("w", encoding="utf-8", newline="\n") as output:
        for mutant_id, raw_status in statuses.items():
            diff = diffs.get(mutant_id, "")
            function_name = mutant_id.split("__mutmut_", 1)[0].split(".", 1)[-1]
            if function_name.startswith("x_"):
                function_name = function_name[2:]
            raw_outcome, normalized = normalize_status(raw_status)
            record = {
                "schema_version": "1.0",
                "task_id": runtime_manifest["task_id"],
                "trajectory_id": args.trajectory_id,
                "checkpoint_id": runtime_manifest["checkpoint_id"],
                "mutant_id": mutant_id,
                "operator": None,
                "location": {
                    "path": source_path,
                    "line": source_location(diff, function_start(source_text, function_name)),
                    "column": None,
                },
                "diff": diff,
                "raw_status": raw_status,
                "raw_outcome": raw_outcome,
                "normalized_status": normalized,
                "base_suite_reached_location": None,
                "review_status": "needs_review" if normalized == "survived" else "not_reviewed",
                "review_rationale": None,
                "evidence_path": "mutmut-results.txt;mutmut-run.log;mutmut-subprocess-hits.jsonl",
                "mutated_source_sha256": None,
            }
            output.write(json.dumps(record, ensure_ascii=False) + "\n")

    summary["records_path"] = records_path.name
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"Mutants: {len(statuses)}")
    print(f"Statuses: {json.dumps(summary['status_counts'], sort_keys=True)}")
    print(f"Complete: {summary['complete']}")
    print(f"Mutation score: {summary['mutation_score']}")
    print(f"Records: {records_path}")
    print(f"Summary: {summary_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

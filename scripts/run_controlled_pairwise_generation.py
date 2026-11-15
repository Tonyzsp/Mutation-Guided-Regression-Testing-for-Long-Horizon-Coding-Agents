"""Dispatch the frozen per-mutant paired Vanilla and Guided model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "artifacts" / "results" / "controlled_pairwise_v1_20261019" / "sample_manifest.json"
DEFAULT_BUDGET = ROOT / "config" / "controlled_pairwise_generation_budget_v1.json"
DEFAULT_PROMPT = ROOT / "prompts" / "controlled_pairwise_v1.md"
OUTPUT_SCHEMA = ROOT / "schemas" / "codex-generated-test-candidates-gap-v2.schema.json"
VANILLA_RUNNER = ROOT / "scripts" / "run_vanilla_generation.py"
GUIDED_RUNNER = ROOT / "scripts" / "run_mutation_guided_generation.py"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_jsonl(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_pair_condition(path: Path, pair: dict[str, Any], method: str, manifest_path: Path) -> None:
    record = {
        "schema_version": "controlled-pairwise-condition/v1",
        "pair_id": pair["pair_id"],
        "task_id": pair["task_id"],
        "checkpoint_id": pair["checkpoint_id"],
        "mutant_id": pair["mutant_id"],
        "method": method,
        "sample_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }
    encoded = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") != encoded:
        raise FileExistsError(f"refusing to overwrite different pair metadata: {path}")
    if not path.exists():
        path.write_text(encoded, encoding="utf-8")


def slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def output_path(root: Path, pair: dict[str, Any], method: str) -> Path:
    return root / pair["task_id"] / slug(pair["mutant_id"]) / method


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--budget-config", type=Path, default=DEFAULT_BUDGET)
    parser.add_argument("--prompt-template", type=Path, default=DEFAULT_PROMPT)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--method", choices=("vanilla_llm", "mutation_guided", "both"), default="both")
    parser.add_argument("--pair-id", help="Optional single preregistered pair ID; useful for a controlled first-call smoke check.")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()

    manifest_path = args.manifest.resolve(strict=True)
    manifest = read_json(manifest_path)
    if manifest.get("schema_version") != "controlled-pairwise-sample/v1":
        raise ValueError("unsupported controlled paired sample manifest")
    output_root = args.output_root.resolve() if args.output_root else manifest_path.parent.resolve()
    if not output_root.is_relative_to((ROOT / "artifacts" / "results").resolve()):
        raise ValueError("paired-generation output root must remain under artifacts/results")
    methods = ("vanilla_llm", "mutation_guided") if args.method == "both" else (args.method,)
    dispatch_path = output_root / "generation_dispatch.jsonl"
    pairs = manifest.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("sample manifest contains no target pairs")
    if args.pair_id is not None:
        pairs = [pair for pair in pairs if pair.get("pair_id") == args.pair_id]
        if not pairs:
            raise ValueError(f"pair ID is not in the frozen sample: {args.pair_id}")

    for pair in pairs:
        ordered = ["vanilla_llm", "mutation_guided"]
        if pair["method_order"] == "mutation_guided_first":
            ordered.reverse()
        if pair["method_order"] not in {"vanilla_first", "mutation_guided_first"}:
            raise ValueError(f"unknown method order for {pair['mutant_id']}")
        for method in ordered:
            if method not in methods:
                continue
            result_dir = output_path(output_root, pair, method)
            result_dir.mkdir(parents=True, exist_ok=True)
            write_pair_condition(result_dir / "pairwise_condition.json", pair, method, manifest_path)
            generation_run_path = result_dir / "generation_run.json"
            if generation_run_path.is_file():
                existing = read_json(generation_run_path)
                write_jsonl(dispatch_path, {
                    "pair_id": pair["pair_id"],
                    "task_id": pair["task_id"],
                    "mutant_id": pair["mutant_id"],
                    "method": method,
                    "status": "already_complete_skipped",
                    "existing_generation_run": str(generation_run_path.relative_to(ROOT).as_posix()),
                    "candidate_count": existing.get("candidate_count"),
                    "at_utc": datetime.now(timezone.utc).isoformat(),
                })
                continue

            package = ROOT / pair["source_package"]
            budget = args.budget_config.resolve(strict=True)
            prompt = args.prompt_template.resolve(strict=True)
            if method == "vanilla_llm":
                command = [
                    sys.executable, str(VANILLA_RUNNER),
                    "--package", str(package),
                    "--output-dir", str(result_dir),
                    "--budget-config", str(budget),
                    "--prompt-template", str(prompt),
                    "--output-schema", str(OUTPUT_SCHEMA),
                    "--timeout-seconds", str(args.timeout_seconds),
                ]
            else:
                records = ROOT / pair["mutant_records"]
                triage = ROOT / pair["target_triage"]
                command = [
                    sys.executable, str(GUIDED_RUNNER),
                    "--package", str(package),
                    "--mutant-records", str(records),
                    "--mutant-triage", str(triage),
                    "--output-dir", str(result_dir),
                    "--budget-config", str(budget),
                    "--prompt-template", str(prompt),
                    "--output-schema", str(OUTPUT_SCHEMA),
                    "--timeout-seconds", str(args.timeout_seconds),
                    "--pairwise-minimal-guidance",
                ]

            started = datetime.now(timezone.utc)
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=args.timeout_seconds + 120,
                    cwd=ROOT,
                    check=False,
                )
                stdout, stderr, exit_code = result.stdout, result.stderr, result.returncode
                status = "completed" if exit_code == 0 and generation_run_path.is_file() else "failed"
            except subprocess.TimeoutExpired as error:
                stdout = error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
                stderr = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
                stderr += f"\nPairwise generation driver timed out after {args.timeout_seconds + 120} seconds.\n"
                exit_code = 124
                status = "timed_out"

            (result_dir / "pairwise_driver.stdout.log").write_text(stdout, encoding="utf-8")
            (result_dir / "pairwise_driver.stderr.log").write_text(stderr, encoding="utf-8")
            run_record = read_json(generation_run_path) if generation_run_path.is_file() else {}
            write_jsonl(dispatch_path, {
                "pair_id": pair["pair_id"],
                "task_id": pair["task_id"],
                "mutant_id": pair["mutant_id"],
                "method": method,
                "status": status,
                "exit_code": exit_code,
                "candidate_count": run_record.get("candidate_count"),
                "output_dir": str(result_dir.relative_to(ROOT).as_posix()),
                "started_at_utc": started.isoformat(),
                "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            })
            print(f"{status}: {pair['mutant_id']} [{method}]")
            if stdout:
                print(stdout.rstrip())
            if stderr:
                print(stderr.rstrip(), file=sys.stderr)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

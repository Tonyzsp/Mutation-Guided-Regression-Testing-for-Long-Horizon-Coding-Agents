"""Run the same C1 review, execution, and targeted-mutant validation for both conditions."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "artifacts" / "results" / "controlled_pairwise_v1_20261019" / "sample_manifest.json"
VALIDATOR = ROOT / "scripts" / "validate_and_freeze_tests.py"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_jsonl(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def generation_dir(root: Path, pair: dict[str, Any], method: str) -> Path:
    relative = pair.get("output_relpath")
    if relative is not None:
        parts = Path(relative)
        if parts.is_absolute() or ".." in parts.parts:
            raise ValueError("pair output_relpath must stay under the output root")
        return root / parts / method
    return root / pair["task_id"] / slug(pair["mutant_id"]) / method


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--method", choices=("vanilla_llm", "mutation_guided", "both"), default="both")
    parser.add_argument("--task", action="append", help="Limit to task IDs; may be repeated")
    parser.add_argument("--pair-id", action="append", help="Limit to exact paired mutant IDs; may be repeated")
    parser.add_argument(
        "--paired-direct-diff-validation",
        action="store_true",
        help="Use the unverified fast path that applies archived diffs and repeats only generated candidate nodes.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--mutmut-timeout-seconds", type=int, default=900)
    args = parser.parse_args()

    manifest_path = args.manifest.resolve(strict=True)
    manifest = read_json(manifest_path)
    output_root = args.output_root.resolve() if args.output_root else manifest_path.parent.resolve()
    if not output_root.is_relative_to((ROOT / "artifacts" / "results").resolve()):
        raise ValueError("paired validation output root must remain under artifacts/results")
    methods = ("vanilla_llm", "mutation_guided") if args.method == "both" else (args.method,)
    dispatch = output_root / "validation_dispatch.jsonl"

    selected_pairs = [
        pair for pair in manifest["pairs"]
        if (not args.task or pair["task_id"] in args.task)
        and (not args.pair_id or pair["pair_id"] in args.pair_id)
    ]
    if not selected_pairs:
        raise ValueError("no pairs selected by --task")
    for pair in selected_pairs:
        for method in methods:
            run_dir = generation_dir(output_root, pair, method)
            run_path = run_dir / "generation_run.json"
            response_path = run_dir / "candidate_response.json"
            if not run_path.is_file() or not response_path.is_file():
                write_jsonl(dispatch, {
                    "pair_id": pair["pair_id"], "task_id": pair["task_id"],
                    "mutant_id": pair["mutant_id"], "method": method,
                    "status": "skipped_generation_incomplete",
                    "at_utc": datetime.now(timezone.utc).isoformat(),
                })
                continue
            if (run_dir / "frozen_test_suite.json").is_file():
                write_jsonl(dispatch, {
                    "pair_id": pair["pair_id"], "task_id": pair["task_id"],
                    "mutant_id": pair["mutant_id"], "method": method,
                    "status": "already_frozen_skipped",
                    "at_utc": datetime.now(timezone.utc).isoformat(),
                })
                continue
            review_path = run_dir / "correctness_review.json"
            if not review_path.is_file():
                write_jsonl(dispatch, {
                    "pair_id": pair["pair_id"], "task_id": pair["task_id"],
                    "mutant_id": pair["mutant_id"], "method": method,
                    "status": "skipped_blind_review_missing",
                    "at_utc": datetime.now(timezone.utc).isoformat(),
                })
                continue

            runtime_dir = ROOT / pair["runtime_dir"]
            baseline_audit = ROOT / pair["base_suite_audit"]
            command = [
                sys.executable, str(VALIDATOR),
                "--generation-dir", str(run_dir),
                "--runtime-dir", str(runtime_dir),
                "--correctness-review", str(review_path),
                "--repeats", "3",
                "--target-repeat-runs", "3",
                "--timeout-seconds", str(args.timeout_seconds),
                "--mutmut-timeout-seconds", str(args.mutmut_timeout_seconds),
                "--baseline-audit", str(baseline_audit),
            ]
            if args.paired_direct_diff_validation:
                command.append("--paired-direct-diff-validation")
                guided_input = read_json(generation_dir(output_root, pair, "mutation_guided") / "input_manifest.json")
                target_records_path = guided_input.get("mutation_record_set_path")
                target_records_sha256 = guided_input.get("mutation_record_set_sha256")
                if not isinstance(target_records_path, str) or not isinstance(target_records_sha256, str):
                    raise ValueError(f"paired generation lacks frozen survivor-record evidence: {pair['pair_id']}")
                command.extend([
                    "--paired-target-record-set", target_records_path,
                    "--paired-target-record-set-sha256", target_records_sha256,
                ])
            if method == "vanilla_llm":
                command.extend(["--paired-target-mutant", pair["mutant_id"]])
            started = datetime.now(timezone.utc)
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=args.mutmut_timeout_seconds + args.timeout_seconds * 8,
                    cwd=ROOT,
                    check=False,
                )
                stdout, stderr, exit_code = result.stdout, result.stderr, result.returncode
                status = "completed" if exit_code == 0 and (run_dir / "frozen_test_suite.json").is_file() else "failed"
            except subprocess.TimeoutExpired as error:
                stdout = error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
                stderr = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
                stderr += "\nPaired validation driver timed out.\n"
                exit_code, status = 124, "timed_out"
            (run_dir / "pairwise_validation.stdout.log").write_text(stdout, encoding="utf-8")
            (run_dir / "pairwise_validation.stderr.log").write_text(stderr, encoding="utf-8")
            write_jsonl(dispatch, {
                "pair_id": pair["pair_id"], "task_id": pair["task_id"],
                "mutant_id": pair["mutant_id"], "method": method,
                "status": status, "exit_code": exit_code,
                "output_dir": str(run_dir.relative_to(ROOT).as_posix()),
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

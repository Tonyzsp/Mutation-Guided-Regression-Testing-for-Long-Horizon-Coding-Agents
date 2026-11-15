"""Blindly review all controlled-pairwise candidate tests per task."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import subprocess
import tempfile
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "artifacts" / "results" / "controlled_pairwise_v1_20261019" / "sample_manifest.json"
RESULTS_ROOT = ROOT / "artifacts" / "results"
REVIEW_SCHEMA = ROOT / "schemas" / "candidate-correctness-review.schema.json"
REVIEWER = "Codex blind specification review (controlled pairwise v1)"
SEED = 20261020


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def slug(value: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def generation_dir(root: Path, pair: dict[str, Any], method: str) -> Path:
    return root / pair["task_id"] / slug(pair["mutant_id"]) / method


def legal_base_files(package_root: Path, relative_root: str) -> list[dict[str, str]]:
    base_root = (package_root / relative_root).resolve(strict=True)
    files = []
    for path in sorted(p for p in base_root.rglob("*") if p.is_file()):
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if len(content.encode("utf-8")) > 250_000:
            continue
        files.append({"path": path.relative_to(base_root).as_posix(), "content": content})
    return files


def render_prompt(specification: str, base_files: list[dict[str, str]], candidates: list[dict[str, str]]) -> str:
    return f"""You are an independent reviewer deciding whether each anonymized pytest candidate asserts only behavior required by the current checkpoint's public specification.

Review every candidate independently. You see only the current C1 specification, the complete legal C1 base tests, and candidate test code. You do not know how any candidate was generated, which method produced it, or which mutation it may distinguish. Do not infer behavior absent from the specification. Do not use hidden tests, future checkpoints, external files, or later failures.

For each test, return exactly one verdict:
- correct: every asserted expected behavior is directly required by the specification, externally observable, and has a deterministic public oracle;
- incorrect: an assertion is unsupported, contradicts the specification, or relies on incidental implementation details;
- uncertain: the provided specification and base tests are insufficient to decide.

Give a short rationale naming the specification requirement or the missing evidence. Do not provide private chain-of-thought. Return one review for every candidate alias and no extras. Set reviewer to \"{REVIEWER}\".

## C1 specification

{specification}

## Complete legal C1 base tests

{json.dumps(base_files, ensure_ascii=False, indent=2)}

## Anonymized candidate tests

{json.dumps(candidates, ensure_ascii=False, indent=2)}
"""


def usage_from_events(text: str) -> dict[str, int] | None:
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        usage = event.get("usage") or event.get("token_usage")
        if isinstance(usage, dict):
            input_tokens = usage.get("input_tokens", usage.get("input"))
            output_tokens = usage.get("output_tokens", usage.get("output"))
            if isinstance(input_tokens, int) and isinstance(output_tokens, int):
                return {"input_tokens": input_tokens, "output_tokens": output_tokens}
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()

    manifest_path = args.manifest.resolve(strict=True)
    manifest = read_json(manifest_path)
    output_root = args.output_root.resolve() if args.output_root else manifest_path.parent.resolve()
    if not output_root.is_relative_to(RESULTS_ROOT.resolve()):
        raise ValueError("blind review output must remain under artifacts/results")

    candidates_by_task: dict[str, list[dict[str, Any]]] = {}
    empty_generation_dirs: list[tuple[Path, str]] = []
    for pair in manifest["pairs"]:
        for method in ("vanilla_llm", "mutation_guided"):
            run_dir = generation_dir(output_root, pair, method)
            response_path = run_dir / "candidate_response.json"
            if not response_path.is_file():
                continue
            response = read_json(response_path)
            tests = response.get("tests")
            if not isinstance(tests, list):
                raise ValueError(f"candidate response has no tests array: {response_path}")
            if len(tests) > 1:
                raise ValueError(f"per-call candidate budget exceeded: {response_path}")
            if not tests:
                empty_generation_dirs.append((run_dir, method))
            for index, candidate in enumerate(tests):
                candidates_by_task.setdefault(pair["task_id"], []).append({
                    "pair_id": pair["pair_id"],
                    "method": method,
                    "generation_dir": run_dir,
                    "relative_path": candidate.get("relative_path"),
                    "content": candidate.get("content"),
                    "original_candidate_path": candidate.get("relative_path"),
                })

    for run_dir, method in empty_generation_dirs:
        write_json(run_dir / "correctness_review.json", {"reviews": []})
        write_json(run_dir / "correctness_review_run.json", {
            "status": "not_needed_no_candidates",
            "candidate_count": 0,
            "method": method,
            "model": None,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        })

    mapping_records = []
    for task, candidates in sorted(candidates_by_task.items()):
        package_root = ROOT / manifest["tasks"][task]["package"]
        package = read_json(package_root / "package.json")
        specification = (package_root / package["specification_path"]).read_text(encoding="utf-8")
        base_files = legal_base_files(package_root, package["base_tests_path"])
        rng = random.Random(SEED + sum(ord(char) for char in task))
        rng.shuffle(candidates)
        anonymized = []
        for index, candidate in enumerate(candidates, 1):
            alias = f"test_pair_{index:03d}.py"
            candidate["alias"] = alias
            anonymized.append({"candidate_path": alias, "content": candidate["content"]})
            mapping_records.append({
                "task_id": task,
                "alias": alias,
                "pair_id": candidate["pair_id"],
                "method": candidate["method"],
                "generation_dir": str(candidate["generation_dir"].relative_to(ROOT).as_posix()),
                "original_candidate_path": candidate["original_candidate_path"],
                "content_sha256": sha256(str(candidate["content"]).encode("utf-8")),
            })

        prompt = render_prompt(specification, base_files, anonymized)
        review_root = output_root / "blind_reviews" / task
        review_root.mkdir(parents=True, exist_ok=True)
        (review_root / "review_prompt.md").write_text(prompt, encoding="utf-8")
        (review_root / "review_mapping.json").write_text(
            json.dumps({"seed": SEED, "records": [x for x in mapping_records if x["task_id"] == task]}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        codex = shutil.which("codex")
        if codex is None:
            raise RuntimeError("Codex CLI was not found on PATH")
        with tempfile.TemporaryDirectory(prefix="pairwise-blind-review-", dir=ROOT / "tmp") as isolated:
            isolated_path = Path(isolated)
            last_message = isolated_path / "review.json"
            command = [
                codex, "exec", "--ephemeral", "--model", args.model,
                "--sandbox", "read-only", "--skip-git-repo-check", "--cd", str(isolated_path),
                "--output-schema", str(REVIEW_SCHEMA), "--output-last-message", str(last_message), "--json", "-",
            ]
            command[2:2] = ["-c", f'model_reasoning_effort="{args.reasoning_effort}"']
            result = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=args.timeout_seconds,
                cwd=isolated_path,
                check=False,
            )
            events, stderr = result.stdout, result.stderr
            response_text = last_message.read_text(encoding="utf-8") if last_message.is_file() else ""

        (review_root / "review_events.jsonl").write_text(events, encoding="utf-8")
        (review_root / "review_stderr.log").write_text(stderr, encoding="utf-8")
        if result.returncode != 0 or not response_text:
            write_json(review_root / "review_run.json", {
                "status": "failed",
                "exit_code": result.returncode,
                "candidate_count": len(candidates),
                "model": args.model,
                "reasoning_effort": args.reasoning_effort,
                "prompt_sha256": sha256(prompt.encode("utf-8")),
                "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            })
            raise RuntimeError(f"blind review call failed for {task}: {review_root / 'review_stderr.log'}")

        response = json.loads(response_text)
        review_rows = response.get("reviews") if isinstance(response, dict) else None
        aliases = {item["alias"] for item in candidates}
        reviewed = {row.get("candidate_path") for row in review_rows or [] if isinstance(row, dict)}
        if not isinstance(review_rows, list) or reviewed != aliases or len(review_rows) != len(aliases):
            raise ValueError(f"blind reviewer returned a different candidate set for {task}")
        by_alias = {row["candidate_path"]: row for row in review_rows}
        for candidate in candidates:
            row = by_alias[candidate["alias"]]
            if row.get("verdict") not in {"correct", "incorrect", "uncertain"}:
                raise ValueError(f"invalid blind review verdict for {candidate['alias']}")
            original_path = candidate["original_candidate_path"]
            review = {
                "reviews": [{
                    "candidate_path": original_path,
                    "verdict": row["verdict"],
                    "rationale": row["rationale"],
                    "reviewer": REVIEWER,
                }]
            }
            generation_path = candidate["generation_dir"]
            write_json(generation_path / "correctness_review.json", review)
            write_json(generation_path / "correctness_review_run.json", {
                "status": "complete",
                "candidate_count": 1,
                "blind_alias": candidate["alias"],
                "batch_review_path": str((review_root / "review_run.json").relative_to(ROOT).as_posix()),
                "model": args.model,
                "reasoning_effort": args.reasoning_effort,
                "verdict": row["verdict"],
                "rationale": row["rationale"],
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
            })

        usage = usage_from_events(events)
        write_json(review_root / "review_run.json", {
            "status": "complete",
            "candidate_count": len(candidates),
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
            "token_usage": usage,
            "usage_available": usage is not None,
            "prompt_sha256": sha256(prompt.encode("utf-8")),
            "review_sha256": sha256(response_text.encode("utf-8")),
            "exit_code": result.returncode,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        })
        print(f"Blindly reviewed {len(candidates)} candidates for {task}: {review_root}")

    write_json(output_root / "blind_review_mapping.json", {
        "seed": SEED,
        "reviewer_prompt_excludes": ["method", "mutant ID", "mutant diff", "specification_basis", "generation assessment"],
        "records": mapping_records,
    })
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

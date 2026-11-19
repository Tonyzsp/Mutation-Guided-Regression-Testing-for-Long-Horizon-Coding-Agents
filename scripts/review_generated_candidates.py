"""Obtain a separate, archived C1 specification review for generated test candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from model_input_policy import exclusions_for_package

RESULTS = ROOT / "artifacts" / "results"
SCHEMA = ROOT / "schemas" / "candidate-correctness-review.schema.json"
REVIEWER = "Codex independent specification review"
SAFE_NAME = re.compile(r"test_[A-Za-z0-9_]+\.py$")
STUDY_BUDGET = ROOT / "config" / "rq1_full_study_budget_v1.json"
STUDY_BUDGET_LOCK = ROOT / "config" / "rq1_full_study_budget_v1.lock.json"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def filesystem_path(path: Path) -> str:
    value = os.path.abspath(os.fspath(path))
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        return "\\\\?\\" + value
    return value


def package_text_files(
    package_root: Path,
    relative_root: str,
    *,
    excluded_relative_paths: set[str] | None = None,
) -> list[tuple[str, str]]:
    root = (package_root / relative_root).resolve(strict=True)
    rows: list[tuple[str, str]] = []
    root_ext = filesystem_path(root)
    found: list[tuple[str, str]] = []
    excluded_relative_paths = excluded_relative_paths or set()
    for current, directories, names in os.walk(root_ext):
        directories.sort()
        for name in sorted(names):
            absolute = os.path.join(current, name)
            relative = os.path.relpath(absolute, root_ext).replace(os.sep, "/")
            if relative in excluded_relative_paths:
                continue
            found.append((relative, absolute))
    for relative, absolute in found:
        size = os.path.getsize(absolute)
        digest = hashlib.sha256()
        with open(absolute, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        file_sha = digest.hexdigest()
        if size > 250_000:
            content = f"[Legal base-test fixture omitted because it exceeds 250000 bytes; bytes={size}; sha256={file_sha}. Do not infer its contents.]"
        else:
            try:
                with open(absolute, "r", encoding="utf-8") as handle:
                    content = handle.read()
            except (UnicodeDecodeError, OSError):
                content = f"[Legal binary base-test fixture; bytes={size}; sha256={file_sha}. Do not infer its contents.]"
        rows.append((relative, content))
    return rows


def load_locked_study_budget(path: Path) -> tuple[dict[str, Any], str]:
    resolved = path.resolve(strict=True)
    actual_sha = sha256(resolved.read_bytes())
    lock_path = resolved.with_name(f"{resolved.stem}.lock.json")
    lock = read_json(lock_path)
    if (
        not resolved.is_relative_to(ROOT.resolve(strict=True))
        or lock.get("budget_path") != resolved.relative_to(ROOT).as_posix()
        or lock.get("sha256") != actual_sha
    ):
        raise ValueError("study-budget SHA-256 differs from its adjacent lock")
    budget = read_json(resolved)
    if budget.get("experiment_id") != lock.get("experiment_id"):
        raise ValueError("study-budget experiment ID differs from its lock")
    return budget, actual_sha


def render_prompt(
    package_root: Path,
    package: dict[str, Any],
    candidates: list[dict[str, Any]],
    requested_output_tokens: int,
) -> tuple[str, list[dict[str, Any]], str]:
    specification = (package_root / package["specification_path"]).read_text(encoding="utf-8")
    excluded_paths, excluded_records, exclusion_policy_sha = exclusions_for_package(
        package_root, package, root_name="base_tests", consumer="candidate_correctness_review"
    )
    base_files = package_text_files(
        package_root, package["base_tests_path"], excluded_relative_paths=excluded_paths
    )
    candidate_rows = []
    for item in candidates:
        candidate_rows.append({
            "candidate_path": item["relative_path"],
            "specification_basis": item["specification_basis"],
            "content": item["content"],
        })
    exclusion_note = ""
    if excluded_records:
        exclusion_note = "\n\nModel-input exclusions applied: " + "; ".join(
            f"{row['path']} omitted ({row['reason']}; sha256={row['sha256']})"
            for row in excluded_records
        ) + ". The original package is preserved for runtime test execution."
    prompt = f"""You are an independent reviewer deciding whether generated pytest candidates assert only behavior required by this checkpoint's public specification.

Use only the current C1 specification and legal C1 test/fixture inventory included below, after applying the model-input exclusion policy. Every remaining base-test file is listed; text files up to 250000 bytes are included verbatim, while binary or larger fixture contents are represented by their path, byte length, and hash. Do not infer omitted binary or large-file contents. Do not use external files, hidden tests, future checkpoints, or later failures. Collection, stability, and source-checkpoint execution are assessed by a separate validator; do not assume a candidate passes those checks. Your task here is only semantic correctness. The base tests are evidence of current coverage, not a license to assert behavior absent from the specification.{exclusion_note}

Requested output-token target: {requested_output_tokens}. Keep each rationale concise. This is a requested target; the Codex CLI does not provide a hard output-token ceiling.

Many candidates build synthetic inputs under pytest's temporary directory. Treat files and rows explicitly written by the test as the actual test fixture; do not assume they are missing because they are not in the checkpoint package. Read helper functions and their arguments to recover the exact fixture values. Apply documented units literally (for example, an explicitly written zero coordinate is a valid position). If the relevant fixture data is present, reason from those values and the specification before choosing `uncertain`. Separate collection and repeat-execution checks occur before freezing; this review decides only semantic correctness.

For each candidate, return exactly one verdict:
- correct: every asserted expected behavior is directly required by the C1 specification and the test has a deterministic public oracle;
- incorrect: at least one assertion is unsupported or conflicts with the specification;
- uncertain: the provided evidence is insufficient to decide.

Give a short rationale naming the relevant specification requirement or the missing/unsupported evidence. Do not provide private chain-of-thought. Return one review for every candidate path, no extras. Set reviewer to \"{REVIEWER}\".

## C1 specification
\n{specification}

## Complete legal C1 base tests
\n{json.dumps([{"path": name, "content": text} for name, text in base_files], ensure_ascii=False, indent=2)}

## Candidate tests
\n{json.dumps(candidate_rows, ensure_ascii=False, indent=2)}
"""
    return prompt, excluded_records, exclusion_policy_sha


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
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--generation-dir", required=True, type=Path)
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--study-budget-config", type=Path, default=STUDY_BUDGET)
    parser.add_argument("--prepared-prompt", type=Path, help="Use a hash-matched locally prepared prompt")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()

    package_root = args.package.resolve(strict=True)
    generation_dir = args.generation_dir.resolve(strict=True)
    if not generation_dir.is_relative_to(RESULTS.resolve(strict=True)):
        raise ValueError("generation directory must remain under artifacts/results")
    review_artifacts = (
        "correctness_review_prompt.md",
        "correctness_review_events.jsonl",
        "correctness_review_stderr.log",
        "correctness_review.json",
        "correctness_review_run.json",
    )
    if any((generation_dir / name).exists() for name in review_artifacts):
        raise FileExistsError("correctness review attempt already exists; refusing to repeat or overwrite it")
    study_budget, study_budget_sha = load_locked_study_budget(args.study_budget_config)
    review_budget = study_budget["candidate_review"]
    if args.model != review_budget["reviewer_model"] or args.reasoning_effort != review_budget["reasoning_effort"]:
        raise ValueError("candidate-review model/reasoning setting differs from the frozen study budget")
    package = read_json(package_root / "package.json")
    response = read_json(generation_dir / "candidate_response.json")
    candidates = response.get("tests")
    if not isinstance(candidates, list):
        raise ValueError("candidate response does not contain a tests array")
    if len(candidates) > review_budget["candidates_per_review_call_max"]:
        raise ValueError("candidate batch exceeds the frozen per-review candidate limit")
    seen: set[str] = set()
    for candidate in candidates:
        relative = candidate.get("relative_path")
        if not isinstance(relative, str) or SAFE_NAME.fullmatch(relative) is None:
            raise ValueError(f"unsafe candidate path: {relative!r}")
        if relative in seen:
            raise ValueError(f"duplicate candidate path: {relative}")
        seen.add(relative)
        if not isinstance(candidate.get("content"), str) or not isinstance(candidate.get("specification_basis"), str):
            raise ValueError(f"candidate {relative} has incomplete content or specification basis")

    requested_output_tokens = int(review_budget["requested_output_tokens_per_call"])
    prompt, excluded_model_inputs, exclusion_policy_sha = render_prompt(
        package_root, package, candidates, requested_output_tokens
    )
    if args.prepared_prompt is not None:
        prepared_path = args.prepared_prompt.resolve(strict=True)
        if not prepared_path.is_relative_to(RESULTS.resolve(strict=True)):
            raise ValueError("prepared review prompt must remain under artifacts/results")
        prepared_manifest_path = prepared_path.with_name("input_manifest.json")
        prepared_manifest = read_json(prepared_manifest_path)
        expected_candidate_sha = sha256((generation_dir / "candidate_response.json").read_bytes())
        if (
            prepared_manifest.get("task_id") != package["task_id"]
            or prepared_manifest.get("study_budget_sha256") != study_budget_sha
            or prepared_manifest.get("source_candidate_response_sha256") != expected_candidate_sha
            or prepared_manifest.get("candidate_count") != len(candidates)
            or prepared_manifest.get("prompt_sha256") != sha256(prepared_path.read_text(encoding="utf-8").encode("utf-8"))
        ):
            raise ValueError("prepared candidate-review prompt manifest does not match current inputs")
        prepared_prompt = prepared_path.read_text(encoding="utf-8")
        if prepared_prompt != prompt:
            raise ValueError("prepared candidate-review prompt differs from the reconstructed current prompt")
        prompt = prepared_prompt
    prompt_path = generation_dir / "correctness_review_prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    output_path = generation_dir / "correctness_review.json"
    run_path = generation_dir / "correctness_review_run.json"
    if not candidates:
        review = {"reviews": []}
        output_path.write_text(json.dumps(review, indent=2) + "\n", encoding="utf-8")
        run_path.write_text(json.dumps({
            "status": "not_needed_no_candidates",
            "candidate_count": 0,
            "call_count": 0,
            "study_budget_id": study_budget["experiment_id"],
            "study_budget_sha256": study_budget_sha,
            "requested_output_tokens": requested_output_tokens,
            "model": None,
            "reasoning_effort": None,
            "source_specification_sha256": sha256((package_root / package["specification_path"]).read_bytes()),
            "base_tests_sha256": package["base_tests_sha256"],
            "model_input_exclusion_policy_sha256": exclusion_policy_sha,
            "excluded_model_inputs": excluded_model_inputs,
            "candidate_response_sha256": sha256((generation_dir / "candidate_response.json").read_bytes()),
            "prompt_sha256": sha256(prompt.encode("utf-8")),
            "review_sha256": sha256(output_path.read_bytes()),
            "token_usage": None,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        }, indent=2) + "\n", encoding="utf-8")
        return 0

    codex = shutil.which("codex")
    if codex is None:
        raise RuntimeError("Codex CLI was not found")
    with tempfile.TemporaryDirectory(prefix="rq1-review-") as isolated:
        isolated_path = Path(isolated)
        last_message = isolated_path / "review.json"
        command = [
            codex, "exec", "--ephemeral", "--model", args.model,
            "--sandbox", "read-only", "--skip-git-repo-check", "--cd", str(isolated_path),
            "--output-schema", str(SCHEMA), "--output-last-message", str(last_message), "--json", "-",
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
        events = result.stdout
        stderr = result.stderr
        captured = last_message.is_file()
        response_text = last_message.read_text(encoding="utf-8") if captured else ""

    (generation_dir / "correctness_review_events.jsonl").write_text(events, encoding="utf-8")
    (generation_dir / "correctness_review_stderr.log").write_text(stderr, encoding="utf-8")
    if result.returncode != 0 or not captured:
        run_path.write_text(json.dumps({
            "status": "failed",
            "exit_code": result.returncode,
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
            "prompt_sha256": sha256(prompt.encode("utf-8")),
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        }, indent=2) + "\n", encoding="utf-8")
        raise RuntimeError(f"review call failed; see {generation_dir / 'correctness_review_stderr.log'}")

    review = json.loads(response_text)
    rows = review.get("reviews") if isinstance(review, dict) else None
    if not isinstance(rows, list):
        raise ValueError("review response does not contain a reviews array")
    reviewed = {item.get("candidate_path") for item in rows if isinstance(item, dict)}
    if len(rows) != len(candidates) or reviewed != seen:
        raise ValueError(f"reviewed candidate paths do not match: expected={sorted(seen)}, got={sorted(str(x) for x in reviewed)}")
    for item in rows:
        if item.get("reviewer") != REVIEWER:
            item["reviewer"] = REVIEWER
    output_path.write_text(json.dumps(review, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    usage = usage_from_events(events)
    run_path.write_text(json.dumps({
        "status": "complete",
        "candidate_count": len(candidates),
        "call_count": 1,
        "study_budget_id": study_budget["experiment_id"],
        "study_budget_sha256": study_budget_sha,
        "requested_output_tokens": requested_output_tokens,
        "source_specification_sha256": sha256((package_root / package["specification_path"]).read_bytes()),
        "base_tests_sha256": package["base_tests_sha256"],
        "model_input_exclusion_policy_sha256": exclusion_policy_sha,
        "excluded_model_inputs": excluded_model_inputs,
        "candidate_response_sha256": sha256((generation_dir / "candidate_response.json").read_bytes()),
        "prompt_sha256": sha256(prompt.encode("utf-8")),
        "review_sha256": sha256(output_path.read_bytes()),
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "token_usage": usage,
        "usage_available": usage is not None,
        "exit_code": result.returncode,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }, indent=2) + "\n", encoding="utf-8")
    print(f"Reviewed {len(candidates)} candidates; result: {output_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

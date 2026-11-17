"""Obtain a separate, archived C1 specification review for generated test candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "artifacts" / "results"
SCHEMA = ROOT / "schemas" / "candidate-correctness-review.schema.json"
REVIEWER = "Codex independent specification review"
SAFE_NAME = re.compile(r"test_[A-Za-z0-9_]+\.py$")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def package_text_files(package_root: Path, relative_root: str) -> list[tuple[str, str]]:
    root = (package_root / relative_root).resolve(strict=True)
    rows: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if len(content.encode("utf-8")) > 250_000:
            continue
        rows.append((path.relative_to(root).as_posix(), content))
    return rows


def render_prompt(package_root: Path, package: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    specification = (package_root / package["specification_path"]).read_text(encoding="utf-8")
    base_files = package_text_files(package_root, package["base_tests_path"])
    candidate_rows = []
    for item in candidates:
        candidate_rows.append({
            "candidate_path": item["relative_path"],
            "specification_basis": item["specification_basis"],
            "content": item["content"],
        })
    return f"""You are an independent reviewer deciding whether generated pytest candidates assert only behavior required by this checkpoint's public specification.

Use only the current C1 specification and complete legal C1 base tests included below. Do not use external files, hidden tests, future checkpoints, or later failures. The test code has already been separately checked for collection, stability, and whether it passes unmodified C1; your task is only semantic correctness. The base tests are evidence of current coverage, not a license to assert behavior absent from the specification.

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
    parser.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args()

    package_root = args.package.resolve(strict=True)
    generation_dir = args.generation_dir.resolve(strict=True)
    if not generation_dir.is_relative_to(RESULTS.resolve(strict=True)):
        raise ValueError("generation directory must remain under artifacts/results")
    if (generation_dir / "correctness_review.json").exists():
        raise FileExistsError("correctness review already exists; refusing to overwrite it")
    package = read_json(package_root / "package.json")
    response = read_json(generation_dir / "candidate_response.json")
    candidates = response.get("tests")
    if not isinstance(candidates, list):
        raise ValueError("candidate response does not contain a tests array")
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

    prompt = render_prompt(package_root, package, candidates)
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
            "prompt_sha256": sha256(prompt.encode("utf-8")),
            "review_sha256": sha256(output_path.read_bytes()),
            "model": None,
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

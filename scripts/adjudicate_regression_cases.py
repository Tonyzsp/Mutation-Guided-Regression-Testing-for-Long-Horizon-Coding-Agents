"""Run the fixed, method-blinded GPT-6 Luna judge on prepared RQ1 cases."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "regression-adjudication-review.schema.json"
PROMPT = ROOT / "prompts" / "regression_adjudication_v1.md"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
    parser.add_argument("--adjudication-dir", required=True, type=Path)
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()

    adjudication_dir = args.adjudication_dir.resolve(strict=True)
    cases_dir = (adjudication_dir / "cases").resolve(strict=True)
    if not cases_dir.is_relative_to(adjudication_dir):
        raise ValueError("cases directory escaped adjudication directory")
    review_dir = adjudication_dir / "reviews"
    review_dir.mkdir(exist_ok=True)
    codex = shutil.which("codex")
    if codex is None:
        raise RuntimeError("Codex CLI was not found")
    template = PROMPT.read_text(encoding="utf-8")
    cases = sorted(cases_dir.glob("case-*.json"))
    if not cases:
        raise ValueError("no prepared adjudication cases")
    results: list[dict[str, Any]] = []
    call_failures: list[dict[str, str]] = []

    for index, case_path in enumerate(cases, 1):
        case = json.loads(case_path.read_text(encoding="utf-8"))
        case_id = case["case_id"]
        case_review_dir = review_dir / case_id
        case_review_dir.mkdir(exist_ok=True)
        response_path = case_review_dir / "review.json"
        run_path = case_review_dir / "run_manifest.json"
        previous_run = json.loads(run_path.read_text(encoding="utf-8")) if run_path.is_file() else None
        if response_path.is_file() and previous_run and previous_run.get("status") == "complete":
            results.extend(json.loads(response_path.read_text(encoding="utf-8"))["reviews"])
            print(f"[{index}/{len(cases)}] REUSE {case_id}", flush=True)
            continue

        prior_attempt_archive: str | None = None
        if previous_run and previous_run.get("status") == "inconclusive_call_failure":
            archive_root = case_review_dir / "prior_failed_attempts"
            archive_root.mkdir(exist_ok=True)
            archive_dir = archive_root / str(len(list(archive_root.iterdir())) + 1)
            archive_dir.mkdir()
            for prior_file in case_review_dir.iterdir():
                if prior_file.is_file():
                    shutil.copy2(prior_file, archive_dir / prior_file.name)
            prior_attempt_archive = str(archive_dir.relative_to(adjudication_dir))

        prompt_text = template + "\n\n## Candidate case (JSON)\n\n" + json.dumps(case, ensure_ascii=False, indent=2)
        (case_review_dir / "prompt.md").write_text(prompt_text, encoding="utf-8")
        attempts: list[dict[str, Any]] = []
        accepted: dict[str, Any] | None = None
        for attempt in range(1, 3):
            with tempfile.TemporaryDirectory(prefix=f"rq1-judge-{case_id}-") as isolated:
                isolated_path = Path(isolated)
                last_message = isolated_path / "review.json"
                command = [
                    codex, "exec", "--ephemeral", "--model", args.model,
                    "--sandbox", "read-only", "--skip-git-repo-check", "--cd", str(isolated_path),
                    "--output-schema", str(SCHEMA), "--output-last-message", str(last_message), "--json", "-",
                ]
                command[2:2] = ["-c", f'model_reasoning_effort="{args.reasoning_effort}"']
                try:
                    completed = subprocess.run(
                        command,
                        input=prompt_text,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=args.timeout_seconds,
                        cwd=isolated_path,
                        check=False,
                    )
                    events = completed.stdout
                    stderr = completed.stderr
                    captured = last_message.is_file()
                    response_text = last_message.read_text(encoding="utf-8") if captured else ""
                    exit_code = completed.returncode
                except subprocess.TimeoutExpired as error:
                    events = error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
                    stderr = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
                    captured = False
                    response_text = ""
                    exit_code = None
                attempt_record = {
                    "attempt": attempt,
                    "exit_code": exit_code,
                    "structured_response_captured": captured,
                    "token_usage": usage_from_events(events),
                    "stderr_sha256": sha256(stderr.encode("utf-8")),
                }
                attempts.append(attempt_record)
                (case_review_dir / f"events-{attempt}.jsonl").write_text(events, encoding="utf-8")
                (case_review_dir / f"stderr-{attempt}.log").write_text(stderr, encoding="utf-8")
                if exit_code == 0 and captured:
                    try:
                        parsed = json.loads(response_text)
                    except json.JSONDecodeError as error:
                        parsed = None
                        attempt_record["validation_error"] = f"judge output was not valid JSON: {error}"
                    reviews = parsed.get("reviews") if isinstance(parsed, dict) else None
                    if isinstance(reviews, list) and len(reviews) == 1 and reviews[0].get("case_id") == case_id:
                        accepted = reviews[0]
                        if float(accepted["confidence"]) < 0.80 or accepted["verdict"] == "inconclusive":
                            accepted["human_review_recommended"] = True
                        parsed["reviews"] = [accepted]
                        response_path.write_text(json.dumps(parsed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                        break
                    if "validation_error" not in attempt_record:
                        attempt_record["validation_error"] = "judge response did not contain exactly this case ID"
                    attempt_record["structured_response_captured"] = captured

        if accepted is None:
            accepted = {
                "case_id": case_id,
                "verdict": "inconclusive",
                "confidence": 0.0,
                "issue_summary": "LLM adjudication unavailable",
                "issue_key": "llm_judge_unavailable",
                "rationale": "The fixed LLM judge did not return a valid structured review within the allowed attempts; the case is inconclusive under the LLM-only protocol.",
                "evidence_refs": ["judge_run_manifest", "judge_attempt_logs"],
                "human_review_recommended": True,
            }
            response_path.write_text(json.dumps({"reviews": [accepted]}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            call_failures.append({"case_id": case_id, "reason": "judge response missing or invalid after allowed attempts"})

        run_manifest = {
            "status": "complete" if not call_failures or call_failures[-1].get("case_id") != case_id else "inconclusive_call_failure",
            "case_id": case_id,
            "case_sha256": sha256(case_path.read_bytes()),
            "prompt_sha256": sha256(prompt_text.encode("utf-8")),
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
            "attempts": attempts,
            "prior_attempt_archive": prior_attempt_archive,
            "token_usage": next((item.get("token_usage") for item in attempts if item.get("token_usage")), None),
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        run_path.write_text(json.dumps(run_manifest, indent=2) + "\n", encoding="utf-8")
        confidence = float(accepted["confidence"])
        results.append(accepted)
        suffix = "; judge call failed" if call_failures and call_failures[-1].get("case_id") == case_id else ""
        print(f"[{index}/{len(cases)}] {case_id}: {accepted['verdict']} ({confidence:.2f}{suffix})", flush=True)

    (adjudication_dir / "llm_judge_reviews.json").write_text(
        json.dumps({"reviews": results}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    summary = {
        "format": "rq1-llm-adjudication/v1",
        "status": "complete" if len(results) == len(cases) else "incomplete",
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "case_count": len(cases),
        "review_count": len(results),
        "error_count": 0,
        "call_failure_count": len(call_failures),
        "call_failures": call_failures,
        "inconclusive_count": sum(
            row.get("verdict") == "inconclusive" or row.get("human_review_recommended") is True
            for row in results
        ),
        "reviews_path": "llm_judge_reviews.json",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    (adjudication_dir / "llm_judge_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if len(results) == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())

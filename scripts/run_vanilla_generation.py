"""Generate Vanilla pytest candidates in an isolated Codex CLI session."""

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
from pathlib import Path, PurePosixPath
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACKAGE_ROOT = REPO_ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
PROMPT_TEMPLATE = REPO_ROOT / "prompts" / "vanilla_v1.md"
OUTPUT_SCHEMA = REPO_ROOT / "schemas" / "codex-generated-test-candidates.schema.json"
BUDGET_CONFIG = REPO_ROOT / "config" / "generation_budget.json"
STUDY_BUDGET_CONFIG = REPO_ROOT / "config" / "rq1_full_study_budget_v1.json"
PACKAGE_AUDITOR = REPO_ROOT / "scripts" / "validate_checkpoint_package.py"
MAX_INPUT_FILE_BYTES = 2_000_000


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def filesystem_path(path: Path) -> str:
    value = os.path.abspath(os.fspath(path))
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        return "\\\\?\\" + value
    return value


def read_text_file(path: Path, encoding: str = "utf-8") -> str:
    return Path(filesystem_path(path)).read_text(encoding=encoding)


def write_text_file(path: Path, content: str, encoding: str = "utf-8") -> None:
    Path(filesystem_path(path)).write_text(content, encoding=encoding)


def file_exists(path: Path) -> bool:
    return os.path.exists(filesystem_path(path))


def file_is_file(path: Path) -> bool:
    return os.path.isfile(filesystem_path(path))


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(read_text_file(path))


def safe_package_path(root: Path, relative: str, field: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
        raise ValueError(f"unsafe {field}: {relative!r}")
    resolved = root.joinpath(*pure.parts).resolve(strict=True)
    if not resolved.is_relative_to(root.resolve(strict=True)):
        raise ValueError(f"{field} escapes package root: {relative!r}")
    return resolved


def gather_text_files(root: Path) -> list[tuple[str, str, str, int]]:
    records: list[tuple[str, str, str, int]] = []
    root_ext = filesystem_path(root)
    files: list[tuple[str, Path]] = []
    for current, directories, names in os.walk(root_ext):
        directories.sort()
        for name in sorted(names):
            absolute = os.path.join(current, name)
            relative = os.path.relpath(absolute, root_ext).replace(os.sep, "/")
            files.append((relative, Path(absolute)))
    for relative, path in sorted(files):
        resolved = Path(filesystem_path(path)).resolve(strict=True)
        if not resolved.is_relative_to(Path(filesystem_path(root)).resolve(strict=True)):
            raise ValueError(f"input symlink escapes allowed root: {relative}")
        if any(part.lower() in {"hidden", "future", "excluded", "reference"} for part in PurePosixPath(relative).parts):
            raise ValueError(f"prohibited data path in generator inputs: {relative}")
        source_bytes = os.stat(filesystem_path(path)).st_size
        digest = hashlib.sha256()
        with open(filesystem_path(path), "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        file_hash = digest.hexdigest()
        if source_bytes > MAX_INPUT_FILE_BYTES:
            content = (
                f"[Large file omitted from the model prompt; legal C1 input only; "
                f"{source_bytes} bytes; sha256={file_hash}. Use the visible text fixtures "
                f"and test references; do not infer this file's contents.]")
            records.append((relative, content, file_hash, source_bytes))
            continue
        raw = Path(filesystem_path(path)).read_bytes()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            content = (
                f"[Non-UTF-8 file omitted from the model prompt; legal C1 input only; "
                f"{source_bytes} bytes; sha256={file_hash}. Do not infer its contents.]"
            )
            records.append((relative, content, file_hash, source_bytes))
            continue
        records.append((relative, content, file_hash, source_bytes))
    return records


def render_prompt(
    package_root: Path,
    package: dict[str, Any],
    budget: dict[str, Any],
    *,
    template_path: Path = PROMPT_TEMPLATE,
    extra_template_values: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    workspace = safe_package_path(package_root, package["workspace_path"], "workspace_path")
    base_tests = safe_package_path(package_root, package["base_tests_path"], "base_tests_path")
    specification = safe_package_path(package_root, package["specification_path"], "specification_path")
    workspace_files = gather_text_files(workspace)
    base_files = gather_text_files(base_tests)
    specification_text = read_text_file(specification)

    def format_files(files: list[tuple[str, str, str]], root_name: str) -> str:
        if not files:
            raise ValueError(f"{root_name} contains no files")
        blocks = []
        for relative, content, _, _ in files:
            blocks.append(f"\n### `{root_name}/{relative}`\n```text\n{content.rstrip()}\n```")
        return "\n".join(blocks)

    max_candidates = int(budget["max_candidates"])
    template = read_text_file(template_path)
    replacements = {
        "MAX_CANDIDATE_FILES": str(max_candidates),
        "MAX_CANDIDATE_TEST_FUNCTIONS": str(max_candidates),
        "MAX_OUTPUT_TOKENS": str(budget["max_output_tokens"]),
        "SPECIFICATION": specification_text,
        "SOURCE_SNAPSHOT": format_files(workspace_files, "workspace"),
        "BASE_TEST_SUITE": format_files(base_files, "base_tests"),
        "TARGET_MUTATION_SECTION": "",
        **(extra_template_values or {}),
    }
    prompt = template
    for name, value in replacements.items():
        prompt = prompt.replace("{{" + name + "}}", value)
    unexpanded = re.findall(r"\{\{[A-Z0-9_]+\}\}", prompt)
    if unexpanded:
        raise ValueError(f"unexpanded prompt template placeholder remains: {unexpanded}")

    input_records = [
        {
            "path": f"{package['workspace_path']}/{p}",
            "sha256": h,
            "bytes": len(c.encode("utf-8")),
            "source_bytes": source_bytes,
        }
        for p, c, h, source_bytes in workspace_files
    ]
    input_records += [
        {
            "path": f"{package['base_tests_path']}/{p}",
            "sha256": h,
            "bytes": len(c.encode("utf-8")),
            "source_bytes": source_bytes,
        }
        for p, c, h, source_bytes in base_files
    ]
    input_records.append(
        {
            "path": package["specification_path"],
            "sha256": sha256_bytes(Path(filesystem_path(specification)).read_bytes()),
            "bytes": len(specification_text.encode("utf-8")),
        }
    )
    package_digest = sha256_bytes(
        json.dumps(input_records, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    manifest = {
        "format": "rq1-generation-input/v1",
        "package_root": str(package_root),
        "task_id": package["task_id"],
        "trajectory_id": package["trajectory_id"],
        "checkpoint_id": package["checkpoint_id"],
        "workspace_revision": package["workspace_revision"],
        "base_tests_sha256": package["base_tests_sha256"],
        "generator_visible_roots": [package["workspace_path"], package["base_tests_path"]],
        "specification_path": package["specification_path"],
        "input_package_sha256": package_digest,
        "input_files": input_records,
        "excluded_roots": ["data/hidden_tests", "data/future_tests", "data/excluded_tests", "later checkpoints"],
    }
    return prompt, manifest


def find_usage(stdout: str) -> dict[str, int] | None:
    candidates: list[dict[str, int]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("usage", "token_usage"):
                usage = value.get(key)
                if isinstance(usage, dict):
                    normalized = {}
                    for target, choices in {
                        "input_tokens": ("input_tokens", "prompt_tokens"),
                        "output_tokens": ("output_tokens", "completion_tokens"),
                    }.items():
                        for name in choices:
                            if isinstance(usage.get(name), int):
                                normalized[target] = usage[name]
                                break
                    if normalized:
                        candidates.append(normalized)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for line in stdout.splitlines():
        try:
            visit(json.loads(line))
        except json.JSONDecodeError:
            continue
    return candidates[-1] if candidates else None


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not file_exists(path):
        return []
    return [json.loads(line) for line in read_text_file(path).splitlines() if line.strip()]


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    write_text_file(
        path,
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
    )


def prompt_resource(path: Path, field: str) -> dict[str, str]:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(REPO_ROOT):
        raise ValueError(f"{field} must remain inside the repository")
    return {
        "path": resolved.relative_to(REPO_ROOT).as_posix(),
        "sha256": sha256_bytes(Path(filesystem_path(resolved)).read_bytes()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True, help="Sanitized <task>/C<n> package")
    parser.add_argument("--output-dir", type=Path, required=True, help="New result directory under artifacts/results")
    parser.add_argument("--budget-config", type=Path, default=BUDGET_CONFIG)
    parser.add_argument(
        "--study-budget-config",
        type=Path,
        default=STUDY_BUDGET_CONFIG,
        help="Full-study budget record whose hash is bound into the run manifest.",
    )
    parser.add_argument("--prompt-template", type=Path, default=PROMPT_TEMPLATE)
    parser.add_argument("--opportunity-brief", type=Path)
    parser.add_argument("--output-schema", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args()

    package_root = args.package.resolve(strict=True)
    output_dir = args.output_dir.resolve()
    allowed_output = (REPO_ROOT / "artifacts" / "results").resolve(strict=True)
    if not output_dir.is_relative_to(allowed_output):
        raise ValueError(f"output directory must remain under {allowed_output}")
    output_dir_preexists = file_exists(output_dir)
    if output_dir_preexists and any(
        file_exists(output_dir / name)
        for name in ("candidate_response.json", "generation_run.json", "frozen_test_suite.json")
    ):
        raise ValueError(f"output directory already contains a completed generation or freeze: {output_dir}")

    audit = subprocess.run(
        [sys.executable, str(PACKAGE_AUDITOR), "--package", str(package_root)],
        capture_output=True,
        text=True,
        check=False,
    )
    if audit.returncode != 0:
        raise ValueError(f"checkpoint package audit failed:\n{audit.stdout}\n{audit.stderr}")

    package = read_json(package_root / "package.json")
    if package["task_id"] != package_root.parent.name or package["checkpoint_id"] != package_root.name:
        raise ValueError("package path does not match its task/checkpoint identifiers")
    budget_config = read_json(args.budget_config)
    study_budget_config = read_json(args.study_budget_config.resolve(strict=True))
    expected_generation_config = study_budget_config.get("generation_budget_config")
    if expected_generation_config != args.budget_config.resolve(strict=True).relative_to(REPO_ROOT).as_posix():
        raise ValueError("study budget does not authorize the selected generation budget configuration")
    configured_schema = budget_config.get("output_schema")
    output_schema_path = args.output_schema or (REPO_ROOT / configured_schema if configured_schema else OUTPUT_SCHEMA)
    budget = budget_config["budget"]
    prompt_budget = dict(budget)
    prompt_budget["max_output_tokens"] = budget.get("per_call_output_tokens", budget["max_output_tokens"])
    template_path = args.prompt_template.resolve(strict=True)
    extra_template_values: dict[str, str] = {}
    if args.opportunity_brief:
        brief_path = args.opportunity_brief.resolve(strict=True)
        extra_template_values["TEST_OPPORTUNITIES"] = read_text_file(brief_path)
    prompt, input_manifest = render_prompt(
        package_root,
        package,
        prompt_budget,
        template_path=template_path,
        extra_template_values=extra_template_values,
    )
    input_manifest["prompt_template"] = prompt_resource(template_path, "prompt template")
    input_manifest["output_schema"] = prompt_resource(output_schema_path.resolve(strict=True), "output schema")
    input_manifest["budget_config"] = prompt_resource(args.budget_config.resolve(strict=True), "budget configuration")
    input_manifest["study_budget_config"] = prompt_resource(
        args.study_budget_config.resolve(strict=True), "full-study budget configuration"
    )
    input_manifest["budget_id"] = budget_config["budget_id"]
    if args.opportunity_brief:
        input_manifest["opportunity_brief"] = prompt_resource(args.opportunity_brief, "opportunity brief")
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = output_dir / "prompt_archive.md"
    input_manifest_path = output_dir / "input_manifest.json"
    generation_context_preexists = file_exists(prompt_path) or file_exists(input_manifest_path)
    if generation_context_preexists:
        if not file_is_file(prompt_path) or not file_is_file(input_manifest_path):
            raise ValueError("existing generation context is incomplete")
        if not file_is_file(prompt_path) or read_text_file(prompt_path) != prompt:
            raise ValueError("existing generation directory has a different or incomplete prompt archive")
        if not file_is_file(input_manifest_path) or read_json(input_manifest_path) != input_manifest:
            raise ValueError("existing generation directory has a different or incomplete input manifest")
    else:
        write_text_file(prompt_path, prompt)
        write_text_file(input_manifest_path, json.dumps(input_manifest, indent=2) + "\n")
        write_text_file(output_dir / "package_audit.txt", audit.stdout)

    codex = shutil.which("codex")
    if codex is None:
        raise RuntimeError("Codex CLI was not found on PATH; no model call was made")
    attempts_path = output_dir / "generation_attempts.jsonl"
    attempts = read_jsonl(attempts_path)
    calls_already_counted = sum(record.get("budget_call_counted") is not False for record in attempts)
    if calls_already_counted >= int(budget["max_calls"]):
        raise ValueError("generation call budget is exhausted")
    attempt_number = len(attempts) + 1
    launched_at = datetime.now(timezone.utc)
    attempt_record: dict[str, Any] = {
        "attempt": attempt_number,
        "status": "started",
        "launched_at_utc": launched_at.isoformat(),
        "input_tokens": None,
        "output_tokens": None,
        "usage_available": False,
        "wall_seconds": None,
        "model_request_sent": None,
        "budget_call_counted": False,
    }
    attempts.append(attempt_record)
    write_jsonl(attempts_path, attempts)
    attempt_response_path = output_dir / f"candidate_response_attempt_{attempt_number:02d}.json"
    attempt_events_path = output_dir / f"codex-events_attempt_{attempt_number:02d}.jsonl"
    attempt_stderr_path = output_dir / f"codex-stderr_attempt_{attempt_number:02d}.log"
    isolated_parent = REPO_ROOT / "tmp" / "generation-isolation"
    isolated_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="rq1-vanilla-isolated-", dir=isolated_parent, ignore_cleanup_errors=True
    ) as isolated_root:
        command = [
            codex,
            "exec",
            "--ephemeral",
            "--model",
            str(budget_config["model"]["model_id"]),
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--cd",
            isolated_root,
            "--output-schema",
            filesystem_path(output_schema_path.resolve(strict=True)),
            "--output-last-message",
            filesystem_path(attempt_response_path),
            "--json",
            "-",
        ]
        reasoning_effort = budget_config["model"].get("reasoning_effort")
        if reasoning_effort:
            command[2:2] = ["-c", f'model_reasoning_effort="{reasoning_effort}"']
        started = datetime.now(timezone.utc)
        timed_out = False
        try:
            result = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=args.timeout_seconds,
                cwd=isolated_root,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            timed_out = True

            def as_text(value: str | bytes | None) -> str:
                if value is None:
                    return ""
                return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value

            result = subprocess.CompletedProcess(
                command,
                124,
                stdout=as_text(error.stdout),
                stderr=as_text(error.stderr) + f"\nCodex CLI timed out after {args.timeout_seconds} seconds.\n",
            )
        except OSError as error:
            attempts[-1].update({
                "status": "failed_before_process_start",
                "finished_at_utc": datetime.now(timezone.utc).isoformat(),
                "model_request_sent": False,
                "budget_call_counted": False,
                "failure": str(error),
                "stderr_path": attempt_stderr_path.name,
            })
            write_text_file(attempt_stderr_path, str(error))
            write_jsonl(attempts_path, attempts)
            raise RuntimeError(f"Could not start Codex CLI; no model request was sent: {error}") from error
        ended = datetime.now(timezone.utc)

    write_text_file(output_dir / attempt_events_path.name, result.stdout)
    write_text_file(output_dir / attempt_stderr_path.name, result.stderr)
    usage = find_usage(result.stdout)
    response_captured = file_is_file(attempt_response_path)
    state_init_failed = "failed to initialize in-process app-server client" in result.stderr
    model_request_sent = bool(result.stdout.strip() or response_captured)
    budget_call_counted = True if timed_out else model_request_sent or not state_init_failed
    attempts[-1].update({
        "status": "completed" if result.returncode == 0 and response_captured else (
            "failed_before_model_request" if state_init_failed else "failed_after_possible_request"
        ),
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "exit_code": result.returncode,
        "input_tokens": usage.get("input_tokens") if usage else None,
        "output_tokens": usage.get("output_tokens") if usage else None,
        "usage_available": usage is not None and "input_tokens" in usage and "output_tokens" in usage,
        "model_request_sent": None if timed_out else model_request_sent if budget_call_counted else False,
        "budget_call_counted": budget_call_counted,
        "timed_out": timed_out,
        "wall_seconds": (datetime.now(timezone.utc) - launched_at).total_seconds(),
        "response_path": attempt_response_path.name if file_is_file(attempt_response_path) else None,
        "events_path": attempt_events_path.name,
        "stderr_path": attempt_stderr_path.name,
    })
    write_jsonl(attempts_path, attempts)
    response_path = output_dir / "candidate_response.json"
    if result.returncode != 0 or not file_is_file(attempt_response_path):
        raise RuntimeError(
            f"Codex generation attempt {attempt_number} failed (exit={result.returncode}); "
            f"see {attempt_stderr_path}"
        )
    shutil.copy2(filesystem_path(attempt_response_path), filesystem_path(response_path))

    response_text = read_text_file(response_path)
    try:
        response = json.loads(response_text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Codex final response is not valid JSON: {error}") from error
    if (
        not isinstance(response, dict)
        or set(response) not in ({"tests"}, {"tests", "assessment"})
        or not isinstance(response.get("tests"), list)
        or ("assessment" in response and (not isinstance(response["assessment"], str) or not response["assessment"].strip()))
    ):
        raise ValueError("Codex response does not match the candidate response shape")
    if budget_config.get("require_generation_assessment") and "assessment" not in response:
        raise ValueError("Codex response is missing the required generation assessment")
    if len(response["tests"]) > int(budget["max_candidates"]):
        raise ValueError("Codex response exceeded the candidate-file budget")

    call_records = [record for record in attempts if record.get("budget_call_counted") is not False]
    all_usage_available = bool(call_records) and all(record.get("usage_available") for record in call_records)
    output_values = [record.get("output_tokens") for record in call_records if isinstance(record.get("output_tokens"), int)]
    input_values = [record.get("input_tokens") for record in call_records if isinstance(record.get("input_tokens"), int)]
    if any(value > int(budget.get("per_call_output_tokens", budget["max_output_tokens"])) for value in output_values):
        over_output_budget: bool | None = True
    elif all_usage_available:
        over_output_budget = sum(output_values) > int(budget["max_output_tokens"])
    else:
        over_output_budget = None
    total_input_tokens = sum(input_values) if all_usage_available else None
    total_output_tokens = sum(output_values) if all_usage_available else None
    wall_values = [record.get("wall_seconds") for record in call_records if isinstance(record.get("wall_seconds"), (int, float))]
    run_record = {
        "schema_version": "1.0",
        "format": "rq1-generation-run/v1",
        "task_id": package["task_id"],
        "trajectory_id": package["trajectory_id"],
        "checkpoint_id": package["checkpoint_id"],
        "method": "vanilla_llm",
        "model_config_id": budget_config["model"]["model_config_id"],
        "budget_id": budget_config["budget_id"],
        "study_budget_id": study_budget_config["budget_id"],
        "study_budget_config": input_manifest["study_budget_config"],
        "prompt_template_version": budget_config.get("prompt_template_version", "vanilla_v1"),
        "input_manifest_path": "input_manifest.json",
        "prompt_archive_path": "prompt_archive.md",
        "attempts_path": "generation_attempts.jsonl",
        "budget": {
            "max_calls": budget["max_calls"],
            "max_output_tokens": budget["max_output_tokens"],
            "max_candidates": budget["max_candidates"],
        },
        "actual_usage": {
            "calls": len(call_records),
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "token_usage_available": all_usage_available,
            "unavailable_reason": None if all_usage_available else "At least one Codex CLI attempt did not expose/capture complete token usage",
            "generation_wall_seconds": sum(wall_values) if len(wall_values) == len(call_records) else None,
            "mutation_analysis_wall_seconds": None,
        },
        "candidate_count": len(response["tests"]),
        "generation_assessment": response.get("assessment"),
        "retained_count": 0,
        "validation_log_path": "validation.jsonl",
        "codex_exit_code": result.returncode,
        "cli_attempts": len(attempts),
        "output_budget_exceeded": over_output_budget,
        "created_at_utc": ended.isoformat(),
        "temperature": budget_config["model"].get("temperature"),
        "decoding_note": budget_config["model"].get("decoding_note"),
    }
    write_text_file(output_dir / "generation_run.json", json.dumps(run_record, indent=2) + "\n")
    print(f"Generated candidate files: {len(response['tests'])}")
    print(f"Model: {budget_config['model']['model_id']} ({budget_config['model']['reasoning_effort']})")
    print(f"Attempts: {len(attempts)}")
    print(f"Token usage: {json.dumps(run_record['actual_usage'], sort_keys=True)}")
    print(f"Candidates: {response_path}")
    print(f"Run record: {output_dir / 'generation_run.json'}")
    return 2 if over_output_budget else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

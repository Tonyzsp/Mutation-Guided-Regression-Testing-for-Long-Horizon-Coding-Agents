"""Generate mutation-guided pytest candidates from one sanitized checkpoint."""

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
from pathlib import Path, PurePosixPath
from typing import Any

from run_vanilla_generation import (
    BUDGET_CONFIG,
    OUTPUT_SCHEMA,
    PACKAGE_AUDITOR,
    REPO_ROOT,
    find_usage,
    read_json,
    read_jsonl,
    render_prompt,
    prompt_resource,
    write_jsonl,
)


PROMPT_TEMPLATE = REPO_ROOT / "prompts" / "mutation_guided_v1.md"
MAX_MUTANT_RECORD_BYTES = 8_000_000
MUTANT_ID_RE = re.compile(r"[A-Za-z0-9_.:-]+")


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def safe_relative(value: str, field: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(f"unsafe {field}: {value!r}")
    return path


def load_eligible_mutants(
    records_path: Path,
    package_root: Path,
    package: dict[str, Any],
    triage_path: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any] | None]:
    allowed_results = (REPO_ROOT / "artifacts" / "results").resolve(strict=True)
    records_path = records_path.resolve(strict=True)
    if not records_path.is_relative_to(allowed_results):
        raise ValueError(f"mutant record file must be under {allowed_results}")
    if records_path.stat().st_size > MAX_MUTANT_RECORD_BYTES:
        raise ValueError("mutant record file exceeds the input limit")

    summary_path = records_path.with_name("mutation_summary.json")
    summary = read_json(summary_path)
    if summary.get("format") != "rq1-mutmut-export/v1" or summary.get("complete") is not True:
        raise ValueError("mutation summary is missing or incomplete")
    if summary.get("records_path") != records_path.name:
        raise ValueError("mutation summary points to a different mutant-record file")
    if summary.get("task_id") != package["task_id"] or summary.get("checkpoint_id") != package["checkpoint_id"]:
        raise ValueError("mutation summary does not match the source checkpoint")
    package_trajectory_root = package["trajectory_id"].split("/", 1)[0]
    if summary.get("trajectory_id") not in {package["trajectory_id"], package_trajectory_root}:
        raise ValueError("mutation summary belongs to a different trajectory")

    workspace = (package_root / package["workspace_path"]).resolve(strict=True)
    for source_relative, expected_hash in summary.get("source_sha256", {}).items():
        safe = safe_relative(source_relative, "mutant source path")
        source_file = workspace.joinpath(*safe.parts).resolve(strict=True)
        if not source_file.is_relative_to(workspace) or not source_file.is_file():
            raise ValueError(f"mutant source path is outside the checkpoint workspace: {source_relative}")
        if sha256_bytes(source_file.read_bytes()) != expected_hash:
            raise ValueError(f"mutant source hash does not match checkpoint package: {source_relative}")

    records = read_jsonl(records_path)
    if len(records) != summary.get("mutant_count"):
        raise ValueError("mutant record count does not match the complete mutation summary")
    survivors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        if record.get("task_id") != package["task_id"] or record.get("checkpoint_id") != package["checkpoint_id"]:
            raise ValueError("mutant record belongs to a different task or checkpoint")
        if record.get("trajectory_id") not in {package["trajectory_id"], package_trajectory_root}:
            raise ValueError("mutant record belongs to a different trajectory")
        if record.get("normalized_status") != "survived":
            continue
        mutant_id = record.get("mutant_id")
        if not isinstance(mutant_id, str) or MUTANT_ID_RE.fullmatch(mutant_id) is None:
            raise ValueError(f"invalid mutant ID in record: {mutant_id!r}")
        if mutant_id in seen:
            raise ValueError(f"duplicate surviving mutant ID: {mutant_id}")
        seen.add(mutant_id)
        location = record.get("location", {})
        source_relative = location.get("path")
        safe = safe_relative(source_relative, "mutant location")
        source_file = workspace.joinpath(*safe.parts).resolve(strict=True)
        if not source_file.is_relative_to(workspace) or not source_file.is_file():
            raise ValueError(f"mutant location is outside the checkpoint workspace: {source_relative!r}")
        diff = record.get("diff")
        if not isinstance(diff, str) or not diff.strip():
            raise ValueError(f"surviving mutant {mutant_id} has no diff")
        survivors.append({
            "mutant_id": mutant_id,
            "operator": record.get("operator"),
            "location": location,
            "diff": diff,
            "normalized_status": "survived",
            "target_function_reached_in_baseline": record.get("target_function_reached_in_baseline"),
            "review_status": record.get("review_status", "not_reviewed"),
        })
    if len(survivors) != summary.get("status_counts", {}).get("survived"):
        raise ValueError("eligible survivor count does not match the mutation summary")

    triage: dict[str, Any] | None = None
    if triage_path is None:
        eligible = survivors
    else:
        triage_path = triage_path.resolve(strict=True)
        if not triage_path.is_relative_to(REPO_ROOT):
            raise ValueError(f"mutant triage must be under {REPO_ROOT}")
        triage = read_json(triage_path)
        if triage.get("schema_version") != "rq1-mutant-triage/v1":
            raise ValueError("unsupported mutant-triage schema")
        if triage.get("task_id") != package["task_id"] or triage.get("checkpoint_id") != package["checkpoint_id"]:
            raise ValueError("mutant triage does not match the source checkpoint")
        package_trajectory = package["trajectory_id"]
        if triage.get("trajectory_id") not in {package_trajectory, package_trajectory.split("/", 1)[0]}:
            raise ValueError("mutant triage belongs to a different trajectory")
        if triage.get("mutation_record_set_sha256") != sha256_bytes(records_path.read_bytes()):
            raise ValueError("mutant triage was prepared for a different mutation record set")
        decisions = triage.get("decisions")
        if not isinstance(decisions, list):
            raise ValueError("mutant triage decisions must be an array")
        decision_by_id: dict[str, dict[str, Any]] = {}
        allowed_decisions = {"eligible", "deferred", "suspected_equivalent", "out_of_scope"}
        for decision in decisions:
            mutant_id = decision.get("mutant_id") if isinstance(decision, dict) else None
            status = decision.get("status") if isinstance(decision, dict) else None
            rationale = decision.get("rationale") if isinstance(decision, dict) else None
            if not isinstance(mutant_id, str) or status not in allowed_decisions:
                raise ValueError("mutant triage contains an invalid decision")
            if not isinstance(rationale, str) or not rationale.strip():
                raise ValueError(f"mutant triage has no rationale for {mutant_id}")
            selected = decision.get("selected_for_generation", status == "eligible")
            selection_rationale = decision.get("selection_rationale")
            if not isinstance(selected, bool):
                raise ValueError(f"mutant triage has an invalid selection flag for {mutant_id}")
            if selected and status != "eligible":
                raise ValueError(f"non-eligible mutant is selected for generation: {mutant_id}")
            if status == "eligible" and not selected and (
                not isinstance(selection_rationale, str) or not selection_rationale.strip()
            ):
                raise ValueError(f"unselected eligible mutant has no selection rationale: {mutant_id}")
            if mutant_id in decision_by_id:
                raise ValueError(f"duplicate mutant triage decision: {mutant_id}")
            decision_by_id[mutant_id] = decision
        survivor_ids = {item["mutant_id"] for item in survivors}
        if set(decision_by_id) != survivor_ids:
            missing = sorted(survivor_ids - set(decision_by_id))
            extra = sorted(set(decision_by_id) - survivor_ids)
            raise ValueError(f"mutant triage must classify every survivor exactly once; missing={missing}, extra={extra}")
        eligible = []
        for survivor in survivors:
            decision = decision_by_id[survivor["mutant_id"]]
            if decision["status"] == "eligible" and decision.get("selected_for_generation", True):
                eligible.append({
                    **survivor,
                    "triage_status": decision["status"],
                    "triage_rationale": decision["rationale"],
                    "gap_ids": decision.get("gap_ids", []),
                    "behavior_family_id": decision.get("behavior_family_id"),
                    "selection_rationale": decision.get("selection_rationale"),
                })
    return eligible, summary, triage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True, help="Sanitized <task>/C<n> package")
    parser.add_argument("--mutant-records", type=Path, required=True, help="Complete same-checkpoint mutant_records.jsonl")
    parser.add_argument("--mutant-triage", type=Path, help="Audited decisions for every survivor; raw records remain unchanged")
    parser.add_argument("--output-dir", type=Path, required=True, help="New result directory under artifacts/results")
    parser.add_argument("--budget-config", type=Path, default=BUDGET_CONFIG)
    parser.add_argument("--prompt-template", type=Path, default=PROMPT_TEMPLATE)
    parser.add_argument("--opportunity-brief", type=Path)
    parser.add_argument("--output-schema", type=Path)
    parser.add_argument(
        "--pairwise-minimal-guidance",
        action="store_true",
        help="Expose only one target mutant ID and diff in the controlled paired-generation prompt.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args()

    package_root = args.package.resolve(strict=True)
    output_dir = args.output_dir.resolve()
    allowed_output = (REPO_ROOT / "artifacts" / "results").resolve(strict=True)
    if not output_dir.is_relative_to(allowed_output):
        raise ValueError(f"output directory must remain under {allowed_output}")
    output_dir_preexists = output_dir.exists()
    if output_dir_preexists and any(
        (output_dir / name).exists()
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
    eligible_mutants, mutation_summary, triage = load_eligible_mutants(
        args.mutant_records, package_root, package, args.mutant_triage
    )
    if args.pairwise_minimal_guidance:
        if len(eligible_mutants) != 1:
            raise ValueError("controlled paired guidance requires exactly one eligible target mutant")
        eligible_mutants = [
            {"mutant_id": eligible_mutants[0]["mutant_id"], "diff": eligible_mutants[0]["diff"]}
        ]
    budget_config = read_json(args.budget_config)
    configured_schema = budget_config.get("output_schema")
    output_schema_path = args.output_schema or (REPO_ROOT / configured_schema if configured_schema else OUTPUT_SCHEMA)
    budget = budget_config["budget"]
    prompt_budget = dict(budget)
    prompt_budget["max_output_tokens"] = budget.get("per_call_output_tokens", budget["max_output_tokens"])
    mutant_json = json.dumps(eligible_mutants, ensure_ascii=False, indent=2)
    template_path = args.prompt_template.resolve(strict=True)
    extra_template_values = {"ELIGIBLE_MUTANTS": mutant_json}
    if args.pairwise_minimal_guidance:
        extra_template_values["TARGET_MUTATION_SECTION"] = (
            "## Target mutation information (the only condition-specific information)\n\n"
            "The single target survivor below is supplied to guide this test. Generate a test that is "
            "consistent with the specification, passes the original C1 implementation, and fails on "
            "this target mutant. Do not assert behavior unsupported by the specification.\n\n"
            + mutant_json
        )
    if args.opportunity_brief:
        brief_path = args.opportunity_brief.resolve(strict=True)
        extra_template_values["TEST_OPPORTUNITIES"] = brief_path.read_text(encoding="utf-8")
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
    input_manifest["budget_id"] = budget_config["budget_id"]
    if args.opportunity_brief:
        input_manifest["opportunity_brief"] = prompt_resource(args.opportunity_brief, "opportunity brief")
    records_path = args.mutant_records.resolve(strict=True)
    summary_path = records_path.with_name("mutation_summary.json")
    eligible_path = output_dir / "eligible_mutants.json"
    eligible_bytes = (json.dumps(eligible_mutants, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    input_manifest.update({
        "method": "mutation_guided",
        "guidance_mode": "target_mutant_id_and_diff_only" if args.pairwise_minimal_guidance else "all_eligible_mutant_records",
        "mutation_record_set_path": str(records_path),
        "mutation_record_set_sha256": sha256_bytes(records_path.read_bytes()),
        "mutation_summary_path": str(summary_path),
        "mutation_summary_sha256": sha256_bytes(summary_path.read_bytes()),
        "eligible_mutants_path": "eligible_mutants.json",
        "eligible_mutants_sha256": sha256_bytes(eligible_bytes),
        "eligible_mutant_count": len(eligible_mutants),
        "eligible_mutant_ids": [item["mutant_id"] for item in eligible_mutants],
    })
    triage_copy_name = None
    triage_copy_bytes = None
    if triage is not None and args.mutant_triage is not None:
        triage_source = args.mutant_triage.resolve(strict=True)
        triage_copy_name = "mutant_triage.json"
        triage_copy_bytes = triage_source.read_bytes()
        eligible_ids = {item["mutant_id"] for item in eligible_mutants}
        decision_by_id = {item["mutant_id"]: item for item in triage["decisions"]}
        semantic_eligible_ids = {
            mutant_id for mutant_id, item in decision_by_id.items()
            if item["status"] == "eligible"
        }
        input_manifest.update({
            "mutant_triage_path": triage_copy_name,
            "mutant_triage_sha256": sha256_bytes(triage_copy_bytes),
            "mutant_triage_source_path": str(triage_source),
            "mutant_triage_status_counts": {
                status: sum(item["status"] == status for item in triage["decisions"])
                for status in ("eligible", "deferred", "suspected_equivalent", "out_of_scope")
            },
            "semantic_eligible_mutant_count": len(semantic_eligible_ids),
            "eligible_not_selected_mutant_ids": sorted(semantic_eligible_ids - eligible_ids),
            "triaged_out_mutant_ids": sorted(
                mutant_id for mutant_id, item in decision_by_id.items()
                if item["status"] != "eligible"
            ),
        })
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = output_dir / "prompt_archive.md"
    input_manifest_path = output_dir / "input_manifest.json"
    generation_context_preexists = prompt_path.exists() or input_manifest_path.exists()
    if generation_context_preexists:
        if not prompt_path.is_file() or not input_manifest_path.is_file():
            raise ValueError("existing generation context is incomplete")
        if not prompt_path.is_file() or prompt_path.read_text(encoding="utf-8") != prompt:
            raise ValueError("existing generation directory has a different or incomplete prompt archive")
        if not input_manifest_path.is_file() or read_json(input_manifest_path) != input_manifest:
            raise ValueError("existing generation directory has a different or incomplete input manifest")
        if not eligible_path.is_file() or eligible_path.read_bytes() != eligible_bytes:
            raise ValueError("existing generation directory has a different eligible-mutant package")
        if triage_copy_name is not None:
            triage_copy_path = output_dir / triage_copy_name
            if not triage_copy_path.is_file() or triage_copy_path.read_bytes() != triage_copy_bytes:
                raise ValueError("existing generation directory has a different mutant-triage package")
    else:
        prompt_path.write_text(prompt, encoding="utf-8")
        input_manifest_path.write_text(json.dumps(input_manifest, indent=2) + "\n", encoding="utf-8")
        eligible_path.write_bytes(eligible_bytes)
        if triage_copy_name is not None and triage_copy_bytes is not None:
            (output_dir / triage_copy_name).write_bytes(triage_copy_bytes)
        (output_dir / "package_audit.txt").write_text(audit.stdout, encoding="utf-8")

    if not eligible_mutants:
        # With no eligible survivor there is no mutation signal to guide a test.
        # Preserve a complete, auditable empty Guided result without an unnecessary
        # model request; the shared base suite is still frozen by the normal validator.
        response = {
            "tests": [],
            "assessment": "No eligible surviving mutants were available, so no Guided test-generation call was made.",
        }
        (output_dir / "candidate_response.json").write_text(
            json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        attempts_path = output_dir / "generation_attempts.jsonl"
        attempts_path.write_text("", encoding="utf-8")
        created_at = datetime.now(timezone.utc).isoformat()
        run_record = {
            "schema_version": "1.0",
            "format": "rq1-generation-run/v1",
            "task_id": package["task_id"],
            "trajectory_id": package["trajectory_id"],
            "checkpoint_id": package["checkpoint_id"],
            "method": "mutation_guided",
            "model_config_id": budget_config["model"]["model_config_id"],
            "budget_id": budget_config["budget_id"],
            "prompt_template_version": budget_config.get("prompt_template_version", "mutation_guided_v1"),
            "input_manifest_path": "input_manifest.json",
            "prompt_archive_path": "prompt_archive.md",
            "attempts_path": "generation_attempts.jsonl",
            "mutation_record_set_path": str(records_path),
            "mutation_record_set_sha256": input_manifest["mutation_record_set_sha256"],
            "eligible_mutant_count": 0,
            "guided_mutant_ids": [],
            "budget": {
                "max_calls": budget["max_calls"],
                "max_output_tokens": budget["max_output_tokens"],
                "max_candidates": budget["max_candidates"],
            },
            "actual_usage": {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "token_usage_available": True,
                "unavailable_reason": None,
                "generation_wall_seconds": 0.0,
                "mutation_analysis_wall_seconds": None,
            },
            "candidate_count": 0,
            "generation_assessment": response["assessment"],
            "retained_count": 0,
            "validation_log_path": "validation.jsonl",
            "codex_exit_code": 0,
            "cli_attempts": 0,
            "output_budget_exceeded": False,
            "created_at_utc": created_at,
            "temperature": budget_config["model"].get("temperature"),
            "decoding_note": budget_config["model"].get("decoding_note"),
        }
        (output_dir / "generation_run.json").write_text(
            json.dumps(run_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print("Eligible surviving mutants: 0")
        print("Generated candidate files: 0 (no generation call was made)")
        print(f"Run record: {output_dir / 'generation_run.json'}")
        return 0

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
    attempts.append({
        "attempt": attempt_number,
        "status": "started",
        "launched_at_utc": launched_at.isoformat(),
        "input_tokens": None,
        "output_tokens": None,
        "usage_available": False,
        "wall_seconds": None,
        "model_request_sent": None,
        "budget_call_counted": False,
    })
    write_jsonl(attempts_path, attempts)
    attempt_response_path = output_dir / f"candidate_response_attempt_{attempt_number:02d}.json"
    attempt_events_path = output_dir / f"codex-events_attempt_{attempt_number:02d}.jsonl"
    attempt_stderr_path = output_dir / f"codex-stderr_attempt_{attempt_number:02d}.log"
    isolated_parent = REPO_ROOT / "tmp" / "generation-isolation"
    isolated_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="rq1-guided-isolated-", dir=isolated_parent, ignore_cleanup_errors=True
    ) as isolated_root:
        command = [
            codex, "exec", "--ephemeral", "--model", str(budget_config["model"]["model_id"]),
            "--sandbox", "read-only", "--skip-git-repo-check", "--cd", isolated_root,
            "--output-schema", str(output_schema_path.resolve(strict=True)), "--output-last-message", str(attempt_response_path),
            "--json", "-",
        ]
        reasoning_effort = budget_config["model"].get("reasoning_effort")
        if reasoning_effort:
            command[2:2] = ["-c", f'model_reasoning_effort="{reasoning_effort}"']
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
            timed_out = False
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
            attempt_stderr_path.write_text(str(error), encoding="utf-8")
            write_jsonl(attempts_path, attempts)
            raise RuntimeError(f"Could not start Codex CLI; no model request was sent: {error}") from error
        ended = datetime.now(timezone.utc)

    attempt_events_path.write_text(result.stdout, encoding="utf-8")
    attempt_stderr_path.write_text(result.stderr, encoding="utf-8")
    usage = find_usage(result.stdout)
    response_captured = attempt_response_path.is_file()
    state_init_failed = "failed to initialize in-process app-server client" in result.stderr
    # Codex can emit startup warnings on stdout even when its local state DB
    # prevents the app-server from initializing. Those bytes do not mean a
    # model request was sent and must not consume the generation-call budget.
    model_request_sent = not state_init_failed and bool(result.stdout.strip() or response_captured)
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
        "response_path": attempt_response_path.name if response_captured else None,
        "events_path": attempt_events_path.name,
        "stderr_path": attempt_stderr_path.name,
    })
    write_jsonl(attempts_path, attempts)
    response_path = output_dir / "candidate_response.json"
    if result.returncode != 0 or not response_captured:
        raise RuntimeError(
            f"Codex generation attempt {attempt_number} failed (exit={result.returncode}); see {attempt_stderr_path}"
        )
    shutil.copy2(attempt_response_path, response_path)
    response = read_json(response_path)
    if (
        not isinstance(response, dict)
        or set(response) not in ({"tests"}, {"tests", "assessment"})
        or not isinstance(response["tests"], list)
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
        output_budget_exceeded: bool | None = True
    elif all_usage_available:
        output_budget_exceeded = sum(output_values) > int(budget["max_output_tokens"])
    else:
        output_budget_exceeded = None
    wall_values = [record.get("wall_seconds") for record in call_records if isinstance(record.get("wall_seconds"), (int, float))]
    run_record = {
        "schema_version": "1.0",
        "format": "rq1-generation-run/v1",
        "task_id": package["task_id"],
        "trajectory_id": package["trajectory_id"],
        "checkpoint_id": package["checkpoint_id"],
        "method": "mutation_guided",
        "model_config_id": budget_config["model"]["model_config_id"],
        "budget_id": budget_config["budget_id"],
        "prompt_template_version": budget_config.get("prompt_template_version", "mutation_guided_v1"),
        "input_manifest_path": "input_manifest.json",
        "prompt_archive_path": "prompt_archive.md",
        "attempts_path": "generation_attempts.jsonl",
        "mutation_record_set_path": str(records_path),
        "mutation_record_set_sha256": input_manifest["mutation_record_set_sha256"],
        "eligible_mutant_count": len(eligible_mutants),
        "semantic_eligible_mutant_count": input_manifest.get(
            "semantic_eligible_mutant_count", len(eligible_mutants)
        ),
        "eligible_not_selected_mutant_ids": input_manifest.get(
            "eligible_not_selected_mutant_ids", []
        ),
        "guided_mutant_ids": [item["mutant_id"] for item in eligible_mutants],
        "budget": {
            "max_calls": budget["max_calls"],
            "max_output_tokens": budget["max_output_tokens"],
            "max_candidates": budget["max_candidates"],
        },
        "actual_usage": {
            "calls": len(call_records),
            "input_tokens": sum(input_values) if all_usage_available else None,
            "output_tokens": sum(output_values) if all_usage_available else None,
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
        "output_budget_exceeded": output_budget_exceeded,
        "created_at_utc": ended.isoformat(),
        "temperature": budget_config["model"].get("temperature"),
        "decoding_note": budget_config["model"].get("decoding_note"),
    }
    (output_dir / "generation_run.json").write_text(json.dumps(run_record, indent=2) + "\n", encoding="utf-8")
    print(f"Eligible surviving mutants: {len(eligible_mutants)}")
    print(f"Generated candidate files: {len(response['tests'])}")
    print(f"Model: {budget_config['model']['model_id']} ({budget_config['model']['reasoning_effort']})")
    print(f"Attempts: {len(attempts)}; counted calls: {len(call_records)}")
    print(f"Token usage: {json.dumps(run_record['actual_usage'], sort_keys=True)}")
    print(f"Candidates: {response_path}")
    print(f"Run record: {output_dir / 'generation_run.json'}")
    return 2 if output_budget_exceeded else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

"""Run the isolated RQ1 C1 recovery-v2 triage and paired generation batch."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from run_mutant_family_triage import validate_response as validate_triage_response

BATCH_PATH = ROOT / "config" / "rq1_recovery_v2_batch.json"
BATCH_LOCK_PATH = ROOT / "config" / "rq1_recovery_v2_batch.lock.json"
MG_AMENDMENT_PATH = ROOT / "config" / "rq1_recovery_v2_mg_only_amendment.json"
MG_AMENDMENT_LOCK = ROOT / "config" / "rq1_recovery_v2_mg_only_amendment.lock.json"
V2_STUDY_BUDGET = ROOT / "config" / "rq1_recovery_v2_study_budget.json"
V2_GENERATION_BUDGET = ROOT / "config" / "generation_budget_rq1_recovery_v2_call.json"
PACKAGE_ROOT = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
V1_ROOT = ROOT / "artifacts" / "results" / "rq1_full_36x1"
RESULT_ROOT = ROOT / "artifacts" / "results" / "rq1_recovery_v2"
TRIAGE_SCHEMA = ROOT / "schemas" / "mutant-family-triage-response.schema.json"
VANILLA_GENERATOR = ROOT / "scripts" / "run_vanilla_generation.py"
GUIDED_GENERATOR = ROOT / "scripts" / "run_mutation_guided_generation.py"
V1_TRIAGE_ROOT = V1_ROOT / "mutant_family_triage_formal_v1_sanitized_20261117"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_lock(path: Path, lock_path: Path, expected_experiment: str) -> tuple[dict[str, Any], str]:
    config = read_json(path)
    lock = read_json(lock_path)
    digest = sha256_file(path)
    if lock.get("budget_path") != path.relative_to(ROOT).as_posix() or lock.get("sha256") != digest:
        raise ValueError(f"budget lock mismatch: {path.relative_to(ROOT)}")
    if config.get("experiment_id") != expected_experiment or lock.get("experiment_id") != expected_experiment:
        raise ValueError(f"unexpected experiment ID in {path.name}")
    return config, digest


def verify_setup() -> tuple[dict[str, Any], str, dict[str, Any]]:
    batch, batch_sha = verify_lock(BATCH_PATH, BATCH_LOCK_PATH, "rq1_full_36x1_recovery_v2")
    amendment, amendment_sha = verify_lock(
        MG_AMENDMENT_PATH,
        MG_AMENDMENT_LOCK,
        "rq1_full_36x1_recovery_v2_mg_only_20261118",
    )
    if amendment.get("parent_batch_sha256") != batch_sha or amendment.get("mg_generation", {}).get("methods") != ["mutation_guided"]:
        raise ValueError("MG-only amendment does not match the parent batch or method scope")
    mg_task_ids = amendment.get("mg_generation", {}).get("task_ids", [])
    if set(mg_task_ids) != set(batch["v1_snapshot"]["guided_zero_retained_tasks"]):
        raise ValueError("MG-only amendment must target exactly the v1 zero-retained MG tasks")
    study, study_sha = verify_lock(
        V2_STUDY_BUDGET,
        V2_STUDY_BUDGET.with_name(f"{V2_STUDY_BUDGET.stem}.lock.json"),
        "rq1_full_36x1_recovery_v2",
    )
    generation_budget = read_json(V2_GENERATION_BUDGET)
    if sha256_file(V2_GENERATION_BUDGET) != batch["execution_adapter"]["per_call_generation_budget_sha256"]:
        raise ValueError("per-call generation budget differs from the recovery batch lock")
    if study_sha != batch["execution_adapter"]["per_call_study_budget_sha256"]:
        raise ValueError("per-call study budget differs from the recovery batch lock")
    generation = study["generation"]
    budget = generation_budget["budget"]
    if (
        generation_budget["model"]["model_id"] != generation["model"]
        or generation_budget["model"]["reasoning_effort"] != generation["reasoning_effort"]
        or budget["max_calls"] != generation["max_calls_per_method_task"]
        or budget["max_candidates"] != generation["max_candidates_per_method_task"]
        or budget["per_call_output_tokens"] != generation["requested_output_tokens_per_call"]
        or budget["max_output_tokens"] != generation["requested_output_tokens_per_method_task"]
    ):
        raise ValueError("per-call generation budget does not match the locked recovery study budget")
    if len(batch["recovery_cohort"]["task_ids"]) != batch["recovery_cohort"]["task_count"]:
        raise ValueError("recovery task count does not match the locked task list")
    if not PACKAGE_ROOT.is_dir() or not V1_ROOT.is_dir():
        raise FileNotFoundError("checkpoint package or v1 result root is missing")
    if batch["execution_gate"]["model_calls_started"] != 0:
        raise ValueError("master batch says model calls have already started; refusing a new run")
    batch["_mg_only_amendment_sha256"] = amendment_sha
    batch["_mg_only_task_ids"] = mg_task_ids
    return batch, batch_sha, study


def v1_method_dir(task_id: str, method: str) -> Path:
    suffix = (
        "vanilla_formal_v1_20261117"
        if method == "vanilla_llm"
        else "mutation_guided_formal_v1_baseline_linked_20261117"
    )
    return V1_ROOT / task_id / "C1" / suffix


def existing_v2_triage(task_id: str, batch_sha: str, study_sha: str) -> dict[str, Any] | None:
    path = RESULT_ROOT / "mutant_triage" / task_id / "mutant_triage.json"
    if not path.is_file():
        return None
    triage = read_json(path)
    stamp = triage.get("recovery_v2", {})
    if stamp.get("batch_sha256") != batch_sha or triage.get("study_budget_sha256") != study_sha:
        raise ValueError(f"{task_id}: existing v2 triage has a different locked input; refusing overwrite")
    source_sidecar = V1_TRIAGE_ROOT / task_id / "family_selection.json"
    sidecar = path.parent / "family_selection.json"
    if not source_sidecar.is_file():
        raise FileNotFoundError(f"{task_id}: v1 family-selection sidecar is missing")
    if sidecar.exists() and sha256_file(sidecar) != sha256_file(source_sidecar):
        raise ValueError(f"{task_id}: v2 family-selection sidecar differs from v1 frozen input")
    if not sidecar.exists():
        shutil.copy2(source_sidecar, sidecar)
    if stamp.get("source") == "fresh_v2_triage_of_v1_frozen_family_representatives":
        attempts = stamp.get("attempts", [])
        if len(attempts) == 1 and _technical_triage_failure(task_id, attempts[0]):
            return None
    return triage


def _technical_triage_failure(task_id: str, attempt: dict[str, Any]) -> bool:
    task_out = RESULT_ROOT / "mutant_triage" / task_id
    response = task_out / f"triage_response_attempt_{int(attempt['attempt']):02d}.json"
    stderr = task_out / str(attempt.get("stderr_path", ""))
    if response.is_file() and response.stat().st_size > 0:
        return False
    error_text = stderr.read_text(encoding="utf-8", errors="replace") if stderr.is_file() else ""
    return int(attempt.get("exit_code", 0)) != 0 and (
        not response.exists() or "failed to initialize in-process app-server client" in error_text
    )


def make_v2_triage(task_id: str, batch: dict[str, Any], batch_sha: str, study_sha: str) -> dict[str, Any]:
    existing = existing_v2_triage(task_id, batch_sha, study_sha)
    if existing is not None:
        return existing
    source_dir = V1_TRIAGE_ROOT / task_id
    source_path = source_dir / "mutant_triage.json"
    selected_path = source_dir / "selected_representatives.json"
    if not source_path.is_file() or not selected_path.is_file():
        raise FileNotFoundError(f"{task_id}: v1 triage source is missing")
    triage = read_json(source_path)
    triage["study_budget_sha256"] = study_sha
    triage["recovery_v2"] = {
        "batch_id": batch["experiment_id"],
        "batch_sha256": batch_sha,
        "source": "inherited_v1_triage",
        "parent_triage_path": str(source_path.relative_to(ROOT).as_posix()),
        "parent_triage_sha256": sha256_file(source_path),
        "new_triage_call_count": 0,
    }
    if task_id in batch["mutant_triage_recovery"]["tasks"]:
        triage = rerun_triage(task_id, triage, source_dir, batch, batch_sha, study_sha)
    out = RESULT_ROOT / "mutant_triage" / task_id / "mutant_triage.json"
    write_json(out, triage)
    return triage


def rerun_triage(
    task_id: str,
    inherited: dict[str, Any],
    source_dir: Path,
    batch: dict[str, Any],
    batch_sha: str,
    study_sha: str,
) -> dict[str, Any]:
    selected = read_json(source_dir / "selected_representatives.json")["selected_representatives"]
    expected_ids = [row["mutant_id"] for row in selected]
    prompt_path = source_dir / "triage_prompt_inline_v2.md"
    input_manifest_path = source_dir / "triage_input_manifest_inline_v2.json"
    if not expected_ids or not prompt_path.is_file() or not input_manifest_path.is_file():
        raise ValueError(f"{task_id}: frozen v1 triage selection/prompt is incomplete")
    original_input = read_json(input_manifest_path)
    if original_input.get("task_id") != task_id or original_input.get("selected_representatives_sha256") != sha256_file(source_dir / "selected_representatives.json"):
        raise ValueError(f"{task_id}: v1 triage prompt is not linked to the frozen selected representatives")

    task_out = RESULT_ROOT / "mutant_triage" / task_id
    task_out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_dir / "selected_representatives.json", task_out / "selected_representatives_v1_source.json")
    shutil.copy2(source_dir / "family_selection.json", task_out / "family_selection.json")
    shutil.copy2(prompt_path, task_out / "triage_prompt_inline_v1_source.md")
    shutil.copy2(input_manifest_path, task_out / "triage_input_manifest_v1_source.json")
    schema_path = TRIAGE_SCHEMA.resolve(strict=True)
    codex = shutil.which("codex")
    if codex is None:
        raise RuntimeError("Codex CLI not found; no triage request was launched")

    prior_path = task_out / "mutant_triage.json"
    prior_triage = read_json(prior_path) if prior_path.is_file() else None
    prior_attempts = (
        prior_triage.get("recovery_v2", {}).get("attempts", [])
        if isinstance(prior_triage, dict) else []
    )
    attempts: list[dict[str, Any]] = list(prior_attempts)
    response: dict[str, Any] | None = None
    for attempt in range(1, 3):
        response_path = task_out / f"triage_response_attempt_{attempt:02d}.json"
        events_path = task_out / f"codex_events_attempt_{attempt:02d}.jsonl"
        stderr_path = task_out / f"codex_stderr_attempt_{attempt:02d}.log"
        previous = next((row for row in attempts if row.get("attempt") == attempt), None)
        if previous is not None:
            if response_path.is_file():
                candidate = validate_triage_response(response_path, expected_ids)
                if candidate is not None:
                    response = candidate
                    break
                if response_path.stat().st_size > 0 or attempt == 2:
                    break
            elif not _technical_triage_failure(task_id, previous):
                break
            continue
        if response_path.exists() or events_path.exists() or stderr_path.exists():
            raise ValueError(f"{task_id}: unrecorded triage attempt slot {attempt} already has artifacts; refusing repeat")
        with tempfile.TemporaryDirectory(prefix=f"rq1-v2-triage-{task_id}-", dir=ROOT / "tmp") as isolated:
            command = [
                codex, "exec", "--ephemeral", "--model", batch["generation"]["model"],
                "--sandbox", "read-only", "--skip-git-repo-check", "--cd", isolated,
                "--output-schema", str(schema_path), "--output-last-message", str(response_path), "--json", "-",
            ]
            command[2:2] = ["-c", f'model_reasoning_effort="{batch["generation"]["reasoning_effort"]}"']
            started = datetime.now(timezone.utc).isoformat()
            try:
                result = subprocess.run(
                    command,
                    input=prompt_path.read_text(encoding="utf-8"),
                    cwd=isolated,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=900,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                stdout = error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
                stderr = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
                result = subprocess.CompletedProcess(command, 124, stdout, stderr + "\nTimed out after 900 seconds.\n")
            events_path.write_text(result.stdout or "", encoding="utf-8")
            stderr_path.write_text(result.stderr or "", encoding="utf-8")
        candidate = None
        if response_path.is_file():
            candidate = validate_triage_response(response_path, expected_ids)
        attempts.append({
            "attempt": attempt,
            "started_at_utc": started,
            "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            "exit_code": result.returncode,
            "response_exists": response_path.is_file(),
            "response_bytes": response_path.stat().st_size if response_path.is_file() else 0,
            "response_sha256": sha256_file(response_path) if response_path.is_file() else None,
            "response_valid": candidate is not None,
            "events_path": events_path.name,
            "stderr_path": stderr_path.name,
        })
        if candidate is not None:
            response = candidate
            break
        # Only a failed process with no captured response permits the one technical retry.
        if (response_path.is_file() and response_path.stat().st_size > 0) or result.returncode == 0:
            break

    if response is not None:
        response_by_id = {row["mutant_id"]: row for row in response["decisions"]}
        for decision in inherited["decisions"]:
            fresh = response_by_id.get(decision.get("mutant_id"))
            if fresh is None:
                continue
            status = fresh["status"]
            decision.update({
                "status": status,
                "rationale": fresh["rationale"].strip(),
                "specification_reference": fresh.get("specification_reference"),
                "observable_behavior": fresh.get("observable_behavior"),
                "trigger": fresh.get("trigger"),
                "expected_result": fresh.get("expected_result"),
                "selected_for_generation": status == "eligible",
                "selection_rationale": "Fresh v2 C1-only triage of the v1-frozen selected family representative." if status == "eligible" else decision.get("selection_rationale"),
            })
        triage_status = "response_valid"
    else:
        triage_status = "response_invalid_or_unavailable"

    selected_ids = [row["mutant_id"] for row in inherited["decisions"] if row.get("selected_for_generation")]
    counts: dict[str, int] = {}
    for row in inherited["decisions"]:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    inherited["selected_for_generation_ids"] = selected_ids
    inherited["selected_for_generation_count"] = len(selected_ids)
    inherited["decision_counts"] = counts
    inherited["study_budget_sha256"] = study_sha
    inherited["recovery_v2"] = {
        "batch_id": batch["experiment_id"],
        "batch_sha256": batch_sha,
        "source": "fresh_v2_triage_of_v1_frozen_family_representatives",
        "parent_triage_path": str((source_dir / "mutant_triage.json").relative_to(ROOT).as_posix()),
        "parent_triage_sha256": sha256_file(source_dir / "mutant_triage.json"),
        "selected_representatives_sha256": sha256_file(source_dir / "selected_representatives.json"),
        "prompt_sha256": sha256_file(prompt_path),
        "input_manifest_sha256": sha256_file(input_manifest_path),
        "new_triage_call_count": len(attempts),
        "new_triage_status": triage_status,
        "attempts": attempts,
    }
    llm = inherited.setdefault("llm_triage", {})
    llm.update({
        "status": triage_status,
        "calls": len(attempts),
        "attempts": attempts,
        "response_sha256": sha256_file(task_out / f"triage_response_attempt_{len(attempts):02d}.json") if response is not None else None,
    })
    if response is not None:
        write_json(task_out / "triage_response.json", response)
    return inherited


def call_generator(task_id: str, method: str, slot: int, triage: dict[str, Any] | None, batch_sha: str, batch: dict[str, Any]) -> dict[str, Any]:
    slot_name = f"call_{slot:02d}"
    logical_call_dir = RESULT_ROOT / task_id / "C1" / method / slot_name
    call_dir = logical_call_dir
    if logical_call_dir.exists():
        completed_runs = list(logical_call_dir.rglob("generation_run.json"))
        for run_path in completed_runs:
            manifest_path = run_path.parent / "input_manifest.json"
            if not manifest_path.is_file():
                continue
            run = read_json(run_path)
            manifest = read_json(manifest_path)
            v2_budget_matches = (
                manifest.get("study_budget_sha256") == sha256_file(V2_STUDY_BUDGET)
                and manifest.get("budget_id") == read_json(V2_GENERATION_BUDGET).get("budget_id")
            )
            if manifest.get("recovery_v2", {}).get("batch_sha256") == batch_sha or v2_budget_matches:
                manifest["recovery_v2"] = {
                    "batch_id": "rq1_full_36x1_recovery_v2",
                    "batch_sha256": batch_sha,
                    "task_id": task_id,
                    "method": method,
                    "call_slot": slot_name,
                    "independent_call_slot": True,
                }
                manifest["recovery_v2_scope_amendment"] = {
                    "amendment_id": "rq1_full_36x1_recovery_v2_mg_only_20261118",
                    "sha256": batch.get("_mg_only_amendment_sha256"),
                    "method_scope": "mutation_guided_only",
                }
                write_json(manifest_path, manifest)
                return {"task_id": task_id, "method": method, "slot": slot, "status": "already_complete", "candidate_count": run.get("candidate_count", 0), "calls": run.get("actual_usage", {}).get("calls", 0)}
        error_path = logical_call_dir / "coordinator.stderr.log"
        known_pre_model_failure = error_path.is_file() and "hash-linked family-selection sidecar is missing" in error_path.read_text(encoding="utf-8", errors="replace")
        if not known_pre_model_failure:
            return {"task_id": task_id, "method": method, "slot": slot, "status": "existing_incomplete_not_retried", "candidate_count": 0, "calls": 0}
        call_dir = logical_call_dir / "model_invocation_after_preflight_fix"
        if call_dir.exists():
            return {"task_id": task_id, "method": method, "slot": slot, "status": "existing_retry_slot_not_retried", "candidate_count": 0, "calls": 0}

    package = PACKAGE_ROOT / task_id / "C1"
    if not package.is_dir():
        raise FileNotFoundError(package)
    command = [
        sys.executable, str(VANILLA_GENERATOR if method == "vanilla_llm" else GUIDED_GENERATOR),
        "--package", str(package),
        "--output-dir", str(call_dir),
        "--budget-config", str(V2_GENERATION_BUDGET),
        "--study-budget-config", str(V2_STUDY_BUDGET),
        "--prompt-template", str(ROOT / ("prompts/vanilla_gap_v4.md" if method == "vanilla_llm" else "prompts/mutation_guided_gap_cohort_v1.md")),
        "--opportunity-brief", str(V1_ROOT / task_id / "C1" / "formal_v1_inputs_20261117" / "gap_opportunities.md"),
        "--timeout-seconds", "900",
    ]
    if method == "mutation_guided":
        v1_run = read_json(v1_method_dir(task_id, method) / "generation_run.json")
        mutant_records = Path(v1_run["mutation_record_set_path"])
        if not mutant_records.is_absolute():
            mutant_records = ROOT / mutant_records
        if triage is None or triage.get("selected_for_generation_count", 0) == 0:
            return {"task_id": task_id, "method": method, "slot": slot, "status": "not_eligible_no_hint", "candidate_count": 0, "calls": 0}
        triage_path = RESULT_ROOT / "mutant_triage" / task_id / "mutant_triage.json"
        command += ["--mutant-records", str(mutant_records), "--mutant-triage", str(triage_path)]

    started = datetime.now(timezone.utc).isoformat()
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    call_dir.mkdir(parents=True, exist_ok=True)
    (call_dir / "coordinator.stdout.log").write_text(result.stdout, encoding="utf-8")
    (call_dir / "coordinator.stderr.log").write_text(result.stderr, encoding="utf-8")
    run_path = call_dir / "generation_run.json"
    response_path = call_dir / "candidate_response.json"
    status = "completed" if result.returncode == 0 and run_path.is_file() else "failed"
    count = 0
    calls = 0
    if run_path.is_file():
        run = read_json(run_path)
        count = int(run.get("candidate_count", 0))
        calls = int(run.get("actual_usage", {}).get("calls", 0))
        manifest_path = call_dir / "input_manifest.json"
        if manifest_path.is_file():
            manifest = read_json(manifest_path)
            manifest["recovery_v2"] = {
                "batch_id": "rq1_full_36x1_recovery_v2",
                "batch_sha256": batch_sha,
                "task_id": task_id,
                "method": method,
                "call_slot": slot_name,
                "independent_call_slot": True,
            }
            manifest["recovery_v2_scope_amendment"] = {
                "amendment_id": "rq1_full_36x1_recovery_v2_mg_only_20261118",
                "sha256": batch.get("_mg_only_amendment_sha256"),
                "method_scope": "mutation_guided_only",
            }
            write_json(manifest_path, manifest)
    record = {
        "task_id": task_id,
        "method": method,
        "call_slot": slot_name,
        "status": status,
        "exit_code": result.returncode,
        "candidate_count": count,
        "model_calls": calls,
        "started_at_utc": started,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "call_directory": str(call_dir.relative_to(ROOT).as_posix()),
        "stdout_tail": result.stdout[-1500:],
        "stderr_tail": result.stderr[-1500:],
    }
    record_path = logical_call_dir / (
        "recovery_v2_call_record.json"
        if call_dir == logical_call_dir
        else "recovery_v2_call_record_after_preflight_fix.json"
    )
    write_json(record_path, record)
    print(f"[{method} {task_id} {slot_name}] {status}; candidates={count}; model_calls={calls}", flush=True)
    return record


def preflight() -> tuple[dict[str, Any], str, dict[str, Any]]:
    batch, batch_sha, study = verify_setup()
    tasks = batch["_mg_only_task_ids"]
    vanilla_zero = set(batch["v1_snapshot"]["vanilla_zero_retained_tasks"])
    guided_zero = set(batch["v1_snapshot"]["guided_zero_retained_tasks"])
    if set(batch["recovery_cohort"]["task_ids"]) != vanilla_zero | guided_zero:
        raise ValueError("parent recovery cohort is not the exact union of v1 zero-retained task sets")
    for task in tasks:
        generation = v1_method_dir(task, "mutation_guided")
        if not (generation / "generation_run.json").is_file() or not (generation / "frozen_test_suite.json").is_file():
            raise FileNotFoundError(f"v1 MG source artifacts missing for {task}")
        if not (PACKAGE_ROOT / task / "C1" / "package.json").is_file():
            raise FileNotFoundError(f"C1 package missing for {task}")
    if RESULT_ROOT.exists():
        for task in tasks:
            triage_path = RESULT_ROOT / "mutant_triage" / task / "mutant_triage.json"
            if not triage_path.is_file():
                raise FileExistsError(f"v2 recovery output exists but is incomplete for {task}: {triage_path}")
            triage = read_json(triage_path)
            if triage.get("recovery_v2", {}).get("batch_sha256") != batch_sha:
                raise ValueError(f"{task}: existing v2 triage belongs to a different batch")
            task_root = RESULT_ROOT / task / "C1"
            if task_root.exists():
                for log in task_root.rglob("input_manifest.json"):
                    manifest = read_json(log)
                    recovery_stamp = manifest.get("recovery_v2", {})
                    is_v2_budgeted_input = (
                        manifest.get("study_budget_sha256") == sha256_file(V2_STUDY_BUDGET)
                        and manifest.get("budget_id") == read_json(V2_GENERATION_BUDGET).get("budget_id")
                    )
                    if recovery_stamp.get("batch_sha256") != batch_sha and not is_v2_budgeted_input:
                        raise ValueError(f"existing v2 generation input has a different batch hash: {log}")
    return batch, batch_sha, study


def run_triage(batch: dict[str, Any], batch_sha: str, study: dict[str, Any], study_sha: str) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for index, task in enumerate(batch["recovery_cohort"]["task_ids"], 1):
        triage = make_v2_triage(task, batch, batch_sha, study_sha)
        output[task] = triage
        print(f"[triage {index}/{len(batch['recovery_cohort']['task_ids'])}] {task}: eligible={triage.get('selected_for_generation_count', 0)}", flush=True)
    return output


def run_generation(batch: dict[str, Any], batch_sha: str, triage_by_task: dict[str, Any], workers: int) -> list[dict[str, Any]]:
    units = [
        (task, "mutation_guided", slot)
        for task in batch["_mg_only_task_ids"]
        for slot in (1, 2)
    ]
    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(call_generator, task, method, slot, triage_by_task.get(task), batch_sha, batch): (task, method, slot)
            for task, method, slot in units
        }
        for future in as_completed(futures):
            try:
                records.append(future.result())
            except Exception as error:
                task, method, slot = futures[future]
                record = {
                    "task_id": task,
                    "method": method,
                    "call_slot": f"call_{slot:02d}",
                    "status": "coordinator_error",
                    "error": f"{type(error).__name__}: {error}",
                    "finished_at_utc": datetime.now(timezone.utc).isoformat(),
                }
                records.append(record)
                print(f"[{method} {task} call_{slot:02d}] ERROR {record['error']}", file=sys.stderr, flush=True)
    def slot_number(row: dict[str, Any]) -> int:
        if isinstance(row.get("slot"), int):
            return row["slot"]
        name = str(row.get("call_slot", "call_99"))
        try:
            return int(name.rsplit("_", 1)[-1])
        except ValueError:
            return 99

    records.sort(key=lambda row: (row["task_id"], row["method"], slot_number(row)))
    write_json(RESULT_ROOT / "generation_call_summary.json", {
        "format": "rq1-recovery-v2-generation-summary/v1",
        "batch_id": batch["experiment_id"],
        "batch_sha256": batch_sha,
        "scope_amendment_sha256": batch.get("_mg_only_amendment_sha256"),
        "methods": ["mutation_guided"],
        "task_count": len(batch["_mg_only_task_ids"]),
        "method_call_slot_count": len(units),
        "status_counts": {},
        "records": records,
    })
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("preflight", "triage", "generation", "all"), default="preflight")
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.workers <= 4:
        raise ValueError("workers must be between 1 and 4")
    batch, batch_sha, study = preflight()
    study_sha = sha256_file(V2_STUDY_BUDGET)
    print(json.dumps({
        "batch_id": batch["experiment_id"],
        "batch_sha256": batch_sha,
        "tasks": len(batch["recovery_cohort"]["task_ids"]),
        "triage_tasks": len(batch["mutant_triage_recovery"]["tasks"]),
        "result_root": str(RESULT_ROOT),
        "phase": args.phase,
        "model_calls_started": 0,
        "scope_amendment": "rq1_full_36x1_recovery_v2_mg_only_20261118",
    }, ensure_ascii=False), flush=True)
    if args.phase == "preflight":
        return 0
    RESULT_ROOT.mkdir(parents=True, exist_ok=True)
    if args.phase in {"triage", "all"}:
        triage = run_triage(batch, batch_sha, study, study_sha)
    else:
        triage = {}
        for task in batch["_mg_only_task_ids"]:
            triage[task] = existing_v2_triage(task, batch_sha, study_sha)
            if triage[task] is None:
                raise ValueError(f"{task}: v2 triage is missing; run --phase triage first")
    if args.phase in {"generation", "all"}:
        run_generation(batch, batch_sha, triage, args.workers)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError, RuntimeError) as error:
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(2) from error

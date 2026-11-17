"""Reclassify mutmut mutants by applying each mutant before running C1 tests.

mutmut's in-memory trampoline can miss changes when a test invokes a CLI in a
child process. This runner keeps the original mutmut result files, applies each
cached mutant to a disposable checkpoint runtime, and executes only the C1
base-test IDs that passed consistently in the repeated baseline audit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import shlex
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from c1_test_selection import stable_pass_selection

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.export_mutant_records import parse_statuses
from scripts.validate_and_freeze_tests import (
    filesystem_path,
    run_in_docker,
    write_text_file,
)


PYTHON_BIN = "/opt/mutmut-env/bin/python"
MUTMUT_BIN = "/opt/mutmut-env/bin/mutmut"
DIRECT_CLASSIFIER_VERSION = "4"
MUTATION_PROTOCOL_VERSION = "rq1-mutmut-v4"
TERMINAL_DIRECT_STATUSES = {"killed", "survived", "not_tested"}
FINAL_DIRECT_STATUSES = TERMINAL_DIRECT_STATUSES | {"error", "timeout"}
MUTATION_BRIDGES = {
    "mutmut_subprocess_bridge.py": REPO_ROOT / "scripts" / "mutmut_subprocess_bridge.py",
    "sitecustomize.py": REPO_ROOT / "scripts" / "mutmut_subprocess_sitecustomize.py",
}


def read_json(path: Path) -> dict[str, Any]:
    with open(filesystem_path(path), encoding="utf-8") as handle:
        return json.load(handle)


def read_text(path: Path, *, encoding: str = "utf-8") -> str:
    with open(filesystem_path(path), encoding=encoding) as handle:
        return handle.read()


def path_is_file(path: Path) -> bool:
    return os.path.isfile(filesystem_path(path))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(filesystem_path(path), "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_with_process_group(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a command and kill its whole child process group on timeout.

    Benchmark tests can start CLI subprocesses that inherit pytest's output
    pipes. Killing only pytest may leave those children alive and keep
    ``communicate()`` blocked, so execute pytest in its own POSIX session.
    """
    if os.name == "nt":
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            command,
            timeout,
            output=stdout,
            stderr=stderr,
        ) from error
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def json_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def worker_config_hash_matches(worker_config: dict[str, Any]) -> bool:
    """Accept canonical shard hashes and the legacy parent-hash encoding."""
    recorded_hash = worker_config.get("classification_config_sha256")
    canonical_signature = {
        key: value
        for key, value in worker_config.items()
        if key not in {"resume", "classification_config_sha256"}
    }
    valid_hashes = {json_sha256(canonical_signature)}
    parent_hash = worker_config.get("parent_classification_config_sha256")
    if isinstance(parent_hash, str):
        legacy_signature = {
            key: value for key, value in worker_config.items() if key != "resume"
        }
        legacy_signature["classification_config_sha256"] = parent_hash
        valid_hashes.add(json_sha256(legacy_signature))
    return isinstance(recorded_hash, str) and recorded_hash in valid_hashes


def retry_inconclusive_records(runtime_dir: Path, results_path: Path) -> int:
    """Keep failed attempts auditable while making only conclusive rows resumable."""
    if not path_is_file(results_path):
        return 0
    records = [
        json.loads(line)
        for line in read_text(results_path).splitlines()
        if line.strip()
    ]
    ids = [record.get("mutant_id") for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError("direct-classification results contain duplicate mutant IDs")
    conclusive = [
        record for record in records
        if record.get("direct_status") in TERMINAL_DIRECT_STATUSES
    ]
    inconclusive = [
        record for record in records
        if record.get("direct_status") not in TERMINAL_DIRECT_STATUSES
    ]
    if not inconclusive:
        return 0

    attempts_path = runtime_dir / "direct_mutant_classification_attempts.jsonl"
    attempt_counts: dict[str, int] = {}
    if path_is_file(attempts_path):
        for line in read_text(attempts_path).splitlines():
            if not line.strip():
                continue
            prior = json.loads(line)
            mutant_id = prior.get("mutant_id")
            if isinstance(mutant_id, str):
                attempt_counts[mutant_id] = max(attempt_counts.get(mutant_id, 0), int(prior.get("attempt_number", 0)))

    with open(filesystem_path(attempts_path), "a", encoding="utf-8") as handle:
        for record in inconclusive:
            mutant_id = str(record.get("mutant_id", ""))
            attempt_number = attempt_counts.get(mutant_id, 0) + 1
            attempt_counts[mutant_id] = attempt_number
            handle.write(json.dumps({
                "format": "rq1-direct-mutant-attempt/v1",
                "archived_at_utc": datetime.now(timezone.utc).isoformat(),
                "attempt_number": attempt_number,
                **record,
            }, ensure_ascii=False, separators=(",", ":")) + "\n")
    write_text_file(
        results_path,
        "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in conclusive),
    )
    return len(inconclusive)


def recover_parallel_worker_results(
    runtime_dir: Path,
    config: dict[str, Any],
    results_path: Path,
) -> int:
    """Recover conclusive rows from each valid shard, even if another shard failed."""
    expected = {item["mutant_id"]: item["mutmut_status"] for item in config["mutants"]}
    existing = [
        json.loads(line)
        for line in read_text(results_path).splitlines()
        if line.strip()
    ] if path_is_file(results_path) else []
    terminal_ids = {
        record["mutant_id"]
        for record in existing
        if record.get("direct_status") in TERMINAL_DIRECT_STATUSES
    }
    pending_ids = set(expected) - terminal_ids
    if not pending_ids:
        return 0

    candidate_roots = sorted(
        runtime_dir.parent.glob(f"{runtime_dir.name}-direct-workers-*"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    print(
        f"Direct shard recovery scan: pending={len(pending_ids)} roots={len(candidate_roots)}",
        flush=True,
    )
    for candidate_root in candidate_roots:
        worker_dirs = sorted(candidate_root.glob("worker-*"))
        if not worker_dirs:
            continue
        candidate_records: dict[str, dict[str, Any]] = {}
        worker_attempt_records: list[tuple[str, str, dict[str, Any]]] = []
        claimed_ids: set[str] = set()
        conflicting_shards = False
        for worker_dir in worker_dirs:
            try:
                worker_config_path = worker_dir / "direct_mutant_classification_config.json"
                worker_config = read_json(worker_config_path) if path_is_file(worker_config_path) else None
                worker_index = worker_dir.name.rsplit("-", 1)[-1]
                worker_results_name = (
                    str(worker_config["results_path"])
                    if worker_config is not None
                    else f"direct-worker-{worker_index}.jsonl"
                )
                worker_summary_name = (
                    str(worker_config["summary_path"])
                    if worker_config is not None
                    else f"direct-worker-{worker_index}-summary.json"
                )
                worker_results_path = worker_dir / worker_results_name
                worker_summary = read_json(worker_dir / worker_summary_name)
                rows = [
                    json.loads(line)
                    for line in read_text(worker_results_path).splitlines()
                    if line.strip()
                ]
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                # A damaged or absent shard must not invalidate its siblings.
                print(f"Direct shard recovery skipped unreadable shard: {worker_dir}", flush=True)
                continue

            if worker_config is not None:
                assigned = {
                    item["mutant_id"]: item["mutmut_status"]
                    for item in worker_config.get("mutants", [])
                }
                worker_signature = {
                    key: value
                    for key, value in worker_config.items()
                    if key not in {"resume", "classification_config_sha256"}
                }
                config_mismatches = [
                    key for key in (
                        "task_id", "checkpoint_id", "classification_algorithm_version",
                        "mutation_protocol_version", "source_paths", "source_sha256",
                        "stable_test_ids", "mutant_test_ids", "baseline_audit_sha256",
                    )
                    if worker_config.get(key) != config.get(key)
                ]
                if not worker_config_hash_matches(worker_config):
                    config_mismatches.append("classification_config_sha256")
                if worker_summary.get("classification_config_sha256") != worker_config.get("classification_config_sha256"):
                    config_mismatches.append("summary_classification_config_sha256")
                config_matches = not config_mismatches
            else:
                # Earlier worker shards stored their partition results and summary
                # but did not copy the worker config into the shard directory.
                # Reconstruct the assigned IDs from the rows, then validate every
                # row against the current parent config below.
                assigned = {
                    record.get("mutant_id"): record.get("mutmut_status")
                    for record in rows
                    if isinstance(record.get("mutant_id"), str)
                }
                config_matches = (
                    worker_summary.get("format") == "rq1-direct-mutant-classification/v1"
                    and worker_summary.get("classification_algorithm_version")
                    == config.get("classification_algorithm_version")
                )
                config_mismatches = [] if config_matches else ["legacy_summary_format_or_algorithm"]
            row_ids = [record.get("mutant_id") for record in rows]
            observed_counts: dict[str, int] = {}
            for record in rows:
                status = record.get("direct_status")
                observed_counts[status] = observed_counts.get(status, 0) + 1
            if (
                not config_matches
                or not assigned
                or any(mutant_id not in expected or expected[mutant_id] != status for mutant_id, status in assigned.items())
                or len(row_ids) != len(set(row_ids))
                or set(row_ids) != set(assigned)
                or claimed_ids & set(assigned)
                or worker_summary.get("classified_count") != len(rows)
                or worker_summary.get("mutant_count") != len(assigned)
                or worker_summary.get("task_id") != config.get("task_id")
                or worker_summary.get("checkpoint_id") != config.get("checkpoint_id")
                or worker_summary.get("baseline_audit_sha256") != config.get("baseline_audit_sha256")
                or worker_summary.get("source_sha256") != config.get("source_sha256")
                or worker_summary.get("status_counts") != observed_counts
                or worker_summary.get("complete") not in {
                    all(record.get("direct_status") in TERMINAL_DIRECT_STATUSES for record in rows),
                    all(record.get("direct_status") in FINAL_DIRECT_STATUSES for record in rows),
                }
            ):
                conflicting_shards = conflicting_shards or bool(claimed_ids & set(assigned))
                print(
                    f"Direct shard recovery skipped metadata-invalid shard: {worker_dir.name} "
                    f"config_present={worker_config is not None} config={config_matches} "
                    f"mismatches={config_mismatches} rows={len(rows)} assigned={len(assigned)} "
                    f"summary_counts_match={worker_summary.get('status_counts') == observed_counts}",
                    flush=True,
                )
                continue

            for source_name, expected_hash in config["source_sha256"].items():
                source_path = worker_dir / Path(*source_name.split("/"))
                if not path_is_file(source_path) or sha256_file(source_path) != expected_hash:
                    config_matches = False
                    break
            if not config_matches:
                print(f"Direct shard recovery skipped config/source mismatch: {worker_dir}", flush=True)
                continue

            shard_valid = True
            shard_records: dict[str, dict[str, Any]] = {}
            for record in rows:
                mutant_id = record["mutant_id"]
                matched_tests = config["mutant_test_ids"].get(mutant_id, [])
                no_test_evidence = not matched_tests and expected[mutant_id] == "no tests"
                selected_test_ids = [] if no_test_evidence else matched_tests or config["stable_test_ids"]
                selection_method = (
                    "mutmut_no_tests_without_subprocess_hit" if no_test_evidence
                    else "subprocess_function_hits" if matched_tests
                    else "full_stable_suite_fallback"
                )
                source_changed = record.get("source_changed")
                status = record.get("direct_status")
                if (
                    record.get("mutmut_status") != expected[mutant_id]
                    or record.get("selected_test_ids") != selected_test_ids
                    or record.get("test_selection_method") != selection_method
                    or status not in TERMINAL_DIRECT_STATUSES | {"error", "timeout"}
                    or not isinstance(source_changed, dict)
                    or set(source_changed) != set(config["source_paths"])
                    or (status in TERMINAL_DIRECT_STATUSES and record.get("error") is not None)
                    or (status in {"killed", "survived"} and (not all(source_changed.values()) or record.get("apply_exit_code") != 0))
                    or (no_test_evidence and status != "not_tested")
                    or (status == "not_tested" and not no_test_evidence)
                ):
                    shard_valid = False
                    break
                if status in TERMINAL_DIRECT_STATUSES:
                    shard_records[mutant_id] = record
                else:
                    worker_attempt_records.append((candidate_root.name, worker_dir.name, record))
            if not shard_valid:
                print(f"Direct shard recovery skipped invalid rows: {worker_dir}", flush=True)
                continue
            claimed_ids.update(assigned)
            candidate_records.update(shard_records)
            print(
                f"Direct shard recovery accepted {worker_dir.name}: "
                f"rows={len(rows)} terminal={len(shard_records)}",
                flush=True,
            )

        if conflicting_shards:
            continue
        if worker_attempt_records:
            attempts_path = runtime_dir / "direct_mutant_classification_attempts.jsonl"
            previous_attempts = (
                [
                    json.loads(line)
                    for line in read_text(attempts_path).splitlines()
                    if line.strip()
                ]
                if path_is_file(attempts_path)
                else []
            )
            attempt_counts: dict[str, int] = {}
            archived_shard_keys: set[tuple[str, str, str, str]] = set()
            for attempt in previous_attempts:
                mutant_id = attempt.get("mutant_id")
                if isinstance(mutant_id, str):
                    attempt_counts[mutant_id] = max(
                        attempt_counts.get(mutant_id, 0),
                        int(attempt.get("attempt_number", 0)),
                    )
                origin = attempt.get("recovered_from_worker_shard")
                if isinstance(origin, str):
                    archived_shard_keys.add((
                        origin,
                        str(mutant_id),
                        str(attempt.get("direct_status")),
                        str(attempt.get("error")),
                    ))
            with open(filesystem_path(attempts_path), "a", encoding="utf-8") as handle:
                for worker_run, worker_name, record in worker_attempt_records:
                    mutant_id = str(record.get("mutant_id", ""))
                    origin = f"{worker_run}/{worker_name}"
                    key = (
                        origin,
                        mutant_id,
                        str(record.get("direct_status")),
                        str(record.get("error")),
                    )
                    if key in archived_shard_keys:
                        continue
                    attempt_counts[mutant_id] = attempt_counts.get(mutant_id, 0) + 1
                    handle.write(json.dumps({
                        **record,
                        "format": "rq1-direct-mutant-attempt/v1",
                        "archived_at_utc": datetime.now(timezone.utc).isoformat(),
                        "attempt_number": attempt_counts[mutant_id],
                        "recovered_from_worker_shard": origin,
                    }, ensure_ascii=False, separators=(",", ":")) + "\n")
        recoverable = {
            mutant_id: record
            for mutant_id, record in candidate_records.items()
            if mutant_id in pending_ids
        }
        if recoverable:
            print(
                f"Direct shard recovery recovered {len(recoverable)} pending results",
                flush=True,
            )
            existing_by_id = {record["mutant_id"]: record for record in existing}
            existing_by_id.update(recoverable)
            write_text_file(
                results_path,
                "".join(
                    json.dumps(existing_by_id[mutant["mutant_id"]], ensure_ascii=False, separators=(",", ":")) + "\n"
                    for mutant in config["mutants"]
                    if mutant["mutant_id"] in existing_by_id
                ),
            )
            return len(recoverable)
    return 0


def run_parallel_classification(
    runtime_dir: Path,
    runtime_manifest: dict[str, Any],
    config: dict[str, Any],
    worker_path: Path,
    *,
    workers: int,
    timeout_seconds: int,
    reuse_existing_only: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    """Classify pending mutants in isolated runtime copies, then merge deterministically."""
    results_path = runtime_dir / config["results_path"]
    existing = [
        json.loads(line)
        for line in read_text(results_path).splitlines()
        if line.strip()
    ] if path_is_file(results_path) else []
    completed_ids = {record["mutant_id"] for record in existing}
    pending = [mutant for mutant in config["mutants"] if mutant["mutant_id"] not in completed_ids]
    if not pending:
        return existing, ""

    worker_count = min(workers, len(pending))
    partitions = [pending[index::worker_count] for index in range(worker_count)]
    worker_results: list[tuple[int, list[dict[str, Any]], str, Path]] = []
    pending_ids = {mutant["mutant_id"] for mutant in pending}

    # Reuse complete shards from an interrupted merge before starting costly tests again.
    candidate_roots = sorted(
        runtime_dir.parent.glob(f"{runtime_dir.name}-direct-workers-*"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for candidate_root in candidate_roots:
        candidate_results: list[tuple[int, list[dict[str, Any]], str, Path]] = []
        candidate_ids: set[str] = set()
        valid_candidate = True
        for worker_dir in sorted(candidate_root.glob("worker-*")):
            try:
                worker_index = int(worker_dir.name.rsplit("-", 1)[1])
            except (IndexError, ValueError):
                valid_candidate = False
                break
            if not 1 <= worker_index <= worker_count:
                valid_candidate = False
                break
            assigned_ids = {mutant["mutant_id"] for mutant in partitions[worker_index - 1]}
            worker_config_path = worker_dir / "direct_mutant_classification_config.json"
            worker_config = read_json(worker_config_path) if path_is_file(worker_config_path) else None
            if worker_config is not None and worker_config.get("parent_classification_config_sha256") != config["classification_config_sha256"]:
                valid_candidate = False
                break
            worker_results_path = worker_dir / (
                str(worker_config.get("results_path")) if worker_config is not None
                else f"direct-worker-{worker_index:02d}.jsonl"
            )
            worker_summary_path = worker_dir / (
                str(worker_config.get("summary_path")) if worker_config is not None
                else f"direct-worker-{worker_index:02d}-summary.json"
            )
            progress_path = worker_dir / (
                str(worker_config.get("progress_path")) if worker_config is not None
                else f"direct-worker-{worker_index:02d}.log"
            )
            if not path_is_file(worker_results_path) or not path_is_file(worker_summary_path):
                valid_candidate = False
                break
            worker_rows = [
                json.loads(line)
                for line in read_text(worker_results_path).splitlines()
                if line.strip()
            ]
            row_ids = {record["mutant_id"] for record in worker_rows}
            worker_summary = read_json(worker_summary_path)
            raw_status_by_id = {item["mutant_id"]: item["mutmut_status"] for item in config["mutants"]}
            rows_valid = True
            for record in worker_rows:
                mutant_id = record["mutant_id"]
                matched_tests = config["mutant_test_ids"].get(mutant_id, [])
                no_test_evidence = not matched_tests and raw_status_by_id.get(mutant_id) == "no tests"
                source_changed = record.get("source_changed")
                expected_tests = [] if no_test_evidence else matched_tests or config["stable_test_ids"]
                expected_method = (
                    "mutmut_no_tests_without_subprocess_hit" if no_test_evidence
                    else "subprocess_function_hits" if matched_tests
                    else "full_stable_suite_fallback"
                )
                if (
                    record.get("mutmut_status") != raw_status_by_id.get(mutant_id)
                    or record.get("selected_test_ids") != expected_tests
                    or record.get("test_selection_method") != expected_method
                    or record.get("direct_status") not in FINAL_DIRECT_STATUSES
                    or (
                        record.get("direct_status") in TERMINAL_DIRECT_STATUSES
                        and record.get("error") is not None
                    )
                    or (
                        record.get("direct_status") in {"error", "timeout"}
                        and not record.get("error")
                    )
                    or not isinstance(source_changed, dict)
                    or set(source_changed) != set(config["source_paths"])
                    or (
                        record.get("direct_status") in {"killed", "survived"}
                        and (
                            not all(source_changed.values())
                            or record.get("apply_exit_code") != 0
                        )
                    )
                ):
                    rows_valid = False
                    break
            if (
                len(worker_rows) != len(assigned_ids)
                or row_ids != assigned_ids
                or not rows_valid
                or worker_summary.get("complete") not in {
                    all(record.get("direct_status") in TERMINAL_DIRECT_STATUSES for record in worker_rows),
                    all(record.get("direct_status") in FINAL_DIRECT_STATUSES for record in worker_rows),
                }
                or worker_summary.get("classified_count") != len(assigned_ids)
                or worker_summary.get("source_sha256") != config["source_sha256"]
                or worker_summary.get("task_id") != config["task_id"]
                or worker_summary.get("checkpoint_id") != config["checkpoint_id"]
                or worker_summary.get("baseline_audit_sha256") != config["baseline_audit_sha256"]
                or (worker_config is not None and worker_config.get("classification_config_sha256") != worker_summary.get("classification_config_sha256"))
            ):
                valid_candidate = False
                break
            if candidate_ids & row_ids:
                valid_candidate = False
                break
            for source_name, expected_hash in config["source_sha256"].items():
                actual_hash = sha256_file(worker_dir / Path(*source_name.split("/")))
                if actual_hash != expected_hash:
                    valid_candidate = False
                    break
            if not valid_candidate:
                break
            candidate_ids.update(row_ids)
            progress = read_text(progress_path) if path_is_file(progress_path) else ""
            candidate_results.append((worker_index, worker_rows, progress, worker_dir))
        if valid_candidate and candidate_ids == pending_ids and len(candidate_results) == worker_count:
            worker_results = candidate_results
            break

    if worker_results:
        parallel_root = worker_results[0][3].parent
        worker_count = len(worker_results)
    else:
        if reuse_existing_only:
            raise FileNotFoundError("no complete, hash-matched parallel worker shards are available to merge")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        parallel_root = runtime_dir.parent / f"{runtime_dir.name}-direct-workers-{stamp}"
        parallel_root.mkdir(parents=True, exist_ok=False)

    def ignore_transient(_: str, names: list[str]) -> set[str]:
        transient = {
            "__pycache__",
            ".pytest_cache",
            ".venv",
            ".coverage",
            "direct_mutant_classification.jsonl",
            "direct_mutant_classification.log",
            "direct_mutant_classification_config.json",
            "direct_mutant_classification_summary.json",
            "direct_mutant_classification_worker.py",
            ".rq1_direct_mutant_pytest_report.json",
        }
        return {name for name in names if name in transient or name.startswith(".coverage.")}

    def classify_partition(index: int, mutants: list[dict[str, Any]]) -> tuple[int, list[dict[str, Any]], str, Path]:
        worker_dir = parallel_root / f"worker-{index:02d}"
        shutil.copytree(
            filesystem_path(runtime_dir),
            filesystem_path(worker_dir),
            symlinks=True,
            ignore=ignore_transient,
        )
        worker_manifest = read_json(worker_dir / "runtime_manifest.json")
        worker_manifest["staging_directory"] = str(worker_dir)
        write_text_file(worker_dir / "runtime_manifest.json", json.dumps(worker_manifest, indent=2) + "\n")

        worker_config = dict(config)
        worker_config["mutants"] = mutants
        worker_config["resume"] = False
        worker_config["results_path"] = f"direct-worker-{index:02d}.jsonl"
        worker_config["progress_path"] = f"direct-worker-{index:02d}.log"
        worker_config["summary_path"] = f"direct-worker-{index:02d}-summary.json"
        worker_config["parent_classification_config_sha256"] = config["classification_config_sha256"]
        worker_signature = {
            key: value
            for key, value in worker_config.items()
            if key not in {"resume", "classification_config_sha256"}
        }
        worker_config["classification_config_sha256"] = json_sha256(worker_signature)
        worker_config_path = worker_dir / "direct_mutant_classification_config.json"
        worker_script_path = worker_dir / "direct_mutant_classification_worker.py"
        write_text_file(worker_config_path, json.dumps(worker_config, indent=2) + "\n")
        write_text_file(worker_script_path, read_text(worker_path))

        command = [PYTHON_BIN, "direct_mutant_classification_worker.py", worker_config_path.name]
        worker_timeout = (timeout_seconds + 30) * len(mutants) + 900
        run_result = run_in_docker(worker_dir, worker_manifest, command, timeout=worker_timeout)
        write_text_file(runtime_dir / f"direct-classifier-worker-{index:02d}.stdout.log", run_result.stdout)
        write_text_file(runtime_dir / f"direct-classifier-worker-{index:02d}.stderr.log", run_result.stderr)
        if run_result.returncode != 0:
            raise RuntimeError(
                f"parallel direct-classification worker {index} failed ({run_result.returncode}): {run_result.stderr[-3000:]}"
            )

        worker_results_path = worker_dir / worker_config["results_path"]
        rows = [
            json.loads(line)
            for line in read_text(worker_results_path).splitlines()
            if line.strip()
        ]
        expected_ids = {mutant["mutant_id"] for mutant in mutants}
        actual_ids = {record["mutant_id"] for record in rows}
        if len(rows) != len(mutants) or actual_ids != expected_ids:
            raise ValueError(
                f"parallel worker {index} returned {len(rows)} records for {len(mutants)} assigned mutants"
            )
        worker_summary = read_json(worker_dir / worker_config["summary_path"])
        if worker_summary.get("complete") is not True:
            raise ValueError(f"parallel worker {index} produced an incomplete direct-classification summary")
        for source_name, expected_hash in config["source_sha256"].items():
            actual_hash = sha256_file(worker_dir / Path(*source_name.split("/")))
            if actual_hash != expected_hash:
                raise ValueError(f"parallel worker {index} did not restore source {source_name}")
        progress_path = worker_dir / worker_config["progress_path"]
        progress = read_text(progress_path) if path_is_file(progress_path) else ""
        return index, rows, progress, worker_dir

    if not worker_results:
        futures = []
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = [
                pool.submit(classify_partition, index, partition)
                for index, partition in enumerate(partitions, 1)
            ]
            for future in as_completed(futures):
                worker_results.append(future.result())

    merged = {record["mutant_id"]: record for record in existing}
    progress_lines: list[str] = []
    for index, rows, progress, worker_dir in sorted(worker_results, key=lambda item: item[0]):
        progress_lines.append(f"parallel worker {index}: {worker_dir}")
        if progress:
            progress_lines.extend(f"  {line}" for line in progress.splitlines())
        for record in rows:
            mutant_id = record["mutant_id"]
            if mutant_id in merged:
                raise ValueError(f"duplicate direct-classification record for mutant {mutant_id}")
            merged[mutant_id] = record

    expected_order = [mutant["mutant_id"] for mutant in config["mutants"]]
    if set(merged) != set(expected_order):
        missing = sorted(set(expected_order) - set(merged))
        extra = sorted(set(merged) - set(expected_order))
        raise ValueError(f"merged direct classification does not match raw mutants; missing={missing}, extra={extra}")
    ordered_records = [merged[mutant_id] for mutant_id in expected_order]
    write_text_file(
        results_path,
        "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in ordered_records),
    )
    manifest_path = runtime_dir / "direct_mutant_classification_parallel_manifest.json"
    write_text_file(
        manifest_path,
        json.dumps({
            "format": "rq1-direct-classification-parallel-run/v1",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "worker_count": worker_count,
            "runtime_directory": str(runtime_dir),
            "worker_directories": [str(item[3]) for item in sorted(worker_results, key=lambda item: item[0])],
            "raw_mutant_count": len(config["mutants"]),
            "previously_classified_count": len(existing),
            "newly_classified_count": len(pending),
            "source_sha256": config["source_sha256"],
            "classification_config_sha256": config["classification_config_sha256"],
        }, indent=2) + "\n",
    )
    return ordered_records, "\n".join(progress_lines)


WORKER = r'''from __future__ import annotations
import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

def run_with_process_group(command, timeout):
    if os.name == "nt":
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            command,
            timeout,
            output=stdout,
            stderr=stderr,
        ) from error
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
root = Path.cwd()
sources = {name: root / name for name in config["source_paths"]}
originals = {name: path.read_bytes() for name, path in sources.items()}
expected_hashes = config["source_sha256"]
for name, path in sources.items():
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected_hashes[name]:
        raise SystemExit(f"source hash mismatch before direct classification: {name}")

results_path = root / config["results_path"]
progress_path = root / config["progress_path"]
report_path = root / ".rq1_direct_mutant_pytest_report.json"
if config["resume"] and results_path.is_file():
    completed = {
        json.loads(line)["mutant_id"]
        for line in results_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
else:
    completed = set()
    results_path.write_text("", encoding="utf-8")
progress_path.write_text("", encoding="utf-8")

def log(message: str) -> None:
    with progress_path.open("a", encoding="utf-8") as handle:
        handle.write(message + "\n")

def restore_sources() -> None:
    for name, content in originals.items():
        sources[name].write_bytes(content)

def record_result(record: dict) -> None:
    with results_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

try:
    mutants = config["mutants"]
    total = len(mutants)
    for number, mutant in enumerate(mutants, 1):
        mutant_id = mutant["mutant_id"]
        if mutant_id in completed:
            continue
        matched_test_ids = config["mutant_test_ids"].get(mutant_id, [])
        no_test_evidence = (
            not matched_test_ids
            and mutant["mutmut_status"] == "no tests"
        )
        selected_test_ids = [] if no_test_evidence else matched_test_ids or config["stable_test_ids"]
        selection_method = (
            "mutmut_no_tests_without_subprocess_hit" if no_test_evidence
            else "subprocess_function_hits" if matched_test_ids
            else "full_stable_suite_fallback"
        )
        started = time.monotonic()
        stdout = ""
        stderr = ""
        apply_stdout = ""
        apply_stderr = ""
        apply_exit_code = None
        changed = {name: False for name in sources}
        error_test_ids = []
        if no_test_evidence:
            status = "not_tested"
            failures = []
            error = None
            pytest_exit_code = None
        else:
            restore_sources()
            apply = subprocess.run(
                [config["mutmut_bin"], "apply", mutant_id],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=config["timeout_seconds"], check=False,
            )
            apply_stdout, apply_stderr = apply.stdout, apply.stderr
            apply_exit_code = apply.returncode
            changed = {
                name: hashlib.sha256(path.read_bytes()).hexdigest() != expected_hashes[name]
                for name, path in sources.items()
            }
            if apply.returncode != 0 or not any(changed.values()):
                status = "error"
                failures = []
                error = "mutmut apply failed or did not change a source file"
                pytest_exit_code = None
            else:
                if report_path.exists():
                    report_path.unlink()
                try:
                    selected_command = config["pytest_command"][:-len(config["stable_test_ids"])] + selected_test_ids
                    tested = run_with_process_group(
                        selected_command,
                        timeout=config["timeout_seconds"],
                    )
                except subprocess.TimeoutExpired as exc:
                    status = "timeout"
                    failures = []
                    error = f"pytest timed out after {config['timeout_seconds']} seconds"
                    pytest_exit_code = None
                    stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                    stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
                else:
                    pytest_exit_code = tested.returncode
                    stdout, stderr = tested.stdout, tested.stderr
                    report = None
                    if report_path.is_file():
                        try:
                            report = json.loads(report_path.read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError):
                            report = None
                    outcomes = report.get("tests", []) if isinstance(report, dict) else []
                    failures = sorted(
                        str(item["nodeid"])
                        for item in outcomes
                        if item.get("outcome") == "failed" and isinstance(item.get("nodeid"), str)
                    )
                    errors = sorted(
                        str(item.get("nodeid", "<collection>"))
                        for item in outcomes
                        if item.get("outcome") == "error"
                    )
                    expected_count = len(selected_test_ids)
                    environment_error_markers = (
                        "cannot allocate memory",
                        "memoryerror",
                        "resource temporarily unavailable",
                        "no space left on device",
                        "permission denied",
                        "timed out",
                    )
                    output_lower = (stdout + "\n" + stderr).lower()
                    execution_server_readiness_error = (
                        config.get("task_id") == "execution_server"
                        and config.get("checkpoint_id") == "C1"
                        and "server did not start:" in output_lower
                        and "connection refused" in output_lower
                        and bool(errors)
                        and all(
                            test_id.endswith("::TestCore::test_initial_stats")
                            for test_id in errors
                        )
                    )
                    has_environment_error = any(
                        marker in output_lower for marker in environment_error_markers
                    ) or (
                        "connection refused" in output_lower
                        and not execution_server_readiness_error
                    )
                    test_error_is_mutant_kill = (
                        bool(errors)
                        and set(errors) <= set(selected_test_ids)
                        and mutant["mutmut_status"] == "killed"
                        and not has_environment_error
                    )
                    if test_error_is_mutant_kill:
                        status = "killed"
                        error_test_ids = errors
                        error = None
                    elif errors:
                        status = "error"
                        error = f"pytest result incomplete: collected {len(outcomes)} of {expected_count}; errors={errors}"
                    elif failures:
                        status = "killed"
                        error = None
                    elif len(outcomes) != expected_count:
                        status = "error"
                        error = f"pytest result incomplete: collected {len(outcomes)} of {expected_count}; no failing test recorded"
                    elif tested.returncode == 0:
                        status = "survived"
                        error = None
                    else:
                        status = "error"
                        error = "pytest exited nonzero without a recorded failing test"

        record = {
            "mutant_id": mutant_id,
            "direct_status": status,
            "mutmut_status": mutant["mutmut_status"],
            "selected_test_ids": selected_test_ids,
            "test_selection_method": selection_method,
            "target_function_reached_in_baseline": bool(matched_test_ids) if matched_test_ids else False if no_test_evidence else None,
            "apply_exit_code": apply_exit_code,
            "source_changed": changed,
            "pytest_exit_code": pytest_exit_code,
            "failed_test_ids": failures,
            "error_test_ids": error_test_ids,
            "classification_rationale": (
                "execution_server C1 test_initial_stats readiness error was caused by a raw-killed mutant; the fixture's final connection-refused text can be stale after a non-200 response"
                if error_test_ids and execution_server_readiness_error else
                "stable selected test reported an error for a raw-killed mutant; recorded as killed with the test error retained as evidence"
                if error_test_ids else None
            ),
            "error": error,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        if status in {"error", "timeout"} or error_test_ids:
            log(json.dumps({"mutant_id": mutant_id, "status": status, "error": error, "error_test_ids": error_test_ids, "apply_stdout": apply_stdout[-2000:], "apply_stderr": apply_stderr[-2000:], "pytest_stdout": stdout[-6000:], "pytest_stderr": stderr[-3000:]}, ensure_ascii=False))
        record_result(record)
        completed.add(mutant_id)
        if number == 1 or number % 10 == 0 or number == total:
            log(f"progress {number}/{total}; last={mutant_id}; status={status}")
finally:
    restore_sources()

for name, path in sources.items():
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected_hashes[name]:
        raise SystemExit(f"source was not restored after direct classification: {name}")

records = [json.loads(line) for line in results_path.read_text(encoding="utf-8").splitlines() if line.strip()]
counts = {}
for record in records:
    counts[record["direct_status"]] = counts.get(record["direct_status"], 0) + 1
summary = {
    "format": "rq1-direct-mutant-classification/v1",
    "task_id": config["task_id"],
    "checkpoint_id": config["checkpoint_id"],
    "baseline_audit_sha256": config["baseline_audit_sha256"],
    "source_sha256": expected_hashes,
    "mutant_count": total,
    "classified_count": len(records),
    "status_counts": counts,
    "test_error_kill_count": sum(bool(record.get("error_test_ids")) for record in records),
    "complete": len(records) == total and all(item["direct_status"] in {"killed", "survived", "not_tested", "error", "timeout"} for item in records),
    "all_attempts_resolved": len(records) == total and all(item["direct_status"] in {"killed", "survived", "not_tested"} for item in records),
    "inconclusive_count": sum(counts.get(status, 0) for status in ("not_tested", "error", "timeout")),
    "classification_algorithm_version": config["classification_algorithm_version"],
    "classification_config_sha256": config["classification_config_sha256"],
    "classification_method": "mutmut apply followed by stable passing C1 tests selected from subprocess-hit data; raw no-test statuses without matching subprocess hits are recorded as not_tested (not as survivors), and unmatched mutants use the full stable suite; execution errors and timeouts remain explicit inconclusive outcomes",
    "created_at_utc": datetime.now(timezone.utc).isoformat(),
}
(root / config["summary_path"]).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
print(json.dumps(summary, sort_keys=True))
'''


def matching_baseline(audit_path: Path, task_id: str, checkpoint_id: str) -> dict[str, Any]:
    audit = read_json(audit_path)
    matches = [
        record for record in audit.get("records", [])
        if record.get("task_id") == task_id and record.get("checkpoint_id") == checkpoint_id
    ]
    if len(matches) != 1:
        raise ValueError(f"baseline audit must contain exactly one {task_id}/{checkpoint_id} record")
    record = matches[0]
    if record.get("status") not in {"stable_pass", "stable_with_failures"} or record.get("unstable_test_ids"):
        raise ValueError(f"baseline audit is not stable for {task_id}/{checkpoint_id}")
    if not record.get("stable_pass_test_ids"):
        raise ValueError(f"baseline audit has no stable passing tests for {task_id}/{checkpoint_id}")
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True, help="Completed staged mutmut run containing its cache and result list")
    parser.add_argument("--baseline-audit", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--workers", type=int, default=1, help="Run mutants concurrently in isolated runtime copies")
    parser.add_argument("--resume", action="store_true", help="Resume from existing direct_mutant_classification.jsonl")
    args = parser.parse_args()

    runtime_dir = args.runtime_dir.resolve(strict=True)
    allowed_tmp = (REPO_ROOT / "tmp").resolve(strict=True)
    if not runtime_dir.is_relative_to(allowed_tmp):
        raise ValueError(f"runtime directory must remain under {allowed_tmp}")
    audit_path = args.baseline_audit.resolve(strict=True)
    allowed_results = (REPO_ROOT / "artifacts" / "results").resolve(strict=True)
    if not audit_path.is_relative_to(allowed_results):
        raise ValueError(f"baseline audit must remain under {allowed_results}")
    if args.timeout_seconds < 1 or args.workers < 1:
        raise ValueError("timeout and worker count must be positive")

    runtime_manifest = read_json(runtime_dir / "runtime_manifest.json")
    mutmut_run_manifest = read_json(runtime_dir / "mutmut_run_manifest.json")
    if (
        mutmut_run_manifest.get("mutation_protocol_version") != MUTATION_PROTOCOL_VERSION
        or not mutmut_run_manifest.get("mutation_config_sha256")
        or mutmut_run_manifest.get("bridge_sha256") != {
            name: sha256_file(runtime_dir / name) for name in MUTATION_BRIDGES
        }
        or mutmut_run_manifest.get("mutation_config", {}).get("runtime_config_sha256")
        != sha256_file(REPO_ROOT / "config" / "checkpoint_runtime.json")
        or mutmut_run_manifest.get("mutation_config", {}).get("mutant_selection") != []
    ):
        raise ValueError("raw mutmut run uses an obsolete or incomplete protocol; create a fresh versioned run")
    task_id = str(runtime_manifest["task_id"])
    checkpoint_id = str(runtime_manifest["checkpoint_id"])
    baseline = matching_baseline(audit_path, task_id, checkpoint_id)
    stable_ids = stable_pass_selection(baseline)
    if (
        runtime_manifest.get("base_tests_sha256") != baseline.get("base_tests_sha256")
        or mutmut_run_manifest.get("base_tests_sha256") != baseline.get("base_tests_sha256")
        or set(mutmut_run_manifest.get("mutation_test_selection") or []) != set(stable_ids)
    ):
        raise ValueError("mutmut run does not match the repeated C1 baseline and selected stable test IDs")
    result_file = runtime_dir / "mutmut-results.txt"
    if not result_file.is_file():
        result_file = runtime_dir / "mutmut-results.txt"
    statuses = parse_statuses(result_file.read_text(encoding="utf-8"))
    task_profile = runtime_manifest["task_runtime_profile"]
    profile = task_profile["mutmut"]
    source_paths = list(profile["source_paths"])
    test_command = list(runtime_manifest["test_command_argv"])
    if not test_command:
        raise ValueError("runtime has no pytest command")
    test_command[-1:] = stable_ids
    try:
        q_index = test_command.index("-q")
    except ValueError:
        q_index = -1
    test_command.insert((q_index + 1) if q_index >= 0 else 3, "--maxfail=1")
    q_index = test_command.index("-q")
    report_args = ["--json-report", "--json-report-file=/workspace/.rq1_direct_mutant_pytest_report.json"]
    insert_at = q_index + 1 if q_index >= 0 else 3
    test_command[insert_at:insert_at] = report_args

    source_hashes = {
        relative: sha256_file(runtime_dir / Path(*relative.split("/")))
        for relative in source_paths
    }
    stable_test_id_set = set(stable_ids)
    function_tests: dict[str, set[str]] = {}
    hits_path = runtime_dir / "mutmut-subprocess-hits.jsonl"
    if hits_path.is_file():
        module_name = Path(str(task_profile["entry_file"])).stem
        for line_number, line in enumerate(hits_path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                hit = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"malformed subprocess-hit record at {hits_path}:{line_number}") from error
            name = str(hit.get("name", ""))
            if name.startswith("__main__."):
                name = module_name + name[len("__main__"):]
            test_id = str(hit.get("test_id", ""))
            if name and test_id in stable_test_id_set:
                function_tests.setdefault(name, set()).add(test_id)
    mutant_test_ids = {
        mutant_id: [test_id for test_id in stable_ids if test_id in function_tests[mutant_id.rsplit("__mutmut_", 1)[0]]]
        for mutant_id in statuses
        if mutant_id.rsplit("__mutmut_", 1)[0] in function_tests
    }
    config = {
        "classification_algorithm_version": DIRECT_CLASSIFIER_VERSION,
        "mutation_protocol_version": MUTATION_PROTOCOL_VERSION,
        "mutation_run_config_sha256": mutmut_run_manifest["mutation_config_sha256"],
        "task_id": task_id,
        "checkpoint_id": checkpoint_id,
        "source_paths": source_paths,
        "source_sha256": source_hashes,
        "stable_test_ids": stable_ids,
        "mutant_test_ids": mutant_test_ids,
        "pytest_command": test_command,
        "mutmut_bin": MUTMUT_BIN,
        "timeout_seconds": args.timeout_seconds,
        "resume": args.resume,
        "results_path": "direct_mutant_classification.jsonl",
        "progress_path": "direct_mutant_classification.log",
        "summary_path": "direct_mutant_classification_summary.json",
        "baseline_audit_sha256": sha256_file(audit_path),
        "mutants": [
            {"mutant_id": mutant_id, "mutmut_status": status}
            for mutant_id, status in statuses.items()
        ],
    }
    config_path = runtime_dir / "direct_mutant_classification_config.json"
    signature_payload = {key: value for key, value in config.items() if key != "resume"}
    config["classification_config_sha256"] = json_sha256(signature_payload)
    if args.resume:
        if not config_path.is_file():
            raise ValueError("cannot resume without the prior direct-classification config; use a fresh mutmut runtime")
        previous_config = read_json(config_path)
        if previous_config.get("classification_config_sha256") != config["classification_config_sha256"]:
            raise ValueError("direct-classification config changed; refusing to mix results, use a fresh mutmut runtime")
    elif (runtime_dir / config["results_path"]).exists():
        raise ValueError("direct-classification results already exist; use --resume with an identical config or a fresh mutmut runtime")
    if args.resume:
        recovered = recover_parallel_worker_results(
            runtime_dir,
            config,
            runtime_dir / config["results_path"],
        )
        retried = retry_inconclusive_records(runtime_dir, runtime_dir / config["results_path"])
        if recovered or retried:
            print(
                "Recovered "
                f"{recovered} partial worker results; archived and retrying "
                f"{retried} inconclusive direct-classification attempts"
            )
    worker_path = runtime_dir / "direct_mutant_classification_worker.py"
    write_text_file(config_path, json.dumps(config, indent=2) + "\n")
    write_text_file(worker_path, WORKER)

    worker_command = [PYTHON_BIN, "direct_mutant_classification_worker.py", "direct_mutant_classification_config.json"]
    total_timeout = (args.timeout_seconds + 30) * len(statuses) + 900
    if args.workers > 1:
        progress_path = runtime_dir / config["progress_path"]
        old_progress = progress_path.read_text(encoding="utf-8") if progress_path.is_file() else ""
        _, worker_progress = run_parallel_classification(
            runtime_dir,
            runtime_manifest,
            config,
            worker_path,
            workers=args.workers,
            timeout_seconds=args.timeout_seconds,
        )
        config["resume"] = True
        write_text_file(config_path, json.dumps(config, indent=2) + "\n")
        result = run_in_docker(runtime_dir, runtime_manifest, worker_command, timeout=1200)
        if result.returncode == 0:
            summary = read_json(runtime_dir / config["summary_path"])
            progress_parts = [part for part in (old_progress.strip(), worker_progress.strip()) if part]
            progress_parts.append(
                f"parallel direct classification complete: {summary.get('classified_count')}/{summary.get('mutant_count')}"
            )
            write_text_file(progress_path, "\n".join(progress_parts) + "\n")
    else:
        result = run_in_docker(runtime_dir, runtime_manifest, worker_command, timeout=total_timeout)
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr[-8000:], file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
    if result.returncode != 0:
        print(f"Direct classifier Docker run failed with exit code {result.returncode}", file=sys.stderr)
        return 2
    print(f"Direct mutant results: {runtime_dir / 'direct_mutant_classification.jsonl'}")
    print(f"Direct classifier progress: {runtime_dir / 'direct_mutant_classification.log'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

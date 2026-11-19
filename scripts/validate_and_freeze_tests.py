"""Validate generated pytest candidates on their source checkpoint and freeze them."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTAINER_WORKSPACE = "/workspace"
GENERATION_SCHEMA = REPO_ROOT / "schemas" / "generated-test-candidates.schema.json"
FROZEN_SCHEMA = REPO_ROOT / "schemas" / "frozen-test-suite.schema.json"
GENERATION_SCHEMA_PATH = REPO_ROOT / "schemas" / "generation-run.schema.json"
MUTMUT_RUNNER = REPO_ROOT / "scripts" / "run_checkpoint_mutmut.py"
MAX_CANDIDATE_BYTES = 128_000
PROHIBITED_IMPORTS = {"random", "secrets"}
HTTP_CLIENT_MODULES = {"httpx", "requests"}
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "request", "stream"}
LOCAL_API_FIXTURES = {"api_server", "base_url", "server_url", "local_server_url"}
LOCAL_API_SERVER_OBJECTS = {"server", "mock_server", "http_server"}
NONDETERMINISTIC_CALLS = {"monotonic", "now", "perf_counter", "sleep", "time", "today", "utcnow"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def filesystem_path(path: Path) -> str:
    """Return a Windows extended-length path so deeply nested benchmark files can be copied."""
    value = os.path.abspath(os.fspath(path))
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        return "\\\\?\\" + value
    return value


def write_text_file(path: Path, content: str) -> None:
    with open(filesystem_path(path), "w", encoding="utf-8", newline="") as handle:
        handle.write(content)


def validate_candidate_response(
    value: Any,
    max_candidates: int,
    *,
    method: str,
    eligible_mutant_ids: set[str],
) -> list[dict[str, Any]]:
    if (
        not isinstance(value, dict)
        or set(value) not in ({"tests"}, {"tests", "assessment"})
        or not isinstance(value["tests"], list)
        or ("assessment" in value and (not isinstance(value["assessment"], str) or not value["assessment"].strip()))
    ):
        raise ValueError("candidate response must contain a tests array and optional non-empty assessment")
    candidates = value["tests"]
    if len(candidates) > max_candidates:
        raise ValueError(f"candidate-file budget exceeded: {len(candidates)} > {max_candidates}")
    seen: set[str] = set()
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict) or set(candidate) != {
            "relative_path", "content", "specification_basis", "target_mutant_ids"
        }:
            raise ValueError(f"candidate {index} has invalid or extra fields")
        relative = candidate["relative_path"]
        pure = PurePosixPath(relative) if isinstance(relative, str) else PurePosixPath(".")
        if not isinstance(relative, str) or not re.fullmatch(r"test_[A-Za-z0-9_]+\.py", relative):
            raise ValueError(f"candidate {index} has invalid test path: {relative!r}")
        if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
            raise ValueError(f"candidate {index} attempts to escape its test directory")
        if relative in seen:
            raise ValueError(f"duplicate candidate path: {relative}")
        seen.add(relative)
        if not isinstance(candidate["content"], str) or not candidate["content"].strip():
            raise ValueError(f"candidate {relative} has empty content")
        if len(candidate["content"].encode("utf-8")) > MAX_CANDIDATE_BYTES:
            raise ValueError(f"candidate {relative} exceeds the per-file byte limit")
        if not isinstance(candidate["specification_basis"], str) or not candidate["specification_basis"].strip():
            raise ValueError(f"candidate {relative} has no specification basis")
        targets = candidate["target_mutant_ids"]
        if not isinstance(targets, list) or any(not isinstance(item, str) for item in targets):
            raise ValueError(f"candidate {relative} target_mutant_ids must be an array of strings")
        if len(set(targets)) != len(targets):
            raise ValueError(f"candidate {relative} repeats target mutant IDs")
        if method == "vanilla_llm":
            if targets:
                raise ValueError(f"Vanilla candidate {relative} must have no target mutant IDs")
        elif method == "mutation_guided":
            unknown = sorted(set(targets) - eligible_mutant_ids)
            if unknown:
                raise ValueError(f"candidate {relative} references mutants outside the eligible input: {unknown}")
        else:
            raise ValueError(f"unsupported generation method: {method}")
    return candidates


def verify_guided_targets(
    candidate_stage: Path,
    target_ids: list[str],
    candidate_dir: Path,
    *,
    candidate_test_ids: list[str],
    timeout: int,
    repeat_runs: int = 1,
) -> tuple[list[dict[str, Any]], float, str | None]:
    command = [sys.executable, str(MUTMUT_RUNNER), "--runtime-dir", str(candidate_stage), "--max-children", "4"]
    for mutant_id in target_ids:
        command.extend(["--mutant", mutant_id])
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
        stderr = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
        write_text_file(candidate_dir / "mutmut-validation.stdout.log", stdout)
        write_text_file(candidate_dir / "mutmut-validation.stderr.log", stderr + f"\nTimed out after {timeout} seconds.\n")
        return ([{"mutant_id": mutant_id, "status": "timeout", "killed": False} for mutant_id in target_ids], time.monotonic() - started, "targeted mutmut run timed out")

    elapsed = time.monotonic() - started
    write_text_file(candidate_dir / "mutmut-validation.stdout.log", result.stdout)
    write_text_file(candidate_dir / "mutmut-validation.stderr.log", result.stderr)
    results_match = re.search(r"^Mutmut results: (.+)$", result.stdout, flags=re.MULTILINE)
    if result.returncode != 0 or not results_match:
        return ([{"mutant_id": mutant_id, "status": "error", "killed": False} for mutant_id in target_ids], elapsed, f"targeted mutmut runner failed with exit code {result.returncode}")

    results_path = Path(results_match.group(1).strip()).resolve(strict=True)
    allowed_tmp = (REPO_ROOT / "tmp").resolve(strict=True)
    if not results_path.is_relative_to(allowed_tmp):
        return ([{"mutant_id": mutant_id, "status": "error", "killed": False} for mutant_id in target_ids], elapsed, "mutmut result file escaped repository tmp directory")
    mutation_stage = results_path.parent
    for source_name, destination_name in (
        ("mutmut_run_manifest.json", "mutmut-validation-run-manifest.json"),
        ("mutmut-run.log", "mutmut-validation-run.log"),
        ("mutmut-subprocess-hits.jsonl", "mutmut-validation-subprocess-hits.jsonl"),
    ):
        source_file = mutation_stage / source_name
        if source_file.is_file():
            shutil.copy2(filesystem_path(source_file), filesystem_path(candidate_dir / destination_name))
    shutil.copy2(filesystem_path(results_path), filesystem_path(candidate_dir / "mutmut-validation-results.txt"))
    status_pattern = re.compile(r"^\s*(\S+):\s*(killed|survived|not checked|no tests|skipped|suspicious|timeout|segfault)\s*$", re.MULTILINE)
    statuses = {mutant_id: status for mutant_id, status in status_pattern.findall(results_path.read_text(encoding="utf-8"))}
    checks = []
    direct_seconds = 0.0
    original_runtime_manifest = read_json(candidate_stage / "runtime_manifest.json")
    pytest_command = list(original_runtime_manifest["test_command_argv"])
    if not pytest_command:
        return ([{"mutant_id": mutant_id, "status": "error", "killed": False} for mutant_id in target_ids], elapsed, "runtime manifest has no pytest command")
    pytest_command[-1:] = candidate_test_ids

    # mutmut's status can be misleading for CLI projects whose test invokes the
    # entrypoint in a subprocess. Verify each target independently by applying
    # that exact cached mutant and executing only this generated test.
    if repeat_runs < 1:
        raise ValueError("target mutant repeat count must be positive")
    source_paths = list(original_runtime_manifest["task_runtime_profile"]["mutmut"]["source_paths"])
    for index, mutant_id in enumerate(target_ids, 1):
        repetition_checks: list[dict[str, Any]] = []
        for repetition in range(1, repeat_runs + 1):
            with tempfile.TemporaryDirectory(
                prefix=f"guided-mutant-{index:02d}-r{repetition:02d}-",
                dir=REPO_ROOT / "tmp" / "validation",
                ignore_cleanup_errors=True,
            ) as direct_temp:
                direct_stage = Path(direct_temp) / "workspace"
                shutil.copytree(filesystem_path(mutation_stage), filesystem_path(direct_stage))
                source_before = {name: sha256_file(direct_stage / name) for name in source_paths}
                apply_command = ["/opt/mutmut-env/bin/mutmut", "apply", mutant_id]
                shell_command = shlex.join(apply_command) + " && " + shlex.join(pytest_command)
                started_direct = time.monotonic()
                direct = run_in_docker(direct_stage, original_runtime_manifest, ["sh", "-lc", shell_command], timeout=timeout)
                direct_seconds += time.monotonic() - started_direct
                log_prefix = f"direct-mutant-{index:02d}-run-{repetition:02d}"
                write_text_file(candidate_dir / f"{log_prefix}.stdout.log", direct.stdout)
                write_text_file(candidate_dir / f"{log_prefix}.stderr.log", direct.stderr)
                source_after = {name: sha256_file(direct_stage / name) for name in source_paths}
                source_changed = source_after != source_before
                candidate_failed = any(
                    re.search(r"(?m)^(?:FAILED|ERROR)\s+" + re.escape(test_id) + r"(?:\s|$)", direct.stdout)
                    for test_id in candidate_test_ids
                )
                if direct.returncode == 0 and source_changed:
                    direct_status = "survived"
                elif direct.returncode != 0 and source_changed and candidate_failed:
                    direct_status = "killed"
                else:
                    direct_status = "error"
                repetition_checks.append({
                    "repetition": repetition,
                    "status": direct_status,
                    "killed": direct_status == "killed",
                    "apply_and_test_exit_code": direct.returncode,
                    "source_changed": source_changed,
                    "candidate_failed": candidate_failed,
                    "stdout_log": f"{log_prefix}.stdout.log",
                    "stderr_log": f"{log_prefix}.stderr.log",
                })
        statuses_seen = [item["status"] for item in repetition_checks]
        if all(item == "killed" for item in statuses_seen):
            aggregate_status = "killed"
        elif all(item == "survived" for item in statuses_seen):
            aggregate_status = "survived"
        elif all(item == "error" for item in statuses_seen):
            aggregate_status = "error"
        else:
            aggregate_status = "unstable"
        checks.append({
            "mutant_id": mutant_id,
            "status": aggregate_status,
            "killed": aggregate_status == "killed",
            "mutmut_status": statuses.get(mutant_id, "missing"),
            "verification": "mutmut apply in a fresh runtime copy followed by the candidate pytest node ID",
            "candidate_test_ids": candidate_test_ids,
            "repeat_runs": repeat_runs,
            "repetition_results": repetition_checks,
        })
    elapsed += direct_seconds
    not_killed = [item["mutant_id"] for item in checks if not item["killed"]]
    error = f"target mutants were not killed or direct verification failed: {not_killed}" if not_killed else None
    return checks, elapsed, error


UNIFIED_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _diff_path(value: str) -> str:
    value = value.strip().split("\t", 1)[0]
    if value.startswith(("a/", "b/")):
        value = value[2:]
    return value


def apply_frozen_mutation_diff(source_path: Path, diff: str, expected_relative_path: str) -> str:
    """Apply an archived mutmut diff only when each old hunk has a unique source match."""
    lines = diff.replace("\r\n", "\n").splitlines(keepends=True)
    if len(lines) < 3 or not lines[0].startswith("--- ") or not lines[1].startswith("+++ "):
        raise ValueError("mutant record does not contain a unified diff header")
    old_path, new_path = _diff_path(lines[0][4:]), _diff_path(lines[1][4:])
    expected = PurePosixPath(expected_relative_path).as_posix()
    if old_path != new_path or old_path != expected:
        raise ValueError(f"mutant diff path mismatch: {old_path!r} -> {new_path!r}; expected {expected!r}")

    raw_source = source_path.read_bytes()
    source_text = raw_source.decode("utf-8")
    newline = "\r\n" if b"\r\n" in raw_source else "\n"
    source_lines = source_text.replace("\r\n", "\n").splitlines(keepends=True)
    index = 2
    hunk_count = 0
    while index < len(lines):
        match = UNIFIED_HUNK_RE.match(lines[index])
        if not match:
            index += 1
            continue
        old_start = int(match.group(1))
        old_count = int(match.group(2) or "1")
        new_count = int(match.group(4) or "1")
        index += 1
        old_chunk: list[str] = []
        new_chunk: list[str] = []
        previous_prefix: str | None = None
        while index < len(lines) and not lines[index].startswith("@@ "):
            line = lines[index]
            if line.startswith("\\ No newline at end of file"):
                if previous_prefix in {" ", "-"} and old_chunk:
                    old_chunk[-1] = old_chunk[-1].removesuffix("\n")
                if previous_prefix in {" ", "+"} and new_chunk:
                    new_chunk[-1] = new_chunk[-1].removesuffix("\n")
                index += 1
                continue
            if not line or line[0] not in " +-":
                break
            # Some mutmut records end directly after the final context line
            # without a newline, even when the source line itself has one.
            # In unified diffs, only the explicit marker above means the file
            # line has no trailing newline.
            if not line.endswith("\n"):
                line += "\n"
            prefix, body = line[0], line[1:]
            if prefix in {" ", "-"}:
                old_chunk.append(body)
            if prefix in {" ", "+"}:
                new_chunk.append(body)
            previous_prefix = prefix
            index += 1
        if len(old_chunk) != old_count or len(new_chunk) != new_count:
            raise ValueError("mutant diff hunk line counts do not match its header")
        # mutmut can emit hunk coordinates relative to the extracted function,
        # while retaining file-level source text in its context. Require a unique
        # context match even if the header coordinate happens to match; otherwise
        # a coincidental duplicate could patch the wrong function. Insertion-only
        # hunks have no old context and therefore fail closed.
        matches = [
            candidate
            for candidate in range(max(0, len(source_lines) - old_count + 1))
            if source_lines[candidate : candidate + old_count] == old_chunk
        ]
        if old_count == 0 or len(matches) != 1:
            raise ValueError(
                f"mutant diff context does not uniquely match the frozen source at line {old_start}"
            )
        source_index = matches[0]
        source_lines[source_index : source_index + old_count] = new_chunk
        hunk_count += 1
    if hunk_count == 0:
        raise ValueError("mutant diff contains no unified-diff hunks")
    mutated_text = "".join(source_lines)
    if newline != "\n":
        mutated_text = mutated_text.replace("\n", newline)
    source_path.write_bytes(mutated_text.encode("utf-8"))
    return sha256_file(source_path)


def verify_paired_targets_from_diffs(
    candidate_stage: Path,
    target_ids: list[str],
    target_records: dict[str, dict[str, Any]],
    candidate_dir: Path,
    *,
    candidate_test_ids: list[str],
    runtime_manifest: dict[str, Any],
    timeout: int,
    repeat_runs: int,
) -> tuple[list[dict[str, Any]], float, str | None]:
    source_paths = list(runtime_manifest["task_runtime_profile"]["mutmut"]["source_paths"])
    command = list(runtime_manifest["test_command_argv"])
    command[-1:] = candidate_test_ids
    command.extend(["--json-report", "--json-report-file=/workspace/rq1_validation_report.json"])
    checks: list[dict[str, Any]] = []
    elapsed = 0.0
    for target_index, mutant_id in enumerate(target_ids, 1):
        record = target_records.get(mutant_id)
        if record is None or record.get("normalized_status") != "survived":
            return checks, elapsed, f"paired target mutant is missing from the frozen survivor records: {mutant_id}"
        location_path = record.get("location", {}).get("path")
        if location_path not in source_paths:
            return checks, elapsed, f"target mutant source path is outside the runtime mutation scope: {mutant_id}"
        repetitions: list[dict[str, Any]] = []
        mutated_hash: str | None = None
        for repetition in range(1, repeat_runs + 1):
            with tempfile.TemporaryDirectory(
                prefix=f"paired-diff-mutant-{target_index:02d}-r{repetition:02d}-",
                dir=REPO_ROOT / "tmp" / "validation",
                ignore_cleanup_errors=True,
            ) as direct_temp:
                direct_stage = Path(direct_temp) / "workspace"
                shutil.copytree(filesystem_path(candidate_stage), filesystem_path(direct_stage))
                source_file = direct_stage / location_path
                source_before = sha256_file(source_file)
                try:
                    mutated_hash = apply_frozen_mutation_diff(
                        source_file,
                        str(record["diff"]),
                        location_path.replace("\\", "/"),
                    )
                except (OSError, UnicodeError, ValueError) as error:
                    repetitions.append({
                        "repetition": repetition,
                        "status": "error",
                        "killed": False,
                        "error": f"cannot apply archived mutation diff: {error}",
                    })
                    continue
                source_changed = mutated_hash != source_before
                report_path = direct_stage / "rq1_validation_report.json"
                report_path.unlink(missing_ok=True)
                started = time.monotonic()
                try:
                    result = run_in_docker(direct_stage, runtime_manifest, command, timeout=timeout)
                except subprocess.TimeoutExpired:
                    repetitions.append({
                        "repetition": repetition,
                        "status": "timeout",
                        "killed": False,
                        "source_changed": source_changed,
                    })
                    continue
                elapsed += time.monotonic() - started
                log_prefix = f"paired-diff-mutant-{target_index:02d}-run-{repetition:02d}"
                write_text_file(candidate_dir / f"{log_prefix}.stdout.log", result.stdout)
                write_text_file(candidate_dir / f"{log_prefix}.stderr.log", result.stderr)
                try:
                    outcomes = read_pytest_outcomes(report_path)
                except (OSError, json.JSONDecodeError, RuntimeError) as error:
                    outcomes = {}
                    report_error = str(error)
                else:
                    report_error = None
                candidate_outcomes = [outcomes.get(test_id) for test_id in candidate_test_ids]
                candidate_failed = bool(candidate_outcomes) and all(item == "failed" for item in candidate_outcomes)
                candidate_passed = bool(candidate_outcomes) and all(item == "passed" for item in candidate_outcomes)
                if source_changed and candidate_failed:
                    status = "killed"
                elif source_changed and result.returncode == 0 and candidate_passed:
                    status = "survived"
                else:
                    status = "error"
                repetitions.append({
                    "repetition": repetition,
                    "status": status,
                    "killed": status == "killed",
                    "apply_and_test_exit_code": result.returncode,
                    "source_changed": source_changed,
                    "candidate_failed": candidate_failed,
                    "candidate_passed": candidate_passed,
                    "report_error": report_error,
                    "stdout_log": f"{log_prefix}.stdout.log",
                    "stderr_log": f"{log_prefix}.stderr.log",
                })
        statuses = [item["status"] for item in repetitions]
        if len(statuses) == repeat_runs and all(status == "killed" for status in statuses):
            aggregate = "killed"
        elif len(statuses) == repeat_runs and all(status == "survived" for status in statuses):
            aggregate = "survived"
        elif all(status in {"error", "timeout"} for status in statuses):
            aggregate = "error"
        else:
            aggregate = "unstable"
        checks.append({
            "mutant_id": mutant_id,
            "status": aggregate,
            "killed": aggregate == "killed",
            "mutmut_status": record.get("normalized_status", "unknown"),
            "verification": "apply exact frozen mutmut diff to a fresh source copy, then execute only the candidate pytest node",
            "candidate_test_ids": candidate_test_ids,
            "repeat_runs": repeat_runs,
            "mutation_diff_sha256": sha256_bytes(str(record["diff"]).encode("utf-8")),
            "mutated_source_sha256": mutated_hash,
            "repetition_results": repetitions,
        })
    not_killed = [item["mutant_id"] for item in checks if not item["killed"]]
    error = f"target mutants were not killed or direct verification failed: {not_killed}" if not_killed else None
    return checks, elapsed, error


def static_candidate_checks(relative: str, content: str) -> str | None:
    try:
        tree = ast.parse(content, filename=relative)
    except SyntaxError as error:
        return f"syntax error: {error}"
    test_functions = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
    ]
    if len(test_functions) != 1:
        return f"expected exactly one top-level test function, found {len(test_functions)}"
    bindings = local_url_bindings(tree)
    urllib_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and is_attribute_call(node, "urlopen", "urllib.request")
    ]
    local_urllib_calls = all(
        http_call_uses_local_fixture(node, bindings=bindings)
        and (
            any(item.arg == "timeout" for item in node.keywords)
            or any(
                isinstance(part, ast.Name) and part.id in LOCAL_API_FIXTURES
                for argument in [*node.args, *(item.value for item in node.keywords)]
                for part in ast.walk(argument)
            )
        )
        for node in urllib_calls
    )
    has_local_http = local_urllib_calls and bool(urllib_calls)
    socket_imported = any(
        isinstance(node, ast.Import) and any(alias.name == "socket" for alias in node.names)
        or isinstance(node, ast.ImportFrom) and node.module == "socket"
        for node in ast.walk(tree)
    )
    socket_bind_calls = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_name = alias.name
                if module_name in PROHIBITED_IMPORTS or any(
                    module_name.startswith(name + ".") for name in PROHIBITED_IMPORTS
                ):
                    return f"prohibited nondeterministic or network import: {module_name}"
                if module_name == "urllib.request" and not local_urllib_calls:
                    return "urllib.request is allowed only for timed requests to a loopback URL"
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_name = node.module
            if module_name in PROHIBITED_IMPORTS or any(
                module_name.startswith(name + ".") for name in PROHIBITED_IMPORTS
            ):
                return f"prohibited nondeterministic or network import: {module_name}"
            if module_name == "urllib.request" and not local_urllib_calls:
                return "urllib.request is allowed only for timed requests to a loopback URL"
        elif isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Attribute) and function.attr in NONDETERMINISTIC_CALLS:
                if function.attr not in {"monotonic", "sleep"} or not has_local_http:
                    return f"prohibited wall-clock or sleeping call: {function.attr}"
            if (
                socket_imported
                and isinstance(function, ast.Attribute)
                and function.attr in {"connect", "create_connection"}
            ):
                return "candidate tests may not open arbitrary network connections"
            if isinstance(function, ast.Attribute) and function.attr == "bind":
                if not is_loopback_ephemeral_bind(node):
                    return "socket.bind is allowed only for an ephemeral loopback port"
                socket_bind_calls += 1
            if (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id in HTTP_CLIENT_MODULES
                and function.attr in HTTP_METHODS
                and not http_call_uses_local_fixture(node, bindings=bindings)
            ):
                return "HTTP requests must use a declared local API fixture, not an external URL"
            if is_attribute_call(node, "urlopen", "urllib.request") and not local_urllib_calls:
                return "urllib.request.urlopen must use a timed loopback URL"
    if socket_imported and socket_bind_calls == 0:
        return "socket is allowed only to reserve an ephemeral loopback port"
    forbidden_paths = ("data/hidden_tests", "data/future_tests", "data/excluded_tests")
    lowered = content.lower().replace("\\", "/")
    if any(path in lowered for path in forbidden_paths):
        return "candidate refers to a hidden, future, or excluded data path"
    return None


def is_attribute_call(call: ast.Call, attribute: str, dotted_name: str) -> bool:
    current: ast.AST = call.func
    parts: list[str] = []
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return False
    parts.append(current.id)
    return ".".join(reversed(parts)) == f"{dotted_name}.{attribute}"


def local_url_bindings(tree: ast.AST) -> dict[str, ast.AST]:
    bindings: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            bindings[node.target.id] = node.value
    return bindings


def is_loopback_url_literal(value: str) -> bool:
    lowered = value.lower()
    hosts = "|".join(re.escape(host) for host in sorted(LOOPBACK_HOSTS, key=len, reverse=True))
    return re.match(rf"^https?://(?:{hosts})(?::[0-9]*)?(?:/|$)", lowered) is not None


def is_loopback_ephemeral_bind(call: ast.Call) -> bool:
    if not call.args or not isinstance(call.args[0], ast.Tuple) or len(call.args[0].elts) != 2:
        return False
    host, port = call.args[0].elts
    return (
        isinstance(host, ast.Constant)
        and isinstance(host.value, str)
        and host.value in LOOPBACK_HOSTS
        and isinstance(port, ast.Constant)
        and port.value == 0
    )


def http_call_uses_local_fixture(call: ast.Call, *, bindings: dict[str, ast.AST] | None = None) -> bool:
    """Allow HTTP clients only for declared local fixtures or literal loopback URLs."""
    url_nodes = list(call.args[:1])
    url_nodes.extend(keyword.value for keyword in call.keywords if keyword.arg in {"url", "base_url"})

    def derived_from_fixture(node: ast.AST, seen: set[str] | None = None) -> bool:
        seen = seen or set()
        if isinstance(node, ast.Name):
            if node.id in LOCAL_API_FIXTURES:
                return True
            if bindings and node.id in bindings and node.id not in seen:
                return derived_from_fixture(bindings[node.id], seen | {node.id})
            return False
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return is_loopback_url_literal(node.value)
        if isinstance(node, ast.Attribute):
            return node.attr in {"base_url", "url"} and isinstance(node.value, ast.Name) and node.value.id in LOCAL_API_SERVER_OBJECTS
        if isinstance(node, ast.JoinedStr):
            literal_text = "".join(
                item.value for item in node.values
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
            fixture_derived = any(
                isinstance(item, ast.FormattedValue) and derived_from_fixture(item.value, seen)
                for item in node.values
            )
            loopback_derived = is_loopback_url_literal(literal_text) and any(
                isinstance(item, ast.FormattedValue) and (
                    derived_from_fixture(item.value, seen)
                    or isinstance(item.value, ast.Name) and item.value.id in {"port", "server_port"}
                )
                for item in node.values
            )
            return (
                loopback_derived
                or ("http://" not in literal_text.lower() and "https://" not in literal_text.lower() and fixture_derived)
            )
        if isinstance(node, ast.BinOp):
            literal_parts = [part.value for part in ast.walk(node) if isinstance(part, ast.Constant) and isinstance(part.value, str)]
            if any(is_loopback_url_literal(item) for item in literal_parts):
                return True
            return derived_from_fixture(node.left, seen) or derived_from_fixture(node.right, seen)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "Request" and node.args:
            return derived_from_fixture(node.args[0], seen)
        return False

    return any(derived_from_fixture(node) for node in url_nodes)


def replace_pytest_args(command: list[str], *, collect_only: bool) -> list[str]:
    result = list(command)
    try:
        q_index = result.index("-q")
    except ValueError:
        q_index = -1
    if collect_only:
        if q_index >= 0:
            result[q_index] = "--collect-only"
            result.insert(q_index + 1, "-q")
        else:
            result.insert(3, "--collect-only")
    return result


def run_in_docker(
    stage: Path,
    runtime_manifest: dict[str, Any],
    command: list[str],
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    image = str(runtime_manifest["docker_image"])
    argv = [
        "docker", "run", "--rm", "--workdir", CONTAINER_WORKSPACE,
        "--mount", f"type=bind,source={stage},target={CONTAINER_WORKSPACE}",
        "--env", f"PYTHONPATH={CONTAINER_WORKSPACE}",
    ]
    for mount in runtime_manifest.get("asset_mounts_read_only", []):
        target = str(mount["target"])
        if not target.startswith(CONTAINER_WORKSPACE.rstrip("/") + "/"):
            raise ValueError(f"runtime asset mount is outside workspace: {target}")
        argv.extend(["--mount", f"type=bind,source={mount['source']},target={target},readonly"])
    setup_commands = runtime_manifest.get("container_setup_commands", [])
    if setup_commands:
        shell_commands = [shlex.join(list(item)) for item in setup_commands]
        shell_commands.append(shlex.join(command))
        argv.extend([image, "sh", "-lc", " && ".join(shell_commands)])
    else:
        argv.extend([image, *command])
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def read_pytest_outcomes(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise RuntimeError(f"pytest JSON report was not produced: {path}")
    report = read_json(path)
    outcomes: dict[str, str] = {}
    for item in report.get("tests", []):
        if "nodeid" not in item or "outcome" not in item:
            continue
        outcomes[str(item["nodeid"])] = str(item["outcome"])
    if not outcomes:
        raise RuntimeError(f"pytest JSON report contains no test outcomes: {path}")
    return outcomes


def verify_failure_baseline(
    outcomes: dict[str, str],
    *,
    known_failure_ids: set[str],
    known_test_ids: set[str],
    candidate_ids: set[str] | None = None,
) -> tuple[bool, str | None]:
    candidate_ids = candidate_ids or set()
    missing = sorted(known_test_ids - set(outcomes))
    if missing:
        return False, f"pytest JSON report is missing baseline test IDs: {missing}"
    failed = {nodeid for nodeid, outcome in outcomes.items() if outcome == "failed"}
    unexpected = sorted(failed - known_failure_ids - candidate_ids)
    if unexpected:
        return False, f"new failures beyond the C1 baseline: {unexpected}"
    failed_candidates = sorted(candidate_ids & failed)
    if failed_candidates:
        return False, f"generated candidate tests failed on the source checkpoint: {failed_candidates}"
    invalid_candidate_outcomes = sorted(
        nodeid for nodeid in candidate_ids if outcomes.get(nodeid) != "passed"
    )
    if invalid_candidate_outcomes:
        return False, f"generated candidate tests did not pass on the source checkpoint: {invalid_candidate_outcomes}"
    return True, None


def copy_runtime_stage(source: Path, destination: Path) -> None:
    ignored = {"__pycache__", ".pytest_cache", "mutants", ".venv"}

    def ignore(_: str, names: list[str]) -> set[str]:
        return {name for name in names if name in ignored or name.startswith(".coverage")}

    shutil.copytree(filesystem_path(source), filesystem_path(destination), ignore=ignore)


def collect_candidate_ids(stdout: str, relative: str) -> list[str]:
    suffix = f"/{relative}::"
    return [line.strip() for line in stdout.splitlines() if line.strip().startswith("base_tests/") and suffix in line]


def read_review(path: Path, candidate_paths: set[str]) -> dict[str, dict[str, Any]]:
    raw = read_json(path)
    if set(raw) != {"reviews"} or not isinstance(raw["reviews"], list):
        raise ValueError("correctness review must contain only a reviews array")
    reviews: dict[str, dict[str, Any]] = {}
    for item in raw["reviews"]:
        if not isinstance(item, dict) or set(item) != {"candidate_path", "verdict", "rationale", "reviewer"}:
            raise ValueError("each correctness review needs candidate_path, verdict, rationale, reviewer")
        candidate_path = item["candidate_path"]
        if candidate_path not in candidate_paths or candidate_path in reviews:
            raise ValueError(f"review references unknown or duplicate candidate: {candidate_path}")
        if item["verdict"] not in {"correct", "incorrect", "uncertain"}:
            raise ValueError(f"invalid correctness verdict for {candidate_path}")
        if not all(isinstance(item[key], str) and item[key].strip() for key in ("rationale", "reviewer")):
            raise ValueError(f"review for {candidate_path} lacks rationale or reviewer")
        reviews[candidate_path] = item
    if set(reviews) != candidate_paths:
        raise ValueError("every candidate requires a correctness review before freezing")
    return reviews


def hash_tree(root: Path) -> str:
    items = []
    root_path = filesystem_path(root)
    for current, directories, files in os.walk(root_path):
        directories.sort()
        for name in sorted(files):
            path = os.path.join(current, name)
            relative = os.path.relpath(path, root_path).replace(os.sep, "/")
            items.append({"path": relative, "sha256": sha256_file(path)})
    items.sort(key=lambda item: item["path"])
    payload = json.dumps(items, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(payload)


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation-dir", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--correctness-review", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--target-repeat-runs",
        type=int,
        default=1,
        help="Fresh mutant-application executions required for a stable target kill.",
    )
    parser.add_argument(
        "--paired-target-mutant",
        help="For a Vanilla candidate in a controlled pair, evaluate it against this preselected target mutant.",
    )
    parser.add_argument(
        "--paired-target-record-set",
        type=Path,
        help="Frozen mutmut survivor JSONL used to apply the exact paired mutant diff after generation.",
    )
    parser.add_argument("--paired-target-record-set-sha256")
    parser.add_argument(
        "--paired-direct-diff-validation",
        action="store_true",
        help="Use the exact frozen mutant diff and repeat only the candidate node; the official C1 suite is checked separately.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--mutmut-timeout-seconds", type=int, default=900)
    parser.add_argument(
        "--baseline-audit",
        type=Path,
        help="Repeated source-checkpoint base-suite audit; stable pre-existing failures are tracked separately",
    )
    args = parser.parse_args()

    generation_dir = args.generation_dir.resolve(strict=True)
    runtime_dir = args.runtime_dir.resolve(strict=True)
    allowed_results = (REPO_ROOT / "artifacts" / "results").resolve(strict=True)
    allowed_runtime = (REPO_ROOT / "tmp" / "checkpoint-runtime").resolve(strict=True)
    if not generation_dir.is_relative_to(allowed_results):
        raise ValueError(f"generation directory must remain under {allowed_results}")
    if not runtime_dir.is_relative_to(allowed_runtime):
        raise ValueError(f"runtime directory must remain under {allowed_runtime}")
    if args.repeats < 1 or args.target_repeat_runs < 1:
        raise ValueError("repeat counts must be positive")
    if args.paired_direct_diff_validation and (args.paired_target_record_set is None or not args.paired_target_record_set_sha256):
        raise ValueError("paired direct-diff validation requires a hashed target survivor record set")
    if (generation_dir / "frozen_test_suite.json").exists():
        raise ValueError("suite is already frozen; refusing to modify it")

    generation_run_path = generation_dir / "generation_run.json"
    generation_run = read_json(generation_run_path)
    input_manifest = read_json(generation_dir / "input_manifest.json")
    runtime_manifest = read_json(runtime_dir / "runtime_manifest.json")
    budget_resource = input_manifest.get("budget_config")
    if isinstance(budget_resource, dict) and isinstance(budget_resource.get("path"), str):
        relative_budget_path = PurePosixPath(budget_resource["path"])
        if relative_budget_path.is_absolute() or ".." in relative_budget_path.parts:
            raise ValueError("generation budget configuration path is unsafe")
        budget_path = REPO_ROOT.joinpath(*relative_budget_path.parts).resolve(strict=True)
        if not budget_path.is_relative_to(REPO_ROOT.resolve(strict=True)):
            raise ValueError("generation budget configuration escapes the repository")
        if sha256_file(budget_path) != budget_resource.get("sha256"):
            raise ValueError("generation budget configuration changed after the generation call")
        budget_config = read_json(budget_path)
        if budget_config.get("budget_id") != input_manifest.get("budget_id"):
            raise ValueError("generation input manifest budget ID does not match its budget configuration")
    else:
        # Backward compatibility for the initial pilot bundles before the budget path was hashed.
        budget_config = read_json(REPO_ROOT / "config" / "generation_budget.json")
    if budget_config.get("budget_id") != generation_run.get("budget_id"):
        raise ValueError("generation run budget ID differs from the frozen budget configuration")
    method = generation_run["method"]
    if method not in {"vanilla_llm", "mutation_guided"}:
        raise ValueError(f"unsupported generation method: {method}")
    pair_metadata_path = generation_dir / "pairwise_condition.json"
    pair_metadata: dict[str, Any] | None = None
    if pair_metadata_path.is_file():
        pair_metadata = read_json(pair_metadata_path)
        if pair_metadata.get("method") != method:
            raise ValueError("paired condition metadata does not match the generation method")
        if pair_metadata.get("task_id") != generation_run["task_id"] or pair_metadata.get("mutant_id") is None:
            raise ValueError("paired condition metadata does not match this C1 generation run")
    if args.paired_target_mutant is not None:
        if method != "vanilla_llm":
            raise ValueError("--paired-target-mutant is valid only for a Vanilla generation run")
        if pair_metadata is None or pair_metadata.get("mutant_id") != args.paired_target_mutant:
            raise ValueError("Vanilla target mutant does not match the frozen paired-condition metadata")
    elif method == "vanilla_llm" and pair_metadata is not None:
        raise ValueError("controlled paired Vanilla validation requires --paired-target-mutant")
    elif method == "mutation_guided" and pair_metadata is not None:
        if input_manifest.get("guidance_mode") != "target_mutant_id_and_diff_only":
            raise ValueError("controlled Guided generation did not use the minimal one-target diff prompt")
        if input_manifest.get("eligible_mutant_ids") != [pair_metadata["mutant_id"]]:
            raise ValueError("controlled Guided input does not contain exactly its paired target mutant")
    if args.paired_direct_diff_validation and pair_metadata is None:
        raise ValueError("paired direct-diff validation requires frozen paired-condition metadata")
    if runtime_manifest["task_id"] != generation_run["task_id"] or runtime_manifest["checkpoint_id"] != generation_run["checkpoint_id"]:
        raise ValueError("runtime stage does not match the generation checkpoint")
    if runtime_manifest["base_tests_sha256"] != input_manifest["base_tests_sha256"]:
        raise ValueError("runtime stage base-test hash differs from the generator input")
    baseline_audit_path: Path | None = None
    baseline_record: dict[str, Any] | None = None
    stable_pass_ids: list[str] = []
    stable_fail_ids: list[str] = []
    if args.baseline_audit is not None:
        baseline_audit_path = args.baseline_audit.resolve(strict=True)
        allowed_results = (REPO_ROOT / "artifacts" / "results").resolve(strict=True)
        if not baseline_audit_path.is_relative_to(allowed_results):
            raise ValueError(f"baseline audit must remain under {allowed_results}")
        audit = read_json(baseline_audit_path)
        matches = [
            record for record in audit.get("records", [])
            if record.get("task_id") == generation_run["task_id"]
            and record.get("checkpoint_id") == generation_run["checkpoint_id"]
            and record.get("base_tests_sha256") == input_manifest["base_tests_sha256"]
        ]
        if len(matches) != 1:
            raise ValueError("baseline audit must contain one matching task/checkpoint/base-suite record")
        baseline_record = matches[0]
        if baseline_record.get("status") not in {"stable_pass", "stable_with_failures"}:
            raise ValueError(f"source checkpoint baseline is not stable: {baseline_record.get('status')}")
        if baseline_record.get("unstable_test_ids"):
            raise ValueError("source checkpoint base suite has unstable tests; freeze no generated tests")
        stable_pass_ids = list(baseline_record.get("stable_pass_test_ids", []))
        stable_fail_ids = list(baseline_record.get("stable_fail_test_ids", []))
    package_root = Path(input_manifest["package_root"]).resolve(strict=True)
    package = read_json(package_root / "package.json")
    if package["base_tests_sha256"] != input_manifest["base_tests_sha256"]:
        raise ValueError("source package base-test hash changed after generation")
    task_profile = runtime_manifest.get("task_runtime_profile", {})
    if not task_profile:
        runtime_config = read_json(REPO_ROOT / "config" / "checkpoint_runtime.json")
        task_profile = runtime_config.get("tasks", {}).get(generation_run["task_id"], {})
    input_hashes = {item["path"]: item["sha256"] for item in input_manifest["input_files"]}
    for source_relative in task_profile.get("mutmut", {}).get("source_paths", []):
        package_relative = f"{package['workspace_path']}/{source_relative}"
        expected_source_hash = input_hashes.get(package_relative)
        if expected_source_hash is None:
            raise ValueError(f"source hash missing from generation inputs: {package_relative}")
        runtime_source = runtime_dir / source_relative
        if not runtime_source.is_file() or sha256_file(runtime_source) != expected_source_hash:
            raise ValueError(f"runtime source differs from generated-test input: {source_relative}")
    if generation_run.get("output_budget_exceeded"):
        raise ValueError("generation exceeded the declared output-token budget; refusing to freeze")
    if args.repeats != budget_config["validation"]["repeat_runs"]:
        raise ValueError("repeat count must match the frozen Gate 1 budget configuration")
    if args.target_repeat_runs != budget_config["validation"].get("target_repeat_runs", 1):
        raise ValueError("target repeat count must match the frozen generation budget configuration")

    eligible_mutants: list[dict[str, Any]] = []
    eligible_mutant_ids: set[str] = set()
    if method == "mutation_guided":
        mutant_path = Path(input_manifest["mutation_record_set_path"]).resolve(strict=True)
        allowed_results = (REPO_ROOT / "artifacts" / "results").resolve(strict=True)
        if not mutant_path.is_relative_to(allowed_results):
            raise ValueError(f"mutant records must remain under {allowed_results}")
        if sha256_file(mutant_path) != input_manifest["mutation_record_set_sha256"]:
            raise ValueError("mutant record set changed after generation")
        summary_path = Path(input_manifest["mutation_summary_path"]).resolve(strict=True)
        if sha256_file(summary_path) != input_manifest["mutation_summary_sha256"]:
            raise ValueError("mutation summary changed after generation")
        eligible_path = generation_dir / input_manifest["eligible_mutants_path"]
        if sha256_file(eligible_path) != input_manifest["eligible_mutants_sha256"]:
            raise ValueError("eligible-mutant prompt package changed after generation")
        eligible_mutants = json.loads(eligible_path.read_text(encoding="utf-8"))
        if not isinstance(eligible_mutants, list) or len(eligible_mutants) != input_manifest["eligible_mutant_count"]:
            raise ValueError("eligible-mutant package shape/count does not match input manifest")
        eligible_mutant_ids = {item["mutant_id"] for item in eligible_mutants}
        if len(eligible_mutant_ids) != len(eligible_mutants):
            raise ValueError("eligible-mutant package contains duplicate IDs")
        if set(generation_run.get("guided_mutant_ids", [])) != eligible_mutant_ids:
            raise ValueError("generation record guided-mutant IDs differ from the frozen input set")
        triage_relative = input_manifest.get("mutant_triage_path")
        triage_hash = input_manifest.get("mutant_triage_sha256")
        if (triage_relative is None) != (triage_hash is None):
            raise ValueError("mutant-triage path and hash must be recorded together")
        if triage_relative is not None:
            triage_path = (generation_dir / triage_relative).resolve(strict=True)
            if not triage_path.is_relative_to(generation_dir.resolve(strict=True)):
                raise ValueError("mutant-triage file escapes the generation directory")
            if sha256_file(triage_path) != triage_hash:
                raise ValueError("mutant-triage record changed after generation")
            triage = read_json(triage_path)
            if triage.get("schema_version") not in {"rq1-mutant-triage/v1", "rq1-mutant-triage/v2"}:
                raise ValueError("unsupported mutant-triage schema")
            if triage.get("mutation_record_set_sha256") != input_manifest["mutation_record_set_sha256"]:
                raise ValueError("mutant triage does not match the archived raw mutation set")
            raw_survivor_ids = {
                record["mutant_id"]
                for line in mutant_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
                for record in [json.loads(line)]
                if record.get("normalized_status") == "survived"
            }
            decisions = triage.get("decisions")
            if not isinstance(decisions, list):
                raise ValueError("mutant-triage decisions must be an array")
            decision_by_id = {
                decision.get("mutant_id"): decision
                for decision in decisions
                if isinstance(decision, dict)
            }
            if len(decision_by_id) != len(decisions) or set(decision_by_id) != raw_survivor_ids:
                raise ValueError("mutant triage must classify every raw survivor exactly once")
            triaged_eligible = {
                mutant_id
                for mutant_id, decision in decision_by_id.items()
                if decision.get("status") == "eligible"
                and decision.get("selected_for_generation", True) is True
            }
            invalid_selection = [
                mutant_id
                for mutant_id, decision in decision_by_id.items()
                if not isinstance(decision.get("selected_for_generation", decision.get("status") == "eligible"), bool)
                or (
                    decision.get("selected_for_generation", decision.get("status") == "eligible")
                    and decision.get("status") != "eligible"
                )
                or (
                    decision.get("status") == "eligible"
                    and decision.get("selected_for_generation", True) is False
                    and not str(decision.get("selection_rationale", "")).strip()
                )
            ]
            if invalid_selection:
                raise ValueError(f"mutant triage has invalid selection decisions: {sorted(invalid_selection)}")
            if triaged_eligible != eligible_mutant_ids:
                raise ValueError("selected eligible-mutant prompt package differs from the triage decisions")
            expected_not_selected = sorted(
                mutant_id
                for mutant_id, decision in decision_by_id.items()
                if decision.get("status") == "eligible"
                and decision.get("selected_for_generation", True) is False
            )
            if input_manifest.get("eligible_not_selected_mutant_ids", []) != expected_not_selected:
                raise ValueError("unselected eligible-mutant IDs differ from the triage decisions")

    paired_target_records: dict[str, dict[str, Any]] = {}
    if args.paired_direct_diff_validation:
        target_record_path = args.paired_target_record_set.resolve(strict=True)
        allowed_results = (REPO_ROOT / "artifacts" / "results").resolve(strict=True)
        if not target_record_path.is_relative_to(allowed_results):
            raise ValueError("paired target survivor records must remain under artifacts/results")
        if sha256_file(target_record_path) != args.paired_target_record_set_sha256:
            raise ValueError("paired target survivor records changed after mutant selection")
        for line in target_record_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            mutant_record = json.loads(line)
            mutant_id = mutant_record.get("mutant_id")
            if mutant_id == pair_metadata["mutant_id"]:
                if mutant_record.get("normalized_status") != "survived":
                    raise ValueError("paired target mutant is not a prevalidated survivor")
                paired_target_records[mutant_id] = mutant_record
        if set(paired_target_records) != {pair_metadata["mutant_id"]}:
            raise ValueError("paired target mutant is missing or duplicated in the frozen survivor records")
        if method == "mutation_guided":
            eligible_target = next((item for item in eligible_mutants if item.get("mutant_id") == pair_metadata["mutant_id"]), None)
            if eligible_target is None or eligible_target.get("diff") != paired_target_records[pair_metadata["mutant_id"]].get("diff"):
                raise ValueError("applied target diff differs from the exact diff supplied to Guided generation")

    response = read_json(generation_dir / "candidate_response.json")
    candidates = validate_candidate_response(
        response,
        int(generation_run["budget"]["max_candidates"]),
        method=method,
        eligible_mutant_ids=eligible_mutant_ids,
    )
    candidate_paths = {candidate["relative_path"] for candidate in candidates}
    reviews = read_review(args.correctness_review.resolve(strict=True), candidate_paths)
    base_command = list(runtime_manifest["test_command_argv"])
    if baseline_record is not None:
        base_command.extend(["--json-report", "--json-report-file=/workspace/rq1_validation_report.json"])
    collect_command = replace_pytest_args(base_command, collect_only=True)
    # Keep candidate workspaces shallow. The mutmut runner creates a sibling
    # mutant workspace with the same nested base-test paths, which can exceed
    # MAX_PATH on Windows if staged beneath tmp/checkpoint-runtime/<task>.
    stage_parent = REPO_ROOT / "tmp" / "validation"
    stage_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="vanilla-base-", dir=stage_parent, ignore_cleanup_errors=True
    ) as base_temp:
        baseline_stage = Path(base_temp) / "workspace"
        copy_runtime_stage(runtime_dir, baseline_stage)
        baseline = run_in_docker(baseline_stage, runtime_manifest, base_command, timeout=args.timeout_seconds)
        (generation_dir / "base-suite.stdout.log").write_text(baseline.stdout, encoding="utf-8")
        (generation_dir / "base-suite.stderr.log").write_text(baseline.stderr, encoding="utf-8")
        if baseline_record is None:
            if baseline.returncode != 0:
                raise RuntimeError("unaltered base suite failed in the validation runtime; no candidates frozen")
        else:
            baseline_outcomes = read_pytest_outcomes(baseline_stage / "rq1_validation_report.json")
            expected_ids = set(stable_pass_ids) | set(stable_fail_ids)
            baseline_ok, baseline_error = verify_failure_baseline(
                baseline_outcomes,
                known_failure_ids=set(stable_fail_ids),
                known_test_ids=expected_ids,
            )
            if not baseline_ok:
                raise RuntimeError(f"validation runtime does not match the repeated C1 baseline: {baseline_error}")

    validation_log = generation_dir / "validation.jsonl"
    records: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    candidates_root = generation_dir / "candidates"
    candidates_root.mkdir(exist_ok=True)
    frozen_root = generation_dir / "frozen_suite"
    frozen_base = frozen_root / "base_tests"
    frozen_generated_name = "generated_tests"
    frozen_generated = frozen_root / frozen_generated_name
    if len(str(frozen_generated.resolve())) >= 240:
        # Keep enough headroom for candidate filenames on Windows MAX_PATH hosts.
        frozen_generated_name = "gen"
        frozen_generated = frozen_root / frozen_generated_name
    if len(str(frozen_generated.resolve())) >= 248:
        raise ValueError("frozen generated-test directory exceeds the supported Windows path limit")
    frozen_root.mkdir()
    shutil.copytree(
        filesystem_path(package_root / package["base_tests_path"]),
        filesystem_path(frozen_base),
    )
    frozen_generated.mkdir()
    guided_validation_seconds = 0.0

    for index, candidate in enumerate(candidates):
        relative = candidate["relative_path"]
        content = candidate["content"]
        candidate_dir = candidates_root / f"candidate_{index + 1:02d}"
        candidate_dir.mkdir()
        evaluation_target_ids = list(candidate["target_mutant_ids"])
        if method == "vanilla_llm" and args.paired_target_mutant is not None:
            evaluation_target_ids = [args.paired_target_mutant]
        write_text_file(candidate_dir / relative, content)
        rejection = static_candidate_checks(relative, content)
        review = reviews[relative]
        if rejection is None and method == "mutation_guided" and not candidate["target_mutant_ids"]:
            rejection = "Mutation-Guided candidate declares no target mutants"
        if rejection is None and review["verdict"] != "correct":
            rejection = f"specification correctness review: {review['verdict']}"
        record: dict[str, Any] = {
            "candidate_path": relative,
            "content_sha256": sha256_bytes(content.encode("utf-8")),
            "specification_basis": candidate["specification_basis"],
            "correctness_review": review,
            "target_mutant_ids": candidate["target_mutant_ids"],
            "evaluation_target_mutant_ids": evaluation_target_ids,
            "static_check": "passed" if rejection is None else rejection,
            "collection_ids": [],
            "runs": [],
            "target_mutant_results": [],
            "target_validation_suite_sha256": None,
            "validation_mode": "paired_direct_mutant_diff" if args.paired_direct_diff_validation else "full_suite_plus_mutmut",
            "status": "rejected" if rejection else "pending",
            "rejection_reason": rejection,
        }
        if rejection is None:
            with tempfile.TemporaryDirectory(
                prefix=f"vanilla-candidate-{index + 1:02d}-",
                dir=stage_parent,
                ignore_cleanup_errors=True,
            ) as candidate_temp:
                candidate_stage = Path(candidate_temp) / "workspace"
                copy_runtime_stage(runtime_dir, candidate_stage)
                destination = candidate_stage / package["base_tests_path"] / relative
                if destination.exists():
                    rejection = f"candidate path collides with official base test: {relative}"
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(content, encoding="utf-8")
                    if evaluation_target_ids:
                        candidate_runtime_manifest_path = candidate_stage / "runtime_manifest.json"
                        candidate_runtime_manifest = read_json(candidate_runtime_manifest_path)
                        candidate_suite_hash = hash_tree(candidate_stage / package["base_tests_path"])
                        candidate_runtime_manifest["validation_suite_sha256"] = candidate_suite_hash
                        candidate_runtime_manifest["validation_test_path"] = f"{package['base_tests_path']}/{relative}"
                        candidate_runtime_manifest["validation_test_sha256"] = sha256_file(destination)
                        write_json(candidate_runtime_manifest_path, candidate_runtime_manifest)
                        record["target_validation_suite_sha256"] = candidate_suite_hash
                    collected = run_in_docker(candidate_stage, runtime_manifest, collect_command, timeout=args.timeout_seconds)
                    write_text_file(candidate_dir / "collect.stdout.log", collected.stdout)
                    write_text_file(candidate_dir / "collect.stderr.log", collected.stderr)
                    ids = collect_candidate_ids(collected.stdout, relative)
                    record["collection_ids"] = ids
                    if collected.returncode != 0:
                        rejection = f"pytest collection failed with exit code {collected.returncode}"
                    elif not ids:
                        rejection = "candidate collected no stable pytest items"
                    else:
                        if evaluation_target_ids:
                            candidate_runtime_manifest_path = candidate_stage / "runtime_manifest.json"
                            candidate_runtime_manifest = read_json(candidate_runtime_manifest_path)
                            candidate_runtime_manifest["mutmut_test_selection"] = stable_pass_ids + ids
                            write_json(candidate_runtime_manifest_path, candidate_runtime_manifest)
                        candidate_only_command = list(runtime_manifest["test_command_argv"])
                        candidate_only_command[-1:] = ids
                        if baseline_record is not None:
                            candidate_only_command.extend(["--json-report", "--json-report-file=/workspace/rq1_validation_report.json"])
                        for repetition in range(1, args.repeats + 1):
                            report_path = candidate_stage / "rq1_validation_report.json"
                            report_path.unlink(missing_ok=True)
                            execution_command = candidate_only_command if args.paired_direct_diff_validation else base_command
                            run = run_in_docker(candidate_stage, runtime_manifest, execution_command, timeout=args.timeout_seconds)
                            write_text_file(candidate_dir / f"run_{repetition}.stdout.log", run.stdout)
                            write_text_file(candidate_dir / f"run_{repetition}.stderr.log", run.stderr)
                            run_record: dict[str, Any] = {
                                "repetition": repetition,
                                "exit_code": run.returncode,
                                "passed": run.returncode == 0 if baseline_record is None else None,
                            }
                            if args.paired_direct_diff_validation:
                                try:
                                    outcomes = read_pytest_outcomes(report_path) if baseline_record is not None else {}
                                except (OSError, json.JSONDecodeError, RuntimeError) as error:
                                    outcomes = {}
                                    run_record["report_error"] = str(error)
                                run_record["candidate_outcomes"] = {test_id: outcomes.get(test_id) for test_id in ids}
                                candidate_pass = (
                                    run.returncode == 0
                                    and len(ids) > 0
                                    and all(outcomes.get(test_id) == "passed" for test_id in ids)
                                )
                                run_record["passed"] = candidate_pass
                                run_record["new_failure_ids"] = sorted(test_id for test_id in ids if outcomes.get(test_id) == "failed")
                                record["runs"].append(run_record)
                                if not candidate_pass:
                                    rejection = f"candidate did not pass on the original C1 implementation in repetition {repetition}"
                                    break
                            elif baseline_record is None:
                                record["runs"].append(run_record)
                                if run.returncode != 0:
                                    rejection = f"execution repetition {repetition} failed with exit code {run.returncode}"
                                    break
                            else:
                                run_outcomes = read_pytest_outcomes(report_path)
                                candidate_nodeids = set(ids)
                                baseline_ok, baseline_error = verify_failure_baseline(
                                    run_outcomes,
                                    known_failure_ids=set(stable_fail_ids),
                                    known_test_ids=set(stable_pass_ids) | set(stable_fail_ids),
                                    candidate_ids=candidate_nodeids,
                                )
                                run_record["passed"] = baseline_ok
                                run_record["new_failure_ids"] = sorted(
                                    nodeid for nodeid, outcome in run_outcomes.items()
                                    if outcome == "failed" and nodeid not in stable_fail_ids
                                )
                                record["runs"].append(run_record)
                                if not baseline_ok:
                                    rejection = f"execution repetition {repetition} failed baseline-aware validation: {baseline_error}"
                                    break
                    if rejection is None and evaluation_target_ids:
                        if args.paired_direct_diff_validation:
                            target_results, elapsed, target_error = verify_paired_targets_from_diffs(
                                candidate_stage,
                                evaluation_target_ids,
                                paired_target_records,
                                candidate_dir,
                                candidate_test_ids=ids,
                                runtime_manifest=runtime_manifest,
                                timeout=args.timeout_seconds,
                                repeat_runs=args.target_repeat_runs,
                            )
                        else:
                            target_results, elapsed, target_error = verify_guided_targets(
                                candidate_stage,
                                evaluation_target_ids,
                                candidate_dir,
                                candidate_test_ids=ids,
                                timeout=args.mutmut_timeout_seconds,
                                repeat_runs=args.target_repeat_runs,
                            )
                        guided_validation_seconds += elapsed
                        record["target_mutant_results"] = target_results
                        if target_error:
                            rejection = target_error
        if rejection:
            record["status"] = "rejected"
            record["rejection_reason"] = rejection
        else:
            record["status"] = "retained"
            record["rejection_reason"] = None
            shutil.copy2(filesystem_path(candidate_dir / relative), filesystem_path(frozen_generated / relative))
            accepted.append(candidate)
        records.append(record)

    with validation_log.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    suite_hash = hash_tree(frozen_root)
    frozen_at = datetime.now(timezone.utc).isoformat()
    frozen_manifest = {
        "schema_version": "1.0",
        "task_id": package["task_id"],
        "trajectory_id": package["trajectory_id"],
        "source_checkpoint": package["checkpoint_id"],
        "method": method,
        "suite_path": "frozen_suite",
        "base_suite_path": "frozen_suite/base_tests",
        "generated_test_paths": [f"frozen_suite/{frozen_generated_name}/{item['relative_path']}" for item in accepted],
        "validation_log_path": "validation.jsonl",
        "suite_sha256": suite_hash,
        "model_config_id": generation_run["model_config_id"],
        "budget_id": generation_run["budget_id"],
        "frozen_at": frozen_at,
        "metadata": {
            "generation_run_path": "generation_run.json",
            "generation_assessment": generation_run.get("generation_assessment"),
            "input_manifest_path": "input_manifest.json",
            "study_budget_config": input_manifest.get("study_budget_config"),
            "prompt_archive_path": "prompt_archive.md",
            "correctness_review_path": str(args.correctness_review.resolve(strict=True).relative_to(generation_dir)),
            "repeat_runs": args.repeats,
            "target_mutant_repeat_runs": args.target_repeat_runs,
            "validation_mode": "paired_direct_mutant_diff" if args.paired_direct_diff_validation else "full_suite_plus_mutmut",
            "paired_target_mutant": args.paired_target_mutant,
            "pairwise_condition_path": "pairwise_condition.json" if pair_metadata is not None else None,
            "baseline_audit_path": str(baseline_audit_path) if baseline_audit_path else None,
            "baseline_audit_sha256": sha256_file(baseline_audit_path) if baseline_audit_path else None,
            "stable_source_checkpoint_pass_count": len(stable_pass_ids),
            "stable_source_checkpoint_fail_count": len(stable_fail_ids),
            "source_package_sha256": input_manifest["input_package_sha256"],
            "base_tests_sha256": package["base_tests_sha256"],
            "runtime_manifest_path": str(runtime_dir / "runtime_manifest.json"),
            "trajectory_provenance_status": budget_config.get("data_policy", {}).get(
                "trajectory_provenance", "not_recorded_in_budget_config"
            ),
            "excluded_test_policy": budget_config.get("data_policy", {}).get(
                "file_backup_errors", "sanitized checkpoint package isolation manifest"
            ),
            "candidate_files_emitted": len(candidates),
            "candidate_files_retained": len(accepted),
            "eligible_mutant_count": len(eligible_mutants) if method == "mutation_guided" else 0,
            "mutation_record_set_sha256": input_manifest.get("mutation_record_set_sha256"),
            "mutant_triage_path": input_manifest.get("mutant_triage_path"),
            "mutant_triage_sha256": input_manifest.get("mutant_triage_sha256"),
            "mutant_triage_status_counts": input_manifest.get("mutant_triage_status_counts"),
        },
    }
    write_json(generation_dir / "frozen_test_suite.json", frozen_manifest)
    generation_run["retained_count"] = len(accepted)
    generation_run["validation_log_path"] = "validation.jsonl"
    if method == "mutation_guided":
        generation_run["actual_usage"]["mutation_analysis_wall_seconds"] = guided_validation_seconds
    write_json(generation_run_path, generation_run)
    print(f"Candidates emitted: {len(candidates)}")
    print(f"Candidates retained: {len(accepted)}")
    print(f"Frozen suite SHA-256: {suite_hash}")
    print(f"Frozen suite manifest: {generation_dir / 'frozen_test_suite.json'}")
    print(f"Validation log: {validation_log}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

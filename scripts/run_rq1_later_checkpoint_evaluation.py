"""Run frozen C1 Original/Vanilla/Guided suites on later snapshots and extract candidates."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from validate_and_freeze_tests import filesystem_path, hash_tree as frozen_suite_hash
from validate_checkpoint_package import sha256_tree as package_tree_hash

ROOT = Path(__file__).resolve().parents[1]
RESULT_ROOT = ROOT / "artifacts" / "results" / "rq1_full_36x1"
V2_RESULT_ROOT = ROOT / "artifacts" / "results" / "rq1_recovery_v2"
RESULTS_ROOT = ROOT / "artifacts" / "results"
PACKAGE_ROOT = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
UNIT_PATH = ROOT / "config" / "rq1_full_36x1_candidate_units.json"
BASELINE_PATH = RESULT_ROOT / "c1_base_audit_current_sanitized_20261116" / "c1_base_suite_audit.json"
PREPARE_SCRIPT = ROOT / "scripts" / "prepare_checkpoint_runtime.py"
# Canonical physical Vanilla cohort: all 36 manifests and the matching audit are
# present locally.  The former v1/49-test identifier was never materialized.
VANILLA_AUDIT_PATH = RESULT_ROOT / "c1_vanilla_cohort_audit_formal_v1_20261117.json"
DEFAULT_VANILLA = "vanilla_formal_v5_20261116"
DEFAULT_GUIDED = "mutation_guided_formal_v1_baseline_linked_20261117"
CONTAINER_WORKSPACE = "/workspace"
REPORT_PATH = "/workspace/rq1-evaluation-report.json"
METHODS = ("original", "vanilla_llm", "mutation_guided")
REPRODUCIBILITY_REPEATS = 3


def read_json(path: Path) -> Any:
    with open(filesystem_path(path), "r", encoding="utf-8") as handle:
        return json.load(handle)


_TARGET_SYNTAX_ERROR = re.compile(r"\b(?:SyntaxError|IndentationError|TabError)\b")
_TARGET_WORKSPACE_FILE = re.compile(r"File\s+['\"]\/workspace\/(?!base_tests\/)[^'\"]+\.py['\"]")


def is_target_syntax_regression(test: dict[str, Any]) -> bool:
    """Recognize a test setup error caused by invalid Python in the checkpoint code."""
    detail = str(test.get("longrepr") or "")
    return bool(_TARGET_SYNTAX_ERROR.search(detail) and _TARGET_WORKSPACE_FILE.search(detail))


def pytest_test_detail(item: dict[str, Any]) -> str | None:
    detail = item.get("longrepr")
    if detail:
        return str(detail)
    phase_messages = [
        str(item[phase].get("crash", {}).get("message"))
        for phase in ("setup", "call", "teardown")
        if isinstance(item.get(phase), dict)
        and item[phase].get("crash", {}).get("message")
    ]
    return "\n".join(phase_messages) if phase_messages else None


def vanilla_cohort_suffix(task: str) -> str:
    audit = read_json(VANILLA_AUDIT_PATH)
    if audit.get("status") != "complete":
        raise ValueError("the sanitized 36-task Vanilla cohort audit is incomplete")
    if audit.get("format") == "rq1-vanilla-formal-v1-cohort-audit/v1":
        matches = [row for row in audit.get("tasks", []) if row.get("task_id") == task and row.get("status") == "frozen"]
        if len(matches) != 1:
            raise ValueError(f"{task}: no unique audited formal Vanilla suite")
        return DEFAULT_VANILLA
    matches = [row for row in audit.get("records", []) if row.get("task_id") == task and row.get("status") == "complete"]
    if len(matches) != 1:
        raise ValueError(f"{task}: no unique audited Vanilla suite directory")
    return str(matches[0]["cohort_directory"])


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def hash_tree(root: Path) -> str:
    """Use the checkpoint-package tree hash for suite overlays and base-test checks."""
    return package_tree_hash(root)


def snapshot_files(root: Path) -> dict[str, bytes]:
    ignored = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    files: dict[str, bytes] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root)
        if any(part in ignored for part in relative.parts) or path.suffix in {".pyc", ".pyo"}:
            continue
        files[relative.as_posix()] = path.read_bytes()
    return files


def build_pair_evidence(
    task: str,
    checkpoint: str,
    source_package_root: Path,
    target_package_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    source_package = read_json(source_package_root / "package.json")
    target_package = read_json(target_package_root / "package.json")
    source_workspace = package_file(source_package, source_package_root, "workspace_path")
    target_workspace = package_file(target_package, target_package_root, "workspace_path")
    source_spec = package_file(source_package, source_package_root, "specification_path")
    target_spec = package_file(target_package, target_package_root, "specification_path")

    evidence_dir = output_root / "evidence" / task / checkpoint
    if evidence_dir.exists():
        prior = read_json(evidence_dir / "evidence_manifest.json")
        if (
            prior.get("task_id") != task
            or prior.get("target_checkpoint") != checkpoint
            or prior.get("source_package_sha256") != sha256_file(source_package_root / "package.json")
            or prior.get("target_package_sha256") != sha256_file(target_package_root / "package.json")
        ):
            raise ValueError(f"existing adjudication evidence does not match {task}/{checkpoint}")
        return {
            "evidence_directory": str(evidence_dir.relative_to(output_root)),
            "source_specification_path": str((evidence_dir / "source_specification.md").relative_to(output_root)),
            "target_specification_path": str((evidence_dir / "target_specification.md").relative_to(output_root)),
            "workspace_diff_path": str((evidence_dir / "workspace_diff.patch").relative_to(output_root)),
            "evidence_manifest_path": str((evidence_dir / "evidence_manifest.json").relative_to(output_root)),
            "source_agent_run_manifest_path": str((evidence_dir / "source_agent_run_manifest.json").relative_to(output_root))
            if (evidence_dir / "source_agent_run_manifest.json").is_file() else None,
            "target_agent_run_manifest_path": str((evidence_dir / "target_agent_run_manifest.json").relative_to(output_root))
            if (evidence_dir / "target_agent_run_manifest.json").is_file() else None,
        }
    evidence_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(source_spec, evidence_dir / "source_specification.md")
    shutil.copy2(target_spec, evidence_dir / "target_specification.md")
    agent_manifest_files: dict[str, Path] = {}
    for package_root, package, label in (
        (source_package_root, source_package, "source"),
        (target_package_root, target_package, "target"),
    ):
        manifest_relative = package.get("agent_run_manifest_path")
        if isinstance(manifest_relative, str):
            manifest = package_file(package, package_root, "agent_run_manifest_path")
            copied_manifest = evidence_dir / f"{label}_agent_run_manifest.json"
            shutil.copy2(manifest, copied_manifest)
            agent_manifest_files[label] = copied_manifest

    source_files = snapshot_files(source_workspace)
    target_files = snapshot_files(target_workspace)
    diff_lines: list[str] = []
    for relative in sorted(set(source_files) | set(target_files)):
        before = source_files.get(relative)
        after = target_files.get(relative)
        if before == after:
            continue
        if (before is not None and len(before) > 1_000_000) or (after is not None and len(after) > 1_000_000):
            diff_lines.append(f"Binary or oversized file differs: {relative}\n")
            continue
        try:
            before_text = before.decode("utf-8").splitlines(keepends=True) if before is not None else []
            after_text = after.decode("utf-8").splitlines(keepends=True) if after is not None else []
        except UnicodeDecodeError:
            diff_lines.append(f"Binary file differs: {relative}\n")
            continue
        diff_lines.extend(difflib.unified_diff(
            before_text,
            after_text,
            fromfile=f"C1/{relative}" if before is not None else "/dev/null",
            tofile=f"{checkpoint}/{relative}" if after is not None else "/dev/null",
        ))
    (evidence_dir / "workspace_diff.patch").write_text("".join(diff_lines), encoding="utf-8")
    shutil.copy2(source_package_root / "package.json", evidence_dir / "source_package.json")
    shutil.copy2(target_package_root / "package.json", evidence_dir / "target_package.json")
    manifest = {
        "task_id": task,
        "source_checkpoint": "C1",
        "target_checkpoint": checkpoint,
        "source_package_sha256": sha256_file(source_package_root / "package.json"),
        "target_package_sha256": sha256_file(target_package_root / "package.json"),
        "source_specification_sha256": sha256_file(evidence_dir / "source_specification.md"),
        "target_specification_sha256": sha256_file(evidence_dir / "target_specification.md"),
        "workspace_diff_sha256": sha256_file(evidence_dir / "workspace_diff.patch"),
        "source_agent_run_manifest_sha256": sha256_file(agent_manifest_files["source"]) if "source" in agent_manifest_files else None,
        "target_agent_run_manifest_sha256": sha256_file(agent_manifest_files["target"]) if "target" in agent_manifest_files else None,
        "source_workspace_sha256": hash_tree(source_workspace),
        "target_workspace_sha256": hash_tree(target_workspace),
        "changed_workspace_files": len(set(source_files) | set(target_files)) - sum(
            source_files.get(name) == target_files.get(name) for name in set(source_files) | set(target_files)
        ),
    }
    (evidence_dir / "evidence_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {
        "evidence_directory": str(evidence_dir.relative_to(output_root)),
        "source_specification_path": str((evidence_dir / "source_specification.md").relative_to(output_root)),
        "target_specification_path": str((evidence_dir / "target_specification.md").relative_to(output_root)),
        "workspace_diff_path": str((evidence_dir / "workspace_diff.patch").relative_to(output_root)),
        "evidence_manifest_path": str((evidence_dir / "evidence_manifest.json").relative_to(output_root)),
        "source_agent_run_manifest_path": str(agent_manifest_files["source"].relative_to(output_root)) if "source" in agent_manifest_files else None,
        "target_agent_run_manifest_path": str(agent_manifest_files["target"].relative_to(output_root)) if "target" in agent_manifest_files else None,
    }


def package_file(package: dict[str, Any], package_root: Path, key: str) -> Path:
    relative = PurePosixPath(package[key])
    if relative.is_absolute() or ".." in relative.parts or "\\" in package[key]:
        raise ValueError(f"unsafe {key}: {package[key]!r}")
    path = package_root.joinpath(*relative.parts).resolve(strict=True)
    if not path.is_relative_to(package_root.resolve(strict=True)):
        raise ValueError(f"{key} escaped its package root")
    return path


def source_suite(
    task: str,
    method: str,
    guided_suffix: str,
    guided_root: Path = RESULT_ROOT,
    vanilla_suffix: str | None = None,
    vanilla_root: Path = RESULT_ROOT,
) -> tuple[Path, list[str], str, str | None]:
    package_root = (PACKAGE_ROOT / task / "C1").resolve(strict=True)
    package = read_json(package_root / "package.json")
    if method == "original":
        base = package_file(package, package_root, "base_tests_path")
        return base, [], hash_tree(base), None

    suffix = (vanilla_suffix or vanilla_cohort_suffix(task)) if method == "vanilla_llm" else guided_suffix
    generation_root = vanilla_root if method == "vanilla_llm" else guided_root
    generation_dir = generation_root / task / "C1" / suffix
    frozen_path = generation_dir / "frozen_test_suite.json"
    if not os.path.isfile(filesystem_path(frozen_path)):
        raise FileNotFoundError(f"{method} suite is not frozen: {frozen_path}")
    frozen = read_json(frozen_path)
    expected_method = method
    if frozen.get("task_id") != task or frozen.get("source_checkpoint") != "C1" or frozen.get("method") != expected_method:
        raise ValueError(f"frozen suite identity mismatch for {task}/{method}")
    suite_relative = PurePosixPath(frozen["suite_path"])
    base_relative = PurePosixPath(frozen["base_suite_path"])
    for label, relative in (("suite_path", suite_relative), ("base_suite_path", base_relative)):
        if relative.is_absolute() or ".." in relative.parts or "\\" in frozen[label]:
            raise ValueError(f"unsafe frozen {label}: {frozen[label]!r}")
    if base_relative.parts[: len(suite_relative.parts)] != suite_relative.parts:
        raise ValueError(f"invalid frozen base suite path: {frozen['base_suite_path']}")
    suite_root = generation_dir.joinpath(*suite_relative.parts)
    base = generation_dir.joinpath(*base_relative.parts)
    if not os.path.isdir(filesystem_path(base)):
        raise ValueError(f"invalid frozen base suite path: {base}")
    generated = list(frozen.get("generated_test_paths", []))
    if frozen_suite_hash(suite_root) != frozen.get("suite_sha256"):
        raise ValueError(f"frozen suite hash mismatch for {task}/{method}")
    validation_path = generation_dir / frozen["validation_log_path"]
    with open(filesystem_path(validation_path), "r", encoding="utf-8") as handle:
        validation_rows = [json.loads(line) for line in handle if line.strip()]
    retained_ids = [
        str(nodeid)
        for record in validation_rows if record.get("status") == "retained"
        for nodeid in record.get("collection_ids", [])
    ]
    return base, generated, frozen["suite_sha256"], str(generation_dir)


def eligible_source_ids(
    task: str,
    method: str,
    baseline: dict[str, Any],
    guided_suffix: str,
    guided_root: Path = RESULT_ROOT,
    vanilla_suffix: str | None = None,
    vanilla_root: Path = RESULT_ROOT,
) -> set[str]:
    ids = set(baseline.get("stable_pass_test_ids", []))
    if method == "original":
        return ids
    suffix = (vanilla_suffix or vanilla_cohort_suffix(task)) if method == "vanilla_llm" else guided_suffix
    generation_root = vanilla_root if method == "vanilla_llm" else guided_root
    generation_dir = generation_root / task / "C1" / suffix
    frozen = read_json(generation_dir / "frozen_test_suite.json")
    validation_path = generation_dir / frozen["validation_log_path"]
    for line in validation_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("status") == "retained":
            ids.update(str(item) for item in record.get("collection_ids", []))
    return ids


def materialize_suite_overlay(
    task: str,
    method: str,
    guided_suffix: str,
    run_stamp: str,
    guided_root: Path = RESULT_ROOT,
    vanilla_suffix: str | None = None,
    vanilla_root: Path = RESULT_ROOT,
) -> tuple[Path, str, set[str], dict[str, Any]]:
    package_root = (PACKAGE_ROOT / task / "C1").resolve(strict=True)
    package = read_json(package_root / "package.json")
    source_base, generated_paths, _declared_hash, generation_dir_text = source_suite(
        task,
        method,
        guided_suffix,
        guided_root,
        vanilla_suffix,
        vanilla_root,
    )
    suite_root = ROOT / "tmp" / "rq1-evaluation-suites" / task / f"{method}-{run_stamp}"
    base_destination = suite_root / "base_tests"
    suite_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(filesystem_path(source_base), filesystem_path(base_destination))
    if method != "original":
        generation_dir = Path(generation_dir_text)
        frozen = read_json(generation_dir / "frozen_test_suite.json")
        frozen_root = PurePosixPath(frozen["suite_path"])
        for relative in generated_paths:
            pure = PurePosixPath(relative)
            if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
                raise ValueError(f"unsafe frozen generated test path: {relative!r}")
            if pure.parts[: len(frozen_root.parts)] != frozen_root.parts:
                raise ValueError(f"generated test escaped frozen suite: {relative!r}")
            source = generation_dir.joinpath(*pure.parts)
            if not os.path.isfile(filesystem_path(source)):
                raise ValueError(f"generated test escaped frozen suite: {relative!r}")
            filename = pure.name
            destination = base_destination / filename
            if os.path.exists(filesystem_path(destination)):
                raise ValueError(f"generated test collides with base test: {filename}")
            shutil.copy2(filesystem_path(source), filesystem_path(destination))
    actual_hash = hash_tree(base_destination)
    if method == "original" and actual_hash != package.get("base_tests_sha256"):
        raise ValueError(f"Original suite copy hash mismatch for {task}")
    return base_destination, actual_hash, set(), package


def prepare_runtime(package: Path, problems_root: Path, output: Path) -> tuple[Path, dict[str, Any]]:
    result = subprocess.run(
        [
            sys.executable, str(PREPARE_SCRIPT),
            "--package", str(package),
            "--problems-root", str(problems_root),
            "--output", str(output),
        ],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"target runtime staging failed: {result.stdout}\n{result.stderr}")
    marker = "Prepared isolated checkpoint context: "
    line = next((value for value in result.stdout.splitlines() if value.startswith(marker)), None)
    if line is None:
        raise RuntimeError("runtime preparation did not report its staging directory")
    stage = Path(line[len(marker):]).resolve(strict=True)
    return stage, read_json(stage / "runtime_manifest.json")


def evaluation_command(
    runtime_dir: Path,
    runtime: dict[str, Any],
    suite_dir: Path,
    test_nodeids: list[str] | None = None,
) -> list[str]:
    command = [
        "docker", "run", "--rm", "--workdir", CONTAINER_WORKSPACE,
        "--mount", f"type=bind,source={runtime_dir},target={CONTAINER_WORKSPACE}",
        "--mount", f"type=bind,source={suite_dir},target={CONTAINER_WORKSPACE}/base_tests,readonly",
        "--env", f"PYTHONPATH={CONTAINER_WORKSPACE}",
    ]
    for mount in runtime.get("asset_mounts_read_only", []):
        target = str(mount["target"])
        if not target.startswith(CONTAINER_WORKSPACE.rstrip("/") + "/"):
            raise ValueError(f"asset mount escapes workspace: {target}")
        base_tests_target = PurePosixPath(f"{CONTAINER_WORKSPACE}/base_tests")
        target_path = PurePosixPath(target)
        if target_path.parts[: len(base_tests_target.parts)] == base_tests_target.parts:
            relative_target = target_path.parts[len(base_tests_target.parts):]
            if not relative_target or ".." in relative_target:
                raise ValueError(f"invalid asset mount target under base tests: {target}")
            # Docker cannot create a nested mount point beneath a read-only bind mount.
            # Create the empty mount-point directory in the disposable suite overlay first.
            (suite_dir.joinpath(*relative_target)).mkdir(parents=True, exist_ok=True)
        command.extend(["--mount", f"type=bind,source={mount['source']},target={target},readonly"])

    pytest_command = list(runtime["test_command_argv"])
    try:
        pytest_index = pytest_command.index("pytest")
    except ValueError as error:
        raise ValueError("target command does not invoke pytest") from error
    report_args = ["-p", "no:cacheprovider", "--json-report", f"--json-report-file={REPORT_PATH}"]
    if test_nodeids:
        preserved_args: list[str] = []
        replaced_suite_path = False
        for argument in pytest_command[pytest_index + 1:]:
            if argument in {"base_tests", f"{CONTAINER_WORKSPACE}/base_tests"}:
                preserved_args.extend(test_nodeids)
                replaced_suite_path = True
            else:
                preserved_args.append(argument)
        if not replaced_suite_path:
            preserved_args.extend(test_nodeids)
        pytest_command = pytest_command[:pytest_index + 1] + ["-q", *report_args, *preserved_args]
    else:
        pytest_command[pytest_index + 1:pytest_index + 1] = report_args
    shell_commands = [shlex.join(list(item)) for item in runtime.get("container_setup_commands", [])]
    shell_commands.append(shlex.join(pytest_command))
    command.extend([str(runtime["docker_image"]), "sh", "-lc", " && ".join(shell_commands)])
    return command


def run_suite(
    runtime_dir: Path,
    runtime: dict[str, Any],
    suite_dir: Path,
    timeout: int,
    test_nodeids: list[str] | None = None,
) -> dict[str, Any]:
    report = runtime_dir / "rq1-evaluation-report.json"
    if report.exists():
        report.unlink()
    command = evaluation_command(runtime_dir, runtime, suite_dir, test_nodeids)
    try:
        result = subprocess.run(
            command, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as error:
        return {
            "status": "timeout", "exit_code": None, "tests": [],
            "stdout": error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or ""),
            "stderr": error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or ""),
            "report_error": f"pytest timed out after {timeout} seconds",
        }
    stdout_path = runtime_dir / "rq1-evaluation.stdout.log"
    stderr_path = runtime_dir / "rq1-evaluation.stderr.log"
    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    tests: list[dict[str, Any]] = []
    report_error: str | None = None
    if report.is_file():
        try:
            raw = read_json(report)
            tests = [
                {
                    "nodeid": str(item["nodeid"]),
                    "outcome": str(item["outcome"]),
                    "longrepr": pytest_test_detail(item),
                }
                for item in raw.get("tests", []) if "nodeid" in item and "outcome" in item
            ]
        except (OSError, KeyError, json.JSONDecodeError) as error:
            report_error = f"could not parse pytest report: {error}"
    else:
        report_error = "pytest did not produce a JSON report"
    status = "complete" if result.returncode in {0, 1} and report_error is None and tests else "infrastructure_error"
    return {
        "status": status,
        "exit_code": result.returncode,
        "tests": tests,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "report_error": report_error,
        "command": command,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", action="append", help="Limit to task IDs; may be repeated")
    parser.add_argument(
        "--method",
        action="append",
        choices=METHODS,
        help="Evaluate only the selected method(s); may be repeated. Defaults to all three.",
    )
    parser.add_argument("--guided-suffix", default=DEFAULT_GUIDED)
    parser.add_argument(
        "--guided-root", type=Path, default=RESULT_ROOT,
        help="Root containing task/C1/<guided-suffix>; use a separate root for recovery suites.",
    )
    parser.add_argument(
        "--vanilla-suffix",
        help="Frozen Vanilla directory name under each task/C1; defaults to the audited cohort suffix.",
    )
    parser.add_argument(
        "--vanilla-root", type=Path, default=RESULT_ROOT,
        help="Root containing task/C1/<vanilla-suffix>; use a separate root for recovery suites.",
    )
    parser.add_argument("--problems-root", type=Path, default=ROOT / "external" / "scb-problems")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--resume", action="store_true", help="Resume an incomplete evaluation output directory")
    args = parser.parse_args()
    methods = tuple(dict.fromkeys(args.method or METHODS))
    if args.guided_root.resolve() != RESULT_ROOT.resolve() and methods != ("mutation_guided",):
        raise ValueError("a non-default guided root is supported only for MG-only evaluation")
    args.guided_root = args.guided_root.resolve(strict=True)
    args.vanilla_root = args.vanilla_root.resolve(strict=True)

    baseline_audit = read_json(BASELINE_PATH)
    baseline_by_task = {row["task_id"]: row for row in baseline_audit["records"]}
    units = read_json(UNIT_PATH)["units"]
    if baseline_audit.get("status") != "complete" or len(baseline_by_task) != 36:
        raise ValueError("current sanitized C1 baseline audit is incomplete")
    for unit in units:
        task = unit["task_id"]
        package = read_json(PACKAGE_ROOT / task / "C1" / "package.json")
        if baseline_by_task.get(task, {}).get("base_tests_sha256") != package.get("base_tests_sha256"):
            raise ValueError(f"{task}: current C1 baseline does not match the sanitized package base tests")
    selected = [unit for unit in units if not args.task or unit["task_id"] in args.task]
    if not selected:
        raise ValueError("no RQ1 task units selected")

    # Freeze gate: do not read target checkpoint files until each selected method is frozen for every selected C1 unit.
    for unit in selected:
        task = unit["task_id"]
        for method in methods:
            source_suite(
                task,
                method,
                args.guided_suffix,
                args.guided_root,
                args.vanilla_suffix,
                args.vanilla_root,
            )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output_dir.resolve() if args.output_dir else RESULT_ROOT / f"eval_{stamp}"
    if not output.is_relative_to(RESULTS_ROOT.resolve(strict=True)):
        raise ValueError("evaluation output must remain beneath the artifacts/results root")
    args.problems_root = args.problems_root.resolve(strict=True)
    config = {
        "task_ids": [unit["task_id"] for unit in selected],
        "methods": list(methods),
        "guided_suffix": args.guided_suffix,
        "guided_root": str(args.guided_root),
        "vanilla_suffix": args.vanilla_suffix or DEFAULT_VANILLA,
        "vanilla_root": str(args.vanilla_root),
        "problems_root": str(args.problems_root),
        "timeout_seconds": args.timeout_seconds,
        "baseline_audit_sha256": sha256_file(BASELINE_PATH),
        "method_suite_count": len(methods),
    }
    config_path = output / "evaluation_config.json"
    if output.exists():
        if not args.resume or not config_path.is_file():
            raise ValueError("evaluation output already exists; pass --resume for an incomplete matching run")
        if read_json(config_path) != config:
            raise ValueError("evaluation resume configuration differs from the saved run")
        existing_summary_path = output / "evaluation_summary.json"
        if existing_summary_path.is_file() and read_json(existing_summary_path).get("status") == "complete":
            raise ValueError("evaluation output already has a complete final summary")
    else:
        if args.resume:
            raise FileNotFoundError("cannot resume: evaluation output directory does not exist")
        output.mkdir(parents=True)
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    run_path = output / "evaluation_runs.jsonl"
    events_path = output / "candidate_failure_observations.jsonl"
    run_records_by_key = {
        f"{row['task_id']}|{row['target_checkpoint']}|{row['suite_sha256']}": row
        for row in read_jsonl(run_path)
    }
    observations_by_id = {
        row["observation_id"]: row for row in read_jsonl(events_path)
    }

    def needs_reproducibility_repair(task: str, checkpoint: str, suite_hash: str) -> bool:
        return any(
            row.get("task_id") == task
            and row.get("target_checkpoint") == checkpoint
            and row.get("suite_sha256") == suite_hash
            and row.get("reproducibility", {}).get("status") == "infrastructure_error"
            for row in observations_by_id.values()
        )

    expected_run_keys: set[str] = set()
    args.stamp = stamp

    for task_index, unit in enumerate(selected, 1):
        task = unit["task_id"]
        baseline = baseline_by_task[task]
        source_package_root = (PACKAGE_ROOT / task / "C1").resolve(strict=True)
        source_package = read_json(source_package_root / "package.json")
        eligible_by_method = {
            method: eligible_source_ids(
                task,
                method,
                baseline,
                args.guided_suffix,
                args.guided_root,
                args.vanilla_suffix,
                args.vanilla_root,
            )
            for method in methods
        }
        suites: dict[str, dict[str, Any]] = {}
        for method in methods:
            suite_dir, suite_hash, _, _ = materialize_suite_overlay(
                task,
                method,
                args.guided_suffix,
                stamp,
                args.guided_root,
                args.vanilla_suffix,
                args.vanilla_root,
            )
            row = suites.setdefault(suite_hash, {"suite_dir": suite_dir, "methods": [], "eligible_test_ids": set()})
            row["methods"].append(method)
            row["eligible_test_ids"].update(eligible_by_method[method])

        targets = unit.get("target_checkpoints", [])
        for target_index, checkpoint in enumerate(targets, 1):
            expected_run_keys.update(f"{task}|{checkpoint}|{suite_hash}" for suite_hash in suites)
            print(f"[{task_index}/{len(selected)}] {task}/{checkpoint}", flush=True)
            target_package = (PACKAGE_ROOT / task / checkpoint).resolve(strict=True)
            target_package_manifest = read_json(target_package / "package.json")
            pair_evidence = build_pair_evidence(
                task,
                checkpoint,
                source_package_root,
                target_package,
                output,
            )
            suite_keys = [f"{task}|{checkpoint}|{suite_hash}" for suite_hash in suites]
            if all(
                run_records_by_key.get(key, {}).get("status") == "complete"
                and not needs_reproducibility_repair(task, checkpoint, key.rsplit("|", 1)[1])
                for key in suite_keys
            ):
                print(f"  all {len(suites)} suites already complete; skipping", flush=True)
                continue
            target_name = f"{checkpoint}-rq1-{stamp}-{task_index:02d}"
            target_stage_path = ROOT / "tmp" / "checkpoint-runtime" / task / target_name
            runtime_dir, runtime = prepare_runtime(target_package, args.problems_root, target_stage_path)
            for suite_hash, suite in suites.items():
                run_key = f"{task}|{checkpoint}|{suite_hash}"
                repairing_reproductions = needs_reproducibility_repair(task, checkpoint, suite_hash)
                if run_records_by_key.get(run_key, {}).get("status") == "complete" and not repairing_reproductions:
                    continue
                run_dir = output / task / checkpoint / suite_hash[:12]
                run_dir.mkdir(parents=True, exist_ok=True)
                result = run_suite(runtime_dir, runtime, suite["suite_dir"], args.timeout_seconds)
                report = runtime_dir / "rq1-evaluation-report.json"
                if report.is_file():
                    shutil.copy2(report, run_dir / "pytest-report.json")
                (run_dir / "stdout.log").write_text(result["stdout"], encoding="utf-8")
                (run_dir / "stderr.log").write_text(result["stderr"], encoding="utf-8")
                persisted = {key: value for key, value in result.items() if key not in {"stdout", "stderr"}}
                persisted.update({
                    "task_id": task,
                    "source_checkpoint": "C1",
                    "target_checkpoint": checkpoint,
                    "suite_sha256": suite_hash,
                    "methods": suite["methods"],
                    "runtime_manifest_path": str(runtime_dir.relative_to(ROOT) / "runtime_manifest.json"),
                    "pytest_report_path": "pytest-report.json" if (run_dir / "pytest-report.json").is_file() else None,
                    "stdout_path": "stdout.log",
                    "stderr_path": "stderr.log",
                })
                non_code_errors = [
                    item for item in result["tests"]
                    if item["outcome"] == "error" and not is_target_syntax_regression(item)
                ]
                if result["status"] == "complete" and non_code_errors:
                    persisted["status"] = "infrastructure_error"
                    persisted["report_error"] = "pytest reported test/setup errors; excluded from candidate-regression extraction"
                if persisted["status"] != "complete":
                    (run_dir / "run_record.json").write_text(json.dumps(persisted, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                    run_records_by_key[run_key] = persisted
                    write_jsonl(run_path, list(run_records_by_key.values()))
                    continue

                eligible = suite["eligible_test_ids"]
                observed_ids = {item["nodeid"] for item in result["tests"]}
                missing = sorted(eligible - observed_ids)
                if missing:
                    persisted["eligible_tests_missing_from_target_report"] = missing
                    persisted["status"] = "infrastructure_error"
                    (run_dir / "run_record.json").write_text(json.dumps(persisted, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                    run_records_by_key[run_key] = persisted
                    write_jsonl(run_path, list(run_records_by_key.values()))
                    continue
                if repairing_reproductions:
                    stale_ids = [
                        observation_id for observation_id, row in observations_by_id.items()
                        if row.get("task_id") == task
                        and row.get("target_checkpoint") == checkpoint
                        and row.get("suite_sha256") == suite_hash
                    ]
                    for observation_id in stale_ids:
                        del observations_by_id[observation_id]
                for test in result["tests"]:
                    if test["nodeid"] not in eligible:
                        continue
                    if test["outcome"] == "error" and not is_target_syntax_regression(test):
                        continue
                    if test["outcome"] not in {"failed", "error"}:
                        continue
                    relative_test = test["nodeid"].split("::", 1)[0]
                    rel = PurePosixPath(relative_test)
                    if rel.is_absolute() or ".." in rel.parts or "\\" in relative_test:
                        raise ValueError(f"unsafe pytest node ID path: {relative_test!r}")
                    test_file = suite["suite_dir"].joinpath(*rel.parts)
                    test_code = test_file.read_text(encoding="utf-8") if test_file.is_file() else ""
                    occurrences = [
                        {"method": method, "suite_sha256": suite_hash, "nodeid": test["nodeid"]}
                        for method in suite["methods"] if test["nodeid"] in eligible_by_method[method]
                    ]
                    observation = {
                        "observation_id": f"{task}-{checkpoint}-{hashlib.sha256((suite_hash + test['nodeid']).encode()).hexdigest()[:12]}",
                        "task_id": task,
                        "trajectory_id": source_package["trajectory_id"],
                        "source_checkpoint": "C1",
                        "target_checkpoint": checkpoint,
                        "test_nodeid": test["nodeid"],
                        "test_sha256": sha256_file(test_file) if test_file.is_file() else None,
                        "test_code": test_code,
                        "methods_detected": sorted({item["method"] for item in occurrences}),
                        "suite_sha256": suite_hash,
                        "failure_longrepr": test.get("longrepr"),
                        "evidence_run_directory": str(run_dir.relative_to(ROOT)),
                        **pair_evidence,
                    }
                    reproduction_runs: list[dict[str, Any]] = []
                    exact_failure_reproductions = 0
                    exact_error_reproductions = 0
                    for repetition in range(1, REPRODUCIBILITY_REPEATS + 1):
                        reproduced = run_suite(
                            runtime_dir,
                            runtime,
                            suite["suite_dir"],
                            args.timeout_seconds,
                            [test["nodeid"]],
                        )
                        report = runtime_dir / "rq1-evaluation-report.json"
                        test_hash = hashlib.sha256(test["nodeid"].encode()).hexdigest()[:10]
                        repro_prefix = f"rep-{test_hash}-{repetition}"
                        if report.is_file():
                            shutil.copy2(report, run_dir / f"{repro_prefix}.json")
                        (run_dir / f"{repro_prefix}.stdout.log").write_text(
                            reproduced["stdout"], encoding="utf-8"
                        )
                        (run_dir / f"{repro_prefix}.stderr.log").write_text(
                            reproduced["stderr"], encoding="utf-8"
                        )
                        matching_outcomes = [
                            item for item in reproduced["tests"] if item["nodeid"] == test["nodeid"]
                        ]
                        reproduced_failure = (
                            reproduced["status"] == "complete"
                            and len(matching_outcomes) == 1
                            and matching_outcomes[0]["outcome"] == "failed"
                        )
                        reproduced_error = (
                            reproduced["status"] == "complete"
                            and len(matching_outcomes) == 1
                            and matching_outcomes[0]["outcome"] == "error"
                        )
                        if reproduced_failure:
                            exact_failure_reproductions += 1
                        if reproduced_error:
                            exact_error_reproductions += 1
                        reproduction_runs.append({
                            "repetition": repetition,
                            "run_status": reproduced["status"],
                            "exit_code": reproduced["exit_code"],
                            "outcomes": matching_outcomes,
                            "report_error": reproduced.get("report_error"),
                            "pytest_report_path": f"{repro_prefix}.json"
                            if (run_dir / f"{repro_prefix}.json").is_file()
                            else None,
                        })
                    if exact_failure_reproductions == REPRODUCIBILITY_REPEATS:
                        reproducibility_status = "stable_failure"
                    elif exact_error_reproductions == REPRODUCIBILITY_REPEATS:
                        reproducibility_status = "stable_error"
                    elif any(item["run_status"] != "complete" for item in reproduction_runs):
                        reproducibility_status = "infrastructure_error"
                    elif exact_failure_reproductions == 0:
                        reproducibility_status = "transient_failure"
                    else:
                        reproducibility_status = "flaky"
                    observation["reproducibility"] = {
                        "repeat_count": REPRODUCIBILITY_REPEATS,
                        "repeated_failure_count": exact_failure_reproductions,
                        "repeated_error_count": exact_error_reproductions,
                        "status": reproducibility_status,
                        "runs": reproduction_runs,
                    }
                    observations_by_id[observation["observation_id"]] = observation
                    write_jsonl(events_path, list(observations_by_id.values()))
                (run_dir / "run_record.json").write_text(json.dumps(persisted, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                run_records_by_key[run_key] = persisted
                write_jsonl(run_path, list(run_records_by_key.values()))
            print(f"  suites={len(suites)} candidate failures so far={len(observations_by_id)}", flush=True)

    observations = list(observations_by_id.values())
    run_records = list(run_records_by_key.values())
    write_jsonl(events_path, observations)
    write_jsonl(run_path, run_records)
    missing_run_keys = sorted(expected_run_keys - set(run_records_by_key))
    infra_errors = sum(row.get("status") != "complete" for row in run_records) + len(missing_run_keys)
    reproducibility_counts = Counter(
        row.get("reproducibility", {}).get("status", "missing") for row in observations
    )
    summary = {
        "format": "rq1-later-checkpoint-evaluation/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset": "rq1_full_36x1",
        "task_count": len(selected),
        "methods": list(methods),
        "source_checkpoint": "C1",
        "method_suite_count": len(methods),
        "unique_suite_runs": len(run_records),
        "expected_unique_suite_runs": len(expected_run_keys),
        "missing_suite_runs": missing_run_keys,
        "target_run_count": sum(len(unit.get("target_checkpoints", [])) for unit in selected),
        "candidate_failure_observation_count": len(observations),
        "reproducibility_counts": reproducibility_counts,
        "reproducibility_repeat_count": REPRODUCIBILITY_REPEATS,
        "infrastructure_error_count": infra_errors,
        "baseline_audit_sha256": sha256_file(BASELINE_PATH),
        "guided_suite_suffix": args.guided_suffix,
        "guided_suite_root": str(args.guided_root),
        "vanilla_suite_suffix": args.vanilla_suffix or DEFAULT_VANILLA,
        "vanilla_suite_root": str(args.vanilla_root),
        "candidate_observations_path": events_path.name,
        "run_records_path": run_path.name,
        "status": "complete" if infra_errors == 0 else "complete_with_infrastructure_errors",
    }
    (output / "evaluation_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"Evaluation artifacts: {output}")
    return 0 if infra_errors == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

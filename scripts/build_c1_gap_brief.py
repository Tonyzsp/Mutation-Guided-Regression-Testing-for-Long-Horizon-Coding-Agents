"""Create a shared, source-checkpoint-only gap-review brief for one C1 package."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_AUDITOR = REPO_ROOT / "scripts" / "validate_checkpoint_package.py"
DEFAULT_PACKAGE_ROOT = REPO_ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def filesystem_path(path: Path) -> str:
    value = os.path.abspath(os.fspath(path))
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        return "\\\\?\\" + value
    return value


def walk_files(root: Path) -> list[tuple[str, Path]]:
    root_ext = filesystem_path(root)
    result: list[tuple[str, Path]] = []
    for current, directories, names in os.walk(root_ext):
        directories.sort()
        for name in sorted(names):
            absolute = os.path.join(current, name)
            relative = os.path.relpath(absolute, root_ext).replace(os.sep, "/")
            result.append((relative, Path(absolute)))
    return result


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(filesystem_path(path)).read_bytes()).hexdigest()


def safe_package_path(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
        raise ValueError(f"unsafe package path: {relative!r}")
    result = root.joinpath(*pure.parts).resolve(strict=True)
    if not result.is_relative_to(root.resolve(strict=True)):
        raise ValueError(f"package path escapes its root: {relative!r}")
    return result


def sha256_tree(root: Path) -> str:
    records = []
    for relative, path in walk_files(root):
        records.append(
            {
                "path": relative,
                "sha256": sha256_file(path),
            }
        )
    payload = json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def collect_test_functions(root: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    functions: list[dict[str, str]] = []
    parse_errors: list[dict[str, str]] = []
    for relative, path in walk_files(root):
        if not Path(relative).name.startswith("test") or not Path(relative).name.endswith(".py"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except SyntaxError as error:
            parse_errors.append({
                "path": relative,
                "line": str(error.lineno or ""),
                "message": error.msg,
            })
            continue

        class TestCollector(ast.NodeVisitor):
            def __init__(self) -> None:
                self.class_stack: list[str] = []

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.class_stack.append(node.name)
                self.generic_visit(node)
                self.class_stack.pop()

            def _visit_test(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                if node.name.startswith("test_"):
                    docstring = ast.get_docstring(node) or "(no test docstring)"
                    functions.append({
                        "path": relative,
                        "function": ".".join([*self.class_stack, node.name]),
                        "docstring": " ".join(docstring.split()),
                    })
                self.generic_visit(node)

            visit_FunctionDef = _visit_test
            visit_AsyncFunctionDef = _visit_test

        TestCollector().visit(tree)
    return functions, parse_errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True, help="Sanitized task/C1 package")
    parser.add_argument("--output", type=Path, required=True, help="New .md brief under artifacts/results")
    args = parser.parse_args()
    package_root = args.package.resolve(strict=True)
    package_audit = subprocess.run(
        [sys.executable, str(PACKAGE_AUDITOR), "--package", str(package_root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if package_audit.returncode:
        raise ValueError(f"checkpoint package audit failed:\n{package_audit.stdout}\n{package_audit.stderr}")
    package = read_json(package_root / "package.json")
    spec_path = safe_package_path(package_root, package["specification_path"])
    tests_path = safe_package_path(package_root, package["base_tests_path"])
    specification = spec_path.read_text(encoding="utf-8")
    headings = [
        line.strip()
        for line in specification.splitlines()
        if re.match(r"^#{1,6}\s+", line)
    ]
    test_functions, test_parse_errors = collect_test_functions(tests_path)
    if not headings:
        # Some benchmark specifications are prose without Markdown structure.
        # Keep them eligible and tell the reviewer to use the whole document.
        headings = ["(No Markdown headings; audit the complete specification.)"]
    if not test_functions:
        raise ValueError("the C1 base-test files contain no discoverable pytest test functions")

    heading_block = "\n".join(f"- {item}" for item in headings)
    tests_block = "\n".join(
        f"- `{item['path']}::{item['function']}` — {item['docstring']}"
        for item in test_functions
    )
    parse_error_block = (
        "\n".join(f"- `{item['path']}:{item['line']}` — {item['message']}" for item in test_parse_errors)
        if test_parse_errors
        else "- None"
    )
    brief = f"""# Shared specification-gap brief — {package['task_id']}/C1

This brief is generated only from the sanitized C1 specification and legal C1 base-test
files. Give this exact brief to both Vanilla and Mutation-Guided. It contains no mutant
information, hidden tests, future checkpoint data, later failures, or prior generated
candidate results.

## Specification sections to audit

{heading_block}

## C1 base-test functions to compare against

{tests_block}

## Base-test files not available to static AST review

These files remain in the legal base suite. Their parse problem must be accounted for by
the runtime audit; do not infer behavior from them:

{parse_error_block}

## Required gap review

Compare each explicit, externally observable requirement in the supplied specification
with the assertions in the complete base tests. Test names alone do not prove that a
requirement is covered. Identify up to {8} highest-value requirements that have no direct
assertion. Give each gap a stable ID (`GAP-01`, `GAP-02`, ...), cite its specification
section, and state which exact public input and expected result are supported by the
provided specification or current legal fixtures.

Mark a gap **required and testable** only when the input and correctness oracle are
deterministic and supported by those allowed materials. Mark it deferred when an expected
result would require guessing, a new private fixture, hidden data, or future behavior. The
generator should produce one test per required/testable gap, up to the declared candidate
budget, and explain deferred gaps in its assessment. If every explicit behavior is already
directly asserted or no additional behavior has a supported deterministic oracle, explain
that conclusion instead of manufacturing a test.

Do not change the specification to match the implementation. If a base test conflicts with
the written specification, call out the conflict and do not use the conflicting test as an
oracle for a generated candidate. Every generated candidate must pass on the unmodified C1
source and assert only specified behavior.
"""

    output = args.output.resolve()
    allowed_results = (REPO_ROOT / "artifacts" / "results").resolve(strict=True)
    if not output.is_relative_to(allowed_results) or output.exists() or output.suffix.lower() != ".md":
        raise ValueError(f"output must be a new Markdown file beneath {allowed_results}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(brief, encoding="utf-8")
    manifest = {
        "format": "rq1-c1-opportunity-brief/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "task_id": package["task_id"],
        "checkpoint_id": package["checkpoint_id"],
        "trajectory_id": package["trajectory_id"],
        "source_package": str(package_root),
        "workspace_revision": package["workspace_revision"],
        "base_tests_sha256": package["base_tests_sha256"],
        "specification_sha256": sha256_file(spec_path),
        "base_tests_path": package["base_tests_path"],
        "base_test_function_count": len(test_functions),
        "base_test_static_parse_errors": test_parse_errors,
        "specification_headings": headings,
        "brief_path": str(output),
        "brief_sha256": sha256_file(output),
        "generator_inputs_excluded": ["hidden tests", "future checkpoints", "later failures", "mutant outcomes"],
    }
    output.with_name("opportunity_brief_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Gap brief: {output}")
    print(f"Specification sections: {len(headings)}")
    print(f"Base-test functions: {len(test_functions)}")
    print(f"Brief manifest: {output.with_name('opportunity_brief_manifest.json')}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

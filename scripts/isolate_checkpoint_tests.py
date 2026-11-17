"""Move hidden, excluded, and future tests out of checkpoint generator inputs."""

from __future__ import annotations

import argparse
import ast
import builtins
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any


CHECKPOINT_COMPONENT = re.compile(r"^(?:test_)?checkpoint_(\d+)(?:$|[_\-.])", re.I)
SHA256 = re.compile(r"^[a-f0-9]{64}$")
CATEGORY_ROOTS = {
    "hidden": "hidden_tests",
    "future": "future_tests",
    "errors": "excluded_tests",
}
CASE_ID_FIELDS = {"id", "case_id", "test_id", "case_name", "test_name"}
GENERIC_CASE_MARKERS = {
    "case", "cases", "test", "tests", "hidden", "future", "data", "input", "output",
    "expected", "actual", "args", "arguments", "name", "id", "case_id", "test_id",
}


def sha256_tree(root: Path) -> str:
    digest = hashlib.sha256()
    root_ext = filesystem_path(root)
    files: list[tuple[str, str]] = []
    for current, directories, names in os.walk(root_ext):
        directories.sort()
        for name in names:
            absolute = os.path.join(current, name)
            relative = os.path.relpath(absolute, root_ext).replace(os.sep, "/")
            files.append((relative, absolute))
    for relative, absolute in sorted(files):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with open(absolute, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def filesystem_path(path: Path) -> str:
    value = os.path.abspath(os.fspath(path))
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        return "\\\\?\\" + value
    return value


def walk_files(root: Path) -> list[tuple[Path, Path]]:
    """Enumerate files with extended-length paths while returning relative paths."""
    root_ext = filesystem_path(root)
    result: list[tuple[Path, Path]] = []
    for current, directories, names in os.walk(root_ext):
        directories.sort()
        for name in sorted(names):
            absolute = os.path.join(current, name)
            relative = Path(os.path.relpath(absolute, root_ext))
            result.append((Path(absolute), relative))
    return result


def category_for(relative: Path, task: str, checkpoint_number: int) -> str | None:
    parts = [part.casefold() for part in relative.parts]
    if any("hidden" in part for part in parts):
        return "hidden"
    if any("future" in part for part in parts):
        return "future"
    if task == "file_backup" and any("errors" in part for part in parts):
        return "errors"
    for part in parts:
        match = CHECKPOINT_COMPONENT.match(part)
        if match and int(match.group(1)) > checkpoint_number:
            return "future"
    return None


def checkpoint_future(value: str, checkpoint_number: int) -> bool:
    match = CHECKPOINT_COMPONENT.match(value.casefold())
    return bool(match and int(match.group(1)) > checkpoint_number)


def has_marker_token(value: str, marker: str) -> bool:
    """Match category words in snake_case and CamelCase names."""
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    separated = re.sub(r"([A-Z])([A-Z][a-z])", r"\1_\2", separated)
    return marker in re.split(r"[^a-z0-9]+", separated.casefold())


def is_test_source(relative: Path) -> bool:
    name = relative.name.casefold()
    return name.startswith("test_") or name in {"conftest.py", "case_utils.py", "helpers.py"}


def node_category(
    node: ast.AST,
    *,
    task: str,
    checkpoint_number: int,
) -> str | None:
    names: list[str] = []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.append(node.name)
    if isinstance(node, ast.Assign):
        names.extend(target.id for target in node.targets if isinstance(target, ast.Name))
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        names.append(node.target.id)

    folded_names = [name.casefold() for name in names]
    referenced_original_names = {
        item.id
        for item in ast.walk(node)
        if isinstance(item, ast.Name)
    }
    referenced_names = {name.casefold() for name in referenced_original_names}
    all_names = set(folded_names) | referenced_names
    marker_names = set(names) | referenced_original_names
    if any(has_marker_token(name, "hidden") for name in marker_names):
        return "hidden"
    if any(
        checkpoint_future(name, checkpoint_number) or has_marker_token(name, "future")
        for name in marker_names
    ):
        return "future"
    if any(re.search(r"(?:^|_)(?:future|later)_?(?:cases|tests|data)(?:$|_)", name) for name in folded_names):
        return "future"
    if task == "file_backup" and any(re.search(r"(?:^|_)errors?(?:_cases|_tests|_data)?$", name) for name in folded_names):
        return "errors"

    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    if any(re.search(r"(?:^|_)(?:future|later)_?(?:cases|tests|data)(?:$|_)", name) for name in referenced_names):
        return "future"

    for item in ast.walk(node):
        if not isinstance(item, ast.Call):
            continue
        called = item.func.id if isinstance(item.func, ast.Name) else (
            item.func.attr if isinstance(item.func, ast.Attribute) else ""
        )
        if called not in {"run_case", "run_checkpoint_case", "run_case_data"}:
            continue
        category_args = [
            argument.value.casefold()
            for argument in item.args
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
        ]
        category_args.extend(
            keyword.value.value.casefold()
            for keyword in item.keywords
            if keyword.arg in {"category", "group", "test_group"}
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        )
        if "hidden" in category_args:
            return "hidden"
        if "future" in category_args:
            return "future"
        if task == "file_backup" and "errors" in category_args:
            return "errors"
    return None


def restricted_python(
    source: str,
    *,
    task: str,
    checkpoint_number: int,
) -> tuple[str, dict[str, list[str]], dict[str, set[str]]]:
    tree = ast.parse(source)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    declarations: list[ast.AST] = []
    case_ids: dict[str, set[str]] = {"hidden": set(), "future": set(), "errors": set()}
    snippets: dict[str, list[str]] = {"hidden": [], "future": [], "errors": []}
    for node in ast.walk(tree):
        parent = parents.get(node)
        if not isinstance(parent, (ast.Module, ast.ClassDef)):
            continue
        if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        declarations.append(node)

    def defines(node: ast.AST) -> set[str]:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return {node.name.casefold()}
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets.extend(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets.append(node.target)
        return {
            item.id.casefold()
            for target in targets
            for item in ast.walk(target)
            if isinstance(item, ast.Name)
        }

    def references(node: ast.AST) -> set[str]:
        defined = defines(node)
        return {
            item.id.casefold()
            for item in ast.walk(node)
            if isinstance(item, ast.Name) and item.id.casefold() not in defined
        }

    categories: dict[ast.AST, str] = {}
    tainted_names: dict[str, set[str]] = {}
    for node in declarations:
        category = node_category(node, task=task, checkpoint_number=checkpoint_number)
        if category is None:
            continue
        categories[node] = category
        for name in defines(node):
            tainted_names.setdefault(name, set()).add(category)

    module_names = set(dir(builtins))
    has_star_import = False
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                has_star_import = True
            module_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name != "*"
            )
        elif node in categories:
            continue
        else:
            module_names.update(defines(node))

    # A test decorator executes during import. If sanitization removed the data
    # binding it referenced, remove that test rather than leave collection broken.
    if not has_star_import and task == "sheeteval" and checkpoint_number == 1:
        for node in declarations:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node in categories:
                continue
            unresolved = any(
                isinstance(item, ast.Name)
                and isinstance(item.ctx, ast.Load)
                and item.id not in module_names
                for decorator in node.decorator_list
                for item in ast.walk(decorator)
            )
            if unresolved:
                categories[node] = "hidden"
                for name in defines(node):
                    tainted_names.setdefault(name, set()).add("hidden")

    # Propagate restricted status through derived module/class-level values and tests.
    changed = True
    while changed:
        changed = False
        for node in declarations:
            if node in categories:
                continue
            inherited = {
                category
                for name in references(node)
                for category in tainted_names.get(name, set())
            }
            if not inherited:
                continue
            category = "hidden" if "hidden" in inherited else (
                "future" if "future" in inherited else "errors"
            )
            categories[node] = category
            changed = True
            for name in defines(node):
                tainted_names.setdefault(name, set()).add(category)

    candidates: list[tuple[int, int, str, ast.AST]] = []
    for node, category in categories.items():
        start = node.lineno
        for decorator in getattr(node, "decorator_list", []):
            start = min(start, decorator.lineno)
        end = getattr(node, "end_lineno", node.lineno)
        candidates.append((start, end, category, node))
        for item in ast.walk(node):
            if not isinstance(item, ast.Dict):
                continue
            for key, value in zip(item.keys, item.values):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value.casefold() in CASE_ID_FIELDS
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    identifier = value.value.strip().casefold()
                    if identifier and identifier not in GENERIC_CASE_MARKERS:
                        case_ids[category].add(identifier)

    lines = source.splitlines(keepends=True)
    removed: set[int] = set()
    for start, end, category, _node in sorted(candidates, key=lambda item: (item[0], -(item[1] - item[0]))):
        if any(line in removed for line in range(start, end + 1)):
            continue
        snippets[category].append("".join(lines[start - 1 : end]).rstrip())
        removed.update(range(start, end + 1))
    sanitized = "".join("\n" if index in removed else line for index, line in enumerate(lines, 1))
    return sanitized, snippets, case_ids


def json_category(
    value: Any,
    *,
    task: str,
    checkpoint_number: int,
    case_ids: dict[str, set[str]],
) -> str | None:
    if not isinstance(value, dict):
        return None
    for field in ("category", "group", "visibility", "test_category", "test_group", "case_group"):
        marker = value.get(field)
        if isinstance(marker, str):
            folded = marker.casefold()
            if folded == "hidden":
                return "hidden"
            if folded == "future":
                return "future"
            if task == "file_backup" and folded in {"error", "errors"}:
                return "errors"
    for field in ("id", "name", "case", "case_name", "test_id", "test_name"):
        marker = value.get(field)
        if isinstance(marker, str):
            folded = marker.casefold()
            if folded in case_ids["hidden"]:
                return "hidden"
            if folded in case_ids["future"]:
                return "future"
            if folded in case_ids["errors"]:
                return "errors"
    return None


def restricted_json(
    value: Any,
    *,
    task: str,
    checkpoint_number: int,
    case_ids: dict[str, set[str]],
) -> tuple[Any, dict[str, list[dict[str, Any]]]]:
    removed: dict[str, list[dict[str, Any]]] = {"hidden": [], "future": [], "errors": []}
    removed_marker = object()

    def visit(item: Any, path: list[str]) -> Any:
        category = json_category(
            item,
            task=task,
            checkpoint_number=checkpoint_number,
            case_ids=case_ids,
        )
        if category:
            removed[category].append({"json_path": path, "value": item})
            return removed_marker
        if isinstance(item, dict):
            result: dict[str, Any] = {}
            for key, child in item.items():
                key_string = str(key)
                folded = key_string.casefold()
                category = None
                if folded in {"hidden", "hidden_case", "hidden_cases", "hidden_test", "hidden_tests", "hidden_data", "hidden_examples"}:
                    category = "hidden"
                elif folded in {"future", "future_case", "future_cases", "future_test", "future_tests", "future_data", "later_cases", "later_tests", "later_data"} or checkpoint_future(key_string, checkpoint_number):
                    category = "future"
                elif task == "file_backup" and not path and folded in {"errors", "error_cases", "error_tests"}:
                    category = "errors"
                elif folded in case_ids["hidden"]:
                    category = "hidden"
                elif folded in case_ids["future"]:
                    category = "future"
                elif folded in case_ids["errors"]:
                    category = "errors"
                if category:
                    removed[category].append({"json_path": path + [key_string], "value": child})
                    continue
                filtered = visit(child, path + [key_string])
                if filtered is not removed_marker:
                    result[key] = filtered
            return result
        if isinstance(item, list):
            result = []
            for index, child in enumerate(item):
                filtered = visit(child, path + [str(index)])
                if filtered is not removed_marker:
                    result.append(filtered)
            return result
        return item

    return visit(value, []), removed


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with open(filesystem_path(path), "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "data"
        / "checkpoints"
        / "checkpoint_packages_rq1_full_36x1",
        help="Root containing <task>/C<n>/package.json packages",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Move files and update package hashes/manifests (default is dry run)",
    )
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    data_root = root.parent.parent.resolve(strict=True)
    if root.parent.name != "checkpoints":
        raise SystemExit("--root must be directly under data/checkpoints")

    plan: list[tuple[Path, Path, str, str, str]] = []
    embedded_plan: list[dict[str, Any]] = []
    package_dirs = sorted(path.parent for path in root.glob("*/*/package.json"))
    published_hashes: dict[str, str] = {}
    for package_root in package_dirs:
        task = package_root.parent.name
        checkpoint = package_root.name
        match = re.fullmatch(r"C([1-9][0-9]*)", checkpoint)
        if not match:
            raise SystemExit(f"invalid checkpoint directory: {package_root}")
        package_number = int(match.group(1))
        package = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
        published_hashes[f"{task}/{checkpoint}"] = package.get(
            "base_tests_sha256", ""
        )
        base_tests = package_root / package["base_tests_path"]
        files = walk_files(base_tests)
        path_categories: dict[Path, str] = {}
        for source, relative in files:
            category = category_for(relative, task, package_number)
            if category is None:
                continue
            path_categories[source] = category
            destination = (
                data_root
                / CATEGORY_ROOTS[category]
                / "rq1_full_36x1"
                / task
                / checkpoint
                / relative
            )
            data_root_abs = os.path.abspath(data_root)
            destination_abs = os.path.abspath(destination)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or os.path.commonpath([data_root_abs, destination_abs]) != data_root_abs
            ):
                raise SystemExit(f"isolation destination escapes data root: {destination}")
            if os.path.exists(filesystem_path(destination)):
                if file_digest(destination) != file_digest(source):
                    raise SystemExit(
                        f"destination collision with different contents: {destination}"
                    )
                digest = file_digest(source)[:12]
                destination = destination.with_name(
                    f"{destination.name}.duplicate-{digest}"
                )
                if os.path.exists(filesystem_path(destination)):
                    if file_digest(destination) == file_digest(source):
                        plan.append((source, destination, category, task, checkpoint))
                        continue
                    raise SystemExit(
                        f"duplicate isolation destination already exists: {destination}"
                    )
            plan.append((source, destination, category, task, checkpoint))

        case_ids: dict[str, set[str]] = {"hidden": set(), "future": set(), "errors": set()}
        package_embedded: dict[Path, dict[str, Any]] = {}
        for source, relative in files:
            if (
                source in path_categories
                or source.suffix.casefold() != ".py"
                or not is_test_source(relative)
            ):
                continue
            try:
                original = source.read_text(encoding="utf-8")
                sanitized, snippets, extracted_ids = restricted_python(
                    original,
                    task=task,
                    checkpoint_number=package_number,
                )
            except (OSError, SyntaxError, UnicodeDecodeError) as error:
                raise SystemExit(f"cannot audit Python tests at {source}: {error}") from error
            if not any(snippets.values()):
                continue
            for category, values in extracted_ids.items():
                case_ids[category].update(values)
            package_embedded[source] = {
                "relative": relative,
                "sanitized_bytes": sanitized.encode("utf-8"),
                "archives": {
                    category: {"source_snippets": snippets[category]}
                    for category in CATEGORY_ROOTS
                    if snippets[category]
                },
            }

        for source, relative in files:
            if (
                source in path_categories
                or source.suffix.casefold() != ".json"
                or not any(token in relative.name.casefold() for token in ("case", "test_data"))
            ):
                continue
            try:
                original = source.read_text(encoding="utf-8")
                value = json.loads(original)
                sanitized_value, removed = restricted_json(
                    value,
                    task=task,
                    checkpoint_number=package_number,
                    case_ids=case_ids,
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise SystemExit(f"cannot audit JSON tests at {source}: {error}") from error
            for category in CATEGORY_ROOTS:
                if removed[category]:
                    item = package_embedded.setdefault(source, {
                        "relative": relative,
                        "sanitized_bytes": b"",
                        "archives": {},
                    })
                    item["archives"].setdefault(category, {})["json_records"] = removed[category]
            if any(removed.values()):
                package_embedded[source]["sanitized_bytes"] = (
                    json.dumps(sanitized_value, indent=2, ensure_ascii=False) + "\n"
                ).encode("utf-8")

        for source, item in package_embedded.items():
            archive_paths: dict[str, Path] = {}
            for category in item["archives"]:
                relative_without_suffix = item["relative"].with_suffix("")
                archive_paths[category] = (
                    data_root
                    / CATEGORY_ROOTS[category]
                    / "rq1_full_36x1"
                    / task
                    / checkpoint
                    / "embedded"
                    / relative_without_suffix
                ).with_name(relative_without_suffix.name + f".{category}.json")
            embedded_plan.append({
                "source": source,
                "relative": item["relative"],
                "sanitized_bytes": item["sanitized_bytes"],
                "archives": item["archives"],
                "archive_paths": archive_paths,
                "task": task,
                "checkpoint": checkpoint,
            })

    counts = Counter((category, task, checkpoint) for _, _, category, task, checkpoint in plan)
    embedded_counts = Counter()
    for item in embedded_plan:
        for category, payload in item["archives"].items():
            embedded_counts[(category, item["task"], item["checkpoint"])] += (
                len(payload.get("source_snippets", []))
                + len(payload.get("json_records", []))
            )
    print(f"Packages scanned: {len(package_dirs)}")
    print(f"Files to isolate: {len(plan)}")
    for (category, task, checkpoint), count in sorted(counts.items()):
        print(f"  {category}: {task}/{checkpoint}: {count} files")
    print(f"Embedded restricted fragments/records to isolate: {sum(embedded_counts.values())}")
    for (category, task, checkpoint), count in sorted(embedded_counts.items()):
        print(f"  embedded {category}: {task}/{checkpoint}: {count}")
    if not plan and not embedded_plan:
        print("No remaining hidden, excluded, or future test files found.")
    if not args.apply:
        if plan or embedded_plan:
            print("Dry run only. Rerun with --apply to move files and refresh hashes.")
        return

    for source, destination, *_ in plan:
        os.makedirs(filesystem_path(destination.parent), exist_ok=True)
        os.replace(filesystem_path(source), filesystem_path(destination))

    embedded_records: list[dict[str, Any]] = []
    for item in embedded_plan:
        source: Path = item["source"]
        with open(filesystem_path(source), "wb") as handle:
            handle.write(item["sanitized_bytes"])
        for category, payload in item["archives"].items():
            archive: Path = item["archive_paths"][category]
            data_root_abs = os.path.abspath(data_root)
            archive_abs = os.path.abspath(archive)
            if os.path.commonpath([data_root_abs, archive_abs]) != data_root_abs:
                raise SystemExit(f"embedded isolation destination escapes data root: {archive}")
            archive.parent.mkdir(parents=True, exist_ok=True)
            archived = {
                "format": "rq1-isolated-embedded-tests/v1",
                "task_id": item["task"],
                "checkpoint_id": item["checkpoint"],
                "source_path": f"base_tests/{item['relative'].as_posix()}",
                "category": category,
                **payload,
            }
            encoded = (json.dumps(archived, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            if archive.exists() and archive.read_bytes() != encoded:
                digest = hashlib.sha256(encoded).hexdigest()[:12]
                archive = archive.with_name(archive.stem + f".duplicate-{digest}" + archive.suffix)
            archive.write_bytes(encoded)
            embedded_records.append({
                "category": category,
                "task_id": item["task"],
                "checkpoint_id": item["checkpoint"],
                "source_path": f"base_tests/{item['relative'].as_posix()}",
                "archive_path": archive.relative_to(data_root).as_posix(),
                "archive_sha256": hashlib.sha256(encoded).hexdigest(),
                "removed_source_fragments": len(payload.get("source_snippets", [])),
                "removed_json_records": len(payload.get("json_records", [])),
            })

    manifest_path = root.parent / "ISOLATION_MANIFEST.json"
    moves_path = root.parent / "ISOLATION_MOVES.json"
    isolation_manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.is_file()
        else {
            "format": "rq1-checkpoint-isolation-manifest/v1",
            "generator_input_root": "checkpoint_packages_rq1_full_36x1/<task>/C<n>/",
            "package_count": len(package_dirs),
            "packages": [],
        }
    )
    move_manifest = (
        json.loads(moves_path.read_text(encoding="utf-8"))
        if moves_path.is_file()
        else {"hidden": [], "future": [], "errors": []}
    )
    package_records = {
        record["package_path"]: record
        for record in isolation_manifest.get("packages", [])
    }
    for package_root in package_dirs:
        relative = f"{package_root.parent.name}/{package_root.name}"
        if relative not in package_records:
            package_records[relative] = {
                "task_id": package_root.parent.name,
                "checkpoint_id": package_root.name,
                "package_path": relative,
                "published_base_tests_sha256": published_hashes.get(relative, ""),
                "filtered_base_tests_sha256": "",
            }
            isolation_manifest.setdefault("packages", []).append(
                package_records[relative]
            )

    index_path = root / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else None
    index_records = (
        {record["package_path"]: record for record in index.get("records", [])}
        if index is not None
        else {}
    )
    for package_file in (package_root / "package.json" for package_root in package_dirs):
        package_root = package_file.parent
        package = json.loads(package_file.read_text(encoding="utf-8"))
        base_tests = package_root / package["base_tests_path"]
        new_hash = sha256_tree(base_tests)
        if not SHA256.fullmatch(package["workspace_revision"]):
            raise SystemExit(f"invalid workspace revision at {package_root}")
        package["base_tests_sha256"] = new_hash
        package_file.write_text(
            json.dumps(package, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        relative = f"{package['task_id']}/{package['checkpoint_id']}"
        if relative not in package_records:
            raise SystemExit(f"package missing from isolation manifest: {relative}")
        package_records[relative]["filtered_base_tests_sha256"] = new_hash
        if index is not None:
            if relative not in index_records:
                raise SystemExit(f"package missing from index.json: {relative}")
            index_records[relative]["base_tests_sha256"] = new_hash
            index_records[relative]["workspace_revision"] = package[
                "workspace_revision"
            ]

    if index is not None:
        index_path.write_text(
            json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    grouped_moves = [
        {
            "category": category,
            "task_id": task,
            "checkpoint_id": checkpoint,
            "file_count": count,
        }
        for (category, task, checkpoint), count in sorted(counts.items())
    ]
    repair = isolation_manifest.setdefault("repair", {})
    repair["late_isolated_file_count"] = repair.get("late_isolated_file_count", 0) + len(plan)
    old_grouped = repair.get("late_isolated_by_package_category", [])
    combined = Counter(
        {
            (row["category"], row["task_id"], row["checkpoint_id"]): row["file_count"]
            for row in old_grouped
        }
    )
    combined.update(
        {
            (row["category"], row["task_id"], row["checkpoint_id"]): row["file_count"]
            for row in grouped_moves
        }
    )
    grouped_moves = [
        {
            "category": category,
            "task_id": task,
            "checkpoint_id": checkpoint,
            "file_count": count,
        }
        for (category, task, checkpoint), count in sorted(combined.items())
    ]
    repair["late_isolated_by_package_category"] = grouped_moves
    repair["embedded_restricted_data_isolated"] = {
        "record_count": len(embedded_records),
        "fragment_and_record_count": sum(embedded_counts.values()),
        "by_category": {
            category: sum(
                count
                for (item_category, _task, _checkpoint), count in embedded_counts.items()
                if item_category == category
            )
            for category in CATEGORY_ROOTS
        },
    }
    isolation_manifest["isolated_roots"] = {
        "upstream_hidden_tests": "../hidden_tests/rq1_full_36x1/<task>/C<n>/<test-relative-path>/",
        "future_checkpoint_test_data": "../future_tests/rq1_full_36x1/<task>/C<n>/<test-relative-path>/",
        "file_backup_errors": "../excluded_tests/rq1_full_36x1/file_backup/C<n>/<test-relative-path>/",
    }
    isolation_manifest["policy"] = [
        "base_tests in each checkpoint package contains no files or embedded test records from upstream hidden groups",
        "hidden and future case constants, test functions, and JSON records are archived outside generator inputs",
        "file_backup errors groups are excluded from generator inputs per the current team protocol",
        "base_tests contains no data from checkpoint numbers later than the package checkpoint, including checkpoint-labeled fixture files",
        "empty hidden/errors group directories may remain so upstream test loaders collect zero excluded cases",
        "package.json base_tests_sha256 and the isolation manifest filtered hashes are computed from the sanitized trees",
    ]
    move_manifest["remedial_moves"] = grouped_moves
    move_manifest["embedded_remedial_moves"] = embedded_records
    manifest_path.write_text(
        json.dumps(isolation_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    moves_path.write_text(
        json.dumps(move_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("Moved restricted test data to isolated roots and refreshed package and manifest hashes.")


if __name__ == "__main__":
    main()

"""Freeze the preregistered eligibility set and paired-generation dispatch plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "data" / "checkpoints" / "checkpoint_packages_rq1_full_36x1"
RESULTS_ROOT = ROOT / "artifacts" / "results" / "rq1_full_36x1"
POOL_PATH = ROOT / "config" / "controlled_pairwise_mutant_pool_v1.json"
BASELINE_AUDIT = (
    ROOT / "artifacts" / "results" / "rq1_full_36x1"
    / "c1_base_audit_isolation_revalidated_20261019" / "c1_base_suite_audit.json"
)
OUT_ROOT = ROOT / "artifacts" / "results" / "controlled_pairwise_v1_20261019"
TASKS = ("cfgpipe", "code_search", "eve_industry")
MUTATION_RUN_NAME = "mutmut_direct_revalidated_20261019"
DISPATCH_SEED = 20261019


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    return digest(path.read_bytes())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def hash_tree(root: Path) -> str:
    rows = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rows.append({"path": path.relative_to(root).as_posix(), "sha256": file_digest(path)})
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return digest(payload)


def hash_legal_package_inputs(package_dir: Path, package: dict[str, Any]) -> str:
    roots = [
        ("specification", package_dir / package["specification_path"]),
        ("workspace", package_dir / package["workspace_path"]),
        ("base_tests", package_dir / package["base_tests_path"]),
    ]
    rows = [{"path": "package.json", "sha256": file_digest(package_dir / "package.json")}]
    for name, root in roots:
        if root.is_file():
            rows.append({"path": name, "sha256": file_digest(root)})
            continue
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            rows.append({"path": f"{name}/{path.relative_to(root).as_posix()}", "sha256": file_digest(path)})
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return digest(payload)


def find_matching_runtime(task: str, package: dict[str, Any]) -> Path:
    task_root = ROOT / "tmp" / "checkpoint-runtime" / task
    matches: list[Path] = []
    for manifest_path in task_root.glob("*/runtime_manifest.json"):
        manifest = load_json(manifest_path)
        if (
            manifest.get("task_id") != task
            or manifest.get("checkpoint_id") != "C1"
            or manifest.get("base_tests_sha256") != package["base_tests_sha256"]
        ):
            continue
        stage = manifest_path.parent
        package_workspace = PACKAGE_ROOT / task / "C1" / package["workspace_path"]
        source_files = sorted(package_workspace.rglob("*.py"))
        source_matches = all(
            (stage / source.relative_to(package_workspace)).is_file()
            and file_digest(source) == file_digest(stage / source.relative_to(package_workspace))
            for source in source_files
        )
        if source_files and source_matches:
            matches.append(stage)
    if not matches:
        raise ValueError(f"no staged C1 runtime matches the checkpoint source and base tests for {task}")
    return sorted(matches, key=lambda path: path.name)[-1]


def ensure_identical_or_write(path: Path, value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise FileExistsError(f"refusing to overwrite a different frozen artifact: {path}")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(encoded, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=OUT_ROOT)
    args = parser.parse_args()
    output_root = args.output_root.resolve()
    if not output_root.is_relative_to((ROOT / "artifacts" / "results").resolve()):
        raise ValueError("output root must remain under artifacts/results")

    pool = load_json(POOL_PATH)
    if pool.get("schema_version") != "controlled-pairwise-mutant-pool/v1":
        raise ValueError("unsupported eligibility-pool version")
    entries = pool.get("mutants")
    if not isinstance(entries, list) or not (15 <= len(entries) <= 20):
        raise ValueError("the fixed representative pool must contain 15–20 mutants")
    selected_ids = [row.get("mutant_id") for row in entries]
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("eligibility pool contains duplicate mutant IDs")
    if {row.get("task_id") for row in entries} - set(TASKS):
        raise ValueError("eligibility pool references an unsupported task")

    audit = load_json(BASELINE_AUDIT)
    audit_rows = audit.get("records")
    if not isinstance(audit_rows, list):
        raise ValueError("base-suite audit has no records array")
    audit_by_task = {
        row.get("task_id"): row
        for row in audit_rows
        if row.get("checkpoint_id") == "C1" and row.get("task_id") in TASKS
    }
    if set(audit_by_task) != set(TASKS):
        raise ValueError("repeated C1 baseline audit is missing one or more experiment tasks")

    task_data: dict[str, dict[str, Any]] = {}
    decisions_by_task: dict[str, list[dict[str, Any]]] = {}
    selected_by_task: dict[str, list[dict[str, Any]]] = {}
    redundant_by_id = {row["mutant_id"]: row for row in pool.get("redundant_behavior_variants", [])}
    excluded_by_id = {row["mutant_id"]: row for row in pool.get("excluded_mutant_families", [])}

    for task in TASKS:
        package_dir = PACKAGE_ROOT / task / "C1"
        package = load_json(package_dir / "package.json")
        audit_process = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "validate_checkpoint_package.py"), "--package", str(package_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        if audit_process.returncode != 0:
            raise ValueError(f"checkpoint package preflight failed for {task}:\n{audit_process.stdout}\n{audit_process.stderr}")

        baseline = audit_by_task[task]
        if baseline.get("status") not in {"stable_pass", "stable_with_failures"} or baseline.get("unstable_test_ids"):
            raise ValueError(f"baseline is not stable for {task}: {baseline.get('status')}")

        mutation_dir = RESULTS_ROOT / task / "C1" / MUTATION_RUN_NAME
        records_path = mutation_dir / "mutant_records.jsonl"
        summary_path = mutation_dir / "mutation_summary.json"
        records = load_jsonl(records_path)
        summary = load_json(summary_path)
        if summary.get("format") != "rq1-mutmut-export/v1" or summary.get("complete") is not True:
            raise ValueError(f"mutation record set is incomplete for {task}")
        if len(records) != summary.get("mutant_count"):
            raise ValueError(f"mutation record count mismatch for {task}")
        survivors = {
            row["mutant_id"]: row
            for row in records
            if row.get("normalized_status") == "survived"
        }
        expected_survivors = summary.get("status_counts", {}).get("survived")
        if len(survivors) != expected_survivors:
            raise ValueError(f"survivor count mismatch for {task}")

        task_pool = [row for row in entries if row["task_id"] == task]
        for row in task_pool:
            mutant_id = row["mutant_id"]
            survivor = survivors.get(mutant_id)
            if survivor is None:
                raise ValueError(f"selected mutant is not a validated survivor: {mutant_id}")
            classification = next(
                item for item in load_jsonl(mutation_dir / "direct_mutant_classification.jsonl")
                if item.get("mutant_id") == mutant_id
            )
            if classification.get("direct_status") != "survived":
                raise ValueError(f"selected mutant did not survive direct revalidation: {mutant_id}")

        selected_map = {row["mutant_id"]: row for row in task_pool}
        decisions = []
        for mutant_id in sorted(survivors):
            if mutant_id in selected_map:
                item = selected_map[mutant_id]
                decision = {
                    "mutant_id": mutant_id,
                    "status": "eligible",
                    "rationale": item["specification_basis"],
                    "gap_ids": [],
                }
            elif mutant_id in redundant_by_id:
                item = redundant_by_id[mutant_id]
                decision = {
                    "mutant_id": mutant_id,
                    "status": "deferred",
                    "rationale": item["rationale"],
                    "gap_ids": [],
                }
            elif mutant_id in excluded_by_id:
                item = excluded_by_id[mutant_id]
                decision = {
                    "mutant_id": mutant_id,
                    "status": item["status"],
                    "rationale": item["rationale"],
                    "gap_ids": [],
                }
            else:
                decision = {
                    "mutant_id": mutant_id,
                    "status": "deferred",
                    "rationale": "Outside the preregistered 16-mutant, behavior-family sample; this is a scope exclusion and does not claim the survivor is invalid or equivalent.",
                    "gap_ids": [],
                }
            decisions.append(decision)

        task_data[task] = {
            "package_dir": package_dir,
            "package": package,
            "mutation_dir": mutation_dir,
            "records_path": records_path,
            "summary_path": summary_path,
            "records": records,
            "survivors": survivors,
            "runtime_dir": find_matching_runtime(task, package),
            "baseline": baseline,
        }
        decisions_by_task[task] = decisions
        selected_by_task[task] = task_pool

    rng = random.Random(DISPATCH_SEED)
    order_labels = ["vanilla_first"] * (len(entries) // 2)
    order_labels += ["mutation_guided_first"] * (len(entries) - len(order_labels))
    rng.shuffle(order_labels)
    order_by_id = {row["mutant_id"]: order_labels[i] for i, row in enumerate(sorted(entries, key=lambda r: r["mutant_id"]))}

    triage_root = output_root / "triage"
    target_triage_root = output_root / "triage_by_target"
    for task, data in task_data.items():
        task_decisions = decisions_by_task[task]
        triage = {
            "schema_version": "rq1-mutant-triage/v1",
            "task_id": task,
            "trajectory_id": data["package"]["trajectory_id"],
            "checkpoint_id": "C1",
            "mutation_record_set_sha256": file_digest(data["records_path"]),
            "reviewed_at": "2026-10-19",
            "review_basis": "Pre-generation controlled-pairwise eligibility rubric in docs/controlled_pairwise_experiment_protocol_v1.md and config/controlled_pairwise_mutant_pool_v1.json; all direct survivors are classified, with unselected survivors explicitly deferred.",
            "decisions": task_decisions,
        }
        ensure_identical_or_write(triage_root / f"{task}.json", triage)

        task_survivors = data["survivors"]
        for selected in selected_by_task[task]:
            target_id = selected["mutant_id"]
            target_decisions = []
            for mutant_id in sorted(task_survivors):
                if mutant_id == target_id:
                    d = {
                        "mutant_id": target_id,
                        "status": "eligible",
                        "rationale": selected["specification_basis"],
                        "gap_ids": [],
                    }
                else:
                    d = {
                        "mutant_id": mutant_id,
                        "status": "deferred",
                        "rationale": f"This paired call targets only {target_id}; no other mutant information is supplied to the generator.",
                        "gap_ids": [],
                    }
                target_decisions.append(d)
            target_triage = {**triage, "decisions": target_decisions}
            slug = re.sub(r"[^A-Za-z0-9_.-]", "_", target_id)
            ensure_identical_or_write(target_triage_root / task / f"{slug}.json", target_triage)

    sample_rows = []
    for item in sorted(entries, key=lambda row: row["mutant_id"]):
        task = item["task_id"]
        data = task_data[task]
        target_slug = re.sub(r"[^A-Za-z0-9_.-]", "_", item["mutant_id"])
        sample_rows.append({
            **item,
            "pair_id": item["mutant_id"],
            "checkpoint_id": "C1",
            "source_package": str(data["package_dir"].relative_to(ROOT).as_posix()),
            "runtime_dir": str(data["runtime_dir"].relative_to(ROOT).as_posix()),
            "mutant_records": str(data["records_path"].relative_to(ROOT).as_posix()),
            "mutant_summary": str(data["summary_path"].relative_to(ROOT).as_posix()),
            "target_triage": str((target_triage_root / task / f"{target_slug}.json").relative_to(ROOT).as_posix()),
            "base_suite_audit": str(BASELINE_AUDIT.relative_to(ROOT).as_posix()),
            "method_order": order_by_id[item["mutant_id"]],
            "mutant_record_set_sha256": file_digest(data["records_path"]),
            "base_tests_sha256": data["package"]["base_tests_sha256"],
            "workspace_revision": data["package"]["workspace_revision"],
        })

    manifest = {
        "schema_version": "controlled-pairwise-sample/v1",
        "experiment_id": pool["experiment_id"],
        "protocol_path": "docs/controlled_pairwise_experiment_protocol_v1.md",
        "eligibility_pool_path": str(POOL_PATH.relative_to(ROOT).as_posix()),
        "eligibility_pool_sha256": file_digest(POOL_PATH),
        "sample_policy": pool["sampling_unit"],
        "selected_mutant_count": len(sample_rows),
        "random_seed_for_call_order_only": DISPATCH_SEED,
        "generation_conditions": ["vanilla_llm", "mutation_guided"],
        "generator_input_policy": "Vanilla receives C1 spec/source/base tests. Guided receives the same plus one selected target mutant ID and diff. Neither receives triage rationale, hidden/future data, later checkpoints, or regression outcomes.",
        "tasks": {
            task: {
                "package": str(data["package_dir"].relative_to(ROOT).as_posix()),
                "workspace_revision": data["package"]["workspace_revision"],
                "base_tests_sha256": data["package"]["base_tests_sha256"],
                "mutant_record_set_sha256": file_digest(data["records_path"]),
                "mutant_summary_sha256": file_digest(data["summary_path"]),
                "source_package_sha256": hash_legal_package_inputs(data["package_dir"], data["package"]),
                "runtime_dir": str(data["runtime_dir"].relative_to(ROOT).as_posix()),
                "base_suite_status": data["baseline"].get("status"),
                "stable_baseline_fail_ids": data["baseline"].get("stable_fail_test_ids", []),
            }
            for task, data in task_data.items()
        },
        "pairs": sample_rows,
    }
    ensure_identical_or_write(output_root / "sample_manifest.json", manifest)

    summary = {
        "experiment_id": pool["experiment_id"],
        "selected_mutants": len(sample_rows),
        "task_counts": {task: sum(row["task_id"] == task for row in sample_rows) for task in TASKS},
        "survivor_counts": {task: len(data["survivors"]) for task, data in task_data.items()},
        "eligible_sample_counts": {task: len(selected_by_task[task]) for task in TASKS},
        "baseline_statuses": {task: data["baseline"].get("status") for task, data in task_data.items()},
        "manifest": str((output_root / "sample_manifest.json").relative_to(ROOT).as_posix()),
    }
    ensure_identical_or_write(output_root / "preparation_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error

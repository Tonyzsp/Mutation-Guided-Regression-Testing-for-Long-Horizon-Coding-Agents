"""Assemble blinded, task-level MG candidate-review inputs for recovery v2."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "artifacts" / "results"
V2_ROOT = RESULTS / "rq1_recovery_v2"
SUMMARY_PATH = V2_ROOT / "generation_call_summary.json"
AMENDMENT_PATH = ROOT / "config" / "rq1_recovery_v2_mg_only_amendment.json"
AMENDMENT_LOCK_PATH = ROOT / "config" / "rq1_recovery_v2_mg_only_amendment.lock.json"
BATCH_PATH = ROOT / "config" / "rq1_recovery_v2_batch.json"
STUDY_BUDGET_PATH = ROOT / "config" / "rq1_recovery_v2_study_budget.json"
GENERATION_BUDGET_PATH = ROOT / "config" / "generation_budget_rq1_recovery_v2_call.json"
SAFE_NAME = re.compile(r"test_[A-Za-z0-9_]+\.py$")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def active_call_dir(task: str, slot: int) -> Path | None:
    logical = V2_ROOT / task / "C1" / "mutation_guided" / f"call_{slot:02d}"
    if (logical / "generation_run.json").is_file():
        return logical
    repaired = logical / "model_invocation_after_preflight_fix"
    if (repaired / "generation_run.json").is_file():
        return repaired
    return None


def prepare() -> dict[str, Any]:
    amendment = read_json(AMENDMENT_PATH)
    amendment_lock = read_json(AMENDMENT_LOCK_PATH)
    amendment_sha = sha256_file(AMENDMENT_PATH)
    if amendment_lock.get("sha256") != amendment_sha:
        raise ValueError("MG-only amendment differs from its frozen lock")
    if amendment.get("mg_generation", {}).get("methods") != ["mutation_guided"]:
        raise ValueError("the recovery amendment is not MG-only")

    summary = read_json(SUMMARY_PATH)
    if summary.get("scope_amendment_sha256") != amendment_sha:
        raise ValueError("generation summary does not match the MG-only amendment")
    if summary.get("methods") != ["mutation_guided"]:
        raise ValueError("generation summary contains a non-MG method")
    tasks = amendment["mg_generation"]["task_ids"]
    if set(tasks) != {row["task_id"] for row in summary["records"]}:
        raise ValueError("generation summary task set differs from the frozen MG cohort")
    budget = read_json(STUDY_BUDGET_PATH)
    batch = read_json(BATCH_PATH)
    max_candidates = int(batch["generation"]["candidate_files_per_task_method_cap"])
    candidate_limit_per_call = int(read_json(GENERATION_BUDGET_PATH)["budget"]["max_candidates"])

    summary_by_slot = {
        (row["task_id"], int(row.get("slot", str(row.get("call_slot", "call_0")).rsplit("_", 1)[-1]))): row
        for row in summary["records"]
    }
    task_rows: list[dict[str, Any]] = []
    for task in tasks:
        bundle = V2_ROOT / task / "C1" / "mutation_guided" / "candidate_review_bundle"
        candidates: list[dict[str, Any]] = []
        mapping: list[dict[str, Any]] = []
        slot_status: list[dict[str, Any]] = []
        for slot in (1, 2):
            row = summary_by_slot[(task, slot)]
            active = active_call_dir(task, slot)
            slot_status.append({
                "slot": slot,
                "status": row["status"],
                "model_calls": row.get("calls", 0),
                "candidate_count": row.get("candidate_count", 0),
                "active_call_directory": str(active.relative_to(ROOT).as_posix()) if active else None,
            })
            if not row.get("candidate_count"):
                continue
            if active is None or not (active / "candidate_response.json").is_file():
                raise FileNotFoundError(f"{task}/call_{slot:02d}: summary reports candidates but response is missing")
            manifest = read_json(active / "input_manifest.json")
            if manifest.get("recovery_v2_scope_amendment", {}).get("sha256") != amendment_sha:
                raise ValueError(f"{task}/call_{slot:02d}: call manifest lacks the frozen MG-only amendment")
            response_path = active / "candidate_response.json"
            response = read_json(response_path)
            tests = response.get("tests")
            if not isinstance(tests, list) or len(tests) != int(row["candidate_count"]):
                raise ValueError(f"{task}/call_{slot:02d}: response count differs from generation summary")
            if len(tests) > candidate_limit_per_call:
                raise ValueError(f"{task}/call_{slot:02d}: per-call candidate cap exceeded")
            for candidate_index, candidate in enumerate(tests, 1):
                original_name = candidate.get("relative_path")
                if not isinstance(original_name, str) or SAFE_NAME.fullmatch(original_name) is None:
                    raise ValueError(f"{task}/call_{slot:02d}: unsafe candidate path {original_name!r}")
                renamed = f"test_v2call{slot:02d}_{original_name[5:]}"
                item = dict(candidate)
                item["relative_path"] = renamed
                candidates.append(item)
                mapping.append({
                    "review_candidate_path": renamed,
                    "source_candidate_path": original_name,
                    "source_call_slot": f"call_{slot:02d}",
                    "source_call_directory": str(active.relative_to(ROOT).as_posix()),
                    "source_candidate_response_sha256": sha256_file(response_path),
                    "candidate_index_in_source_response": candidate_index,
                })
        if len(candidates) > max_candidates:
            raise ValueError(f"{task}: combined candidate cap exceeded ({len(candidates)} > {max_candidates})")

        response = {"tests": candidates}
        if candidates:
            response["assessment"] = "Recovery-v2 MG candidates combined by independent generation slot for a single blinded correctness review."
        candidate_response_path = bundle / "candidate_response.json"
        map_path = bundle / "candidate_path_map.json"
        if bundle.exists():
            existing = read_json(candidate_response_path)
            existing_map = read_json(map_path)
            if existing != response or existing_map.get("mapping") != mapping:
                raise FileExistsError(f"{bundle} already exists with different review inputs")
        else:
            bundle.mkdir(parents=True)
            write_json(candidate_response_path, response)
            write_json(map_path, {"task_id": task, "mapping": mapping})
        manifest_path = bundle / "review_input_manifest.json"
        manifest = {
            "format": "rq1-recovery-v2-mg-candidate-review-input/v1",
            "task_id": task,
            "study_budget_id": budget["experiment_id"],
            "study_budget_sha256": sha256_file(STUDY_BUDGET_PATH),
            "amendment_sha256": amendment_sha,
            "generation_summary_sha256": sha256_file(SUMMARY_PATH),
            "candidate_count": len(candidates),
            "candidate_response_sha256": sha256_file(candidate_response_path),
            "candidate_path_map_sha256": sha256_file(map_path),
            "slots": slot_status,
            "model_input_policy": "C1 specification and legal base tests are supplied by the locked reviewer; no mutant diffs, method label, hidden/future/excluded tests, or later-checkpoint evidence are supplied.",
        }
        if manifest_path.exists() and read_json(manifest_path) != manifest:
            raise FileExistsError(f"{manifest_path} differs from the current frozen inputs")
        write_json(manifest_path, manifest)

        if candidates:
            review_path = bundle / "correctness_review.json"
            if review_path.is_file():
                review = read_json(review_path)
                reviewed = {entry.get("candidate_path") for entry in review.get("reviews", [])}
                if reviewed != {item["relative_path"] for item in candidates}:
                    raise ValueError(f"{task}: existing review does not cover the frozen bundle")
            else:
                review = None
        else:
            review = {"reviews": []}
            if not (bundle / "correctness_review.json").exists():
                write_json(bundle / "correctness_review.json", review)

        if review is not None:
            review_by_path = {item["candidate_path"]: item for item in review.get("reviews", [])}
            for slot in (1, 2):
                active = active_call_dir(task, slot)
                row = summary_by_slot[(task, slot)]
                if not row.get("candidate_count") or active is None:
                    continue
                subset: list[dict[str, Any]] = []
                for link in mapping:
                    if link["source_call_slot"] != f"call_{slot:02d}":
                        continue
                    reviewed_item = review_by_path[link["review_candidate_path"]]
                    local_item = dict(reviewed_item)
                    local_item["candidate_path"] = link["source_candidate_path"]
                    subset.append(local_item)
                local_review_path = active / "correctness_review.json"
                if local_review_path.exists():
                    if read_json(local_review_path) != {"reviews": subset}:
                        raise FileExistsError(f"{local_review_path} already contains a different review")
                else:
                    write_json(local_review_path, {"reviews": subset})
                link_path = active / "candidate_review_link.json"
                link_manifest = {
                    "format": "rq1-recovery-v2-mg-candidate-review-link/v1",
                    "task_id": task,
                    "source_call_slot": f"call_{slot:02d}",
                    "review_bundle_directory": str(bundle.relative_to(ROOT).as_posix()),
                    "review_bundle_manifest_sha256": sha256_file(manifest_path),
                    "aggregate_review_sha256": sha256_file(bundle / "correctness_review.json"),
                    "local_review_sha256": sha256_file(local_review_path),
                    "candidate_path_map_sha256": sha256_file(map_path),
                }
                if link_path.exists() and read_json(link_path) != link_manifest:
                    raise FileExistsError(f"{link_path} already contains a different review link")
                write_json(link_path, link_manifest)
        task_rows.append({
            "task_id": task,
            "candidate_count": len(candidates),
            "review_bundle_directory": str(bundle.relative_to(ROOT).as_posix()),
            "review_status": "pending" if candidates and not (bundle / "correctness_review.json").is_file() else "ready_or_complete",
        })

    result = {
        "format": "rq1-recovery-v2-mg-review-bundle-summary/v1",
        "tasks": len(tasks),
        "tasks_with_candidates": sum(row["candidate_count"] > 0 for row in task_rows),
        "candidate_count": sum(row["candidate_count"] for row in task_rows),
        "review_budget_max_calls": int(budget["candidate_review"]["max_total_calls"]),
        "records": task_rows,
    }
    write_json(V2_ROOT / "mg_review_bundle_summary.json", result)
    return result


if __name__ == "__main__":
    print(json.dumps(prepare(), ensure_ascii=False, indent=2))

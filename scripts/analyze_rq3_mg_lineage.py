"""Join MG survivors, generation prompts, retained tests, target kills, and later events."""

from __future__ import annotations

import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from validate_and_freeze_tests import filesystem_path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_mutant_family_triage import (  # noqa: E402
    extract_changed_lines,
    inferred_edit_kind,
    parse_changed_ast,
)


ROOT = Path(__file__).resolve().parents[1]
V1_ROOT = ROOT / "artifacts/results/rq1_full_36x1"
V2_ROOT = ROOT / "artifacts/results/rq1_recovery_v2"
V1_SUFFIX = "mutation_guided_formal_v1_baseline_linked_20261117"
V1_EVENTS = V1_ROOT / "eval_guided_only_formal_v1_20261117/guided_judge/mutation_guided_confirmed_events.json"
V2_RECONCILIATION = V2_ROOT / "mg_target_kill_reconciliation_summary.json"
V2_GENERATION = V2_ROOT / "generation_call_summary.json"
V2_COMPOSITE = V2_ROOT / "mg_composite_suite_summary.json"
V2_JUDGE = V2_ROOT / "eval_mg_composite_v2/guided_judge/mg_v2_adjudication_summary.json"
DOMAIN_PATH = ROOT / "config/rq3_task_domain_taxonomy_v1.json"
OUTPUT_ROOT = V2_ROOT / "rq3_mg_combined_lineage_20261118"


def read_json(path: Path) -> Any:
    with open(filesystem_path(path), "r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with open(filesystem_path(path), "r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def relative_filename(path_value: str) -> str:
    module_path = path_value.replace("\\", "/").split("::", 1)[0]
    return Path(module_path).name


def target_function(mutant_id: str) -> str:
    value = mutant_id.rsplit(".", 1)[-1]
    return value.split("__mutmut_", 1)[0]


def infer_mutation_edit_kind(diff: str) -> str:
    old_lines, new_lines = extract_changed_lines(diff)
    return inferred_edit_kind(
        parse_changed_ast(old_lines),
        parse_changed_ast(new_lines),
        old_lines,
        new_lines,
    )


def load_mutant_records(path_value: str) -> dict[str, dict[str, Any]]:
    path = Path(path_value)
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        raise FileNotFoundError(f"mutant record bundle is missing: {path}")
    return {row["mutant_id"]: row for row in read_jsonl(path)}


def main() -> int:
    domains = read_json(DOMAIN_PATH)
    domain_for_task: dict[str, str] = {}
    for domain, tasks in domains["domains"].items():
        for task in tasks:
            if task in domain_for_task:
                raise ValueError(f"task has more than one primary domain: {task}")
            domain_for_task[task] = domain

    unit_manifest = read_json(ROOT / "config/rq1_full_36x1_candidate_units.json")
    tasks = sorted(unit["task_id"] for unit in unit_manifest["units"])
    if set(domain_for_task) != set(tasks) or len(tasks) != 36:
        raise ValueError("RQ3 task-domain taxonomy must label each of the 36 C1 tasks exactly once")

    composite_summary = read_json(V2_COMPOSITE)
    v2_reconciliation = read_json(V2_RECONCILIATION)
    v2_generation = read_json(V2_GENERATION)
    v1_event_ledger = read_json(V1_EVENTS)
    v2_judge = read_json(V2_JUDGE)
    v2_slots = {(row["task_id"], row["call_slot"]): row for row in v2_reconciliation["records"]}
    v2_call_summary = {
        (row["task_id"], f"call_{int(row['slot']):02d}"): row
        for row in v2_generation["records"]
    }

    # Load canonical model-call records. Recovery call-summary rows are the
    # authority for which slots were actually run; older orphaned log_query
    # files are intentionally not included in the v2 cohort.
    prompt_runs: dict[tuple[str, str, str], dict[str, Any]] = {}
    v1_task_runs: dict[str, dict[str, Any]] = {}
    v1_candidate_decisions: Counter[str] = Counter()
    v1_hint_appearances = 0
    v1_calls = 0
    v1_candidates = 0
    v1_mutants: dict[tuple[str, str], dict[str, Any]] = {}
    v1_validation_by_test: dict[tuple[str, str], dict[str, Any]] = {}

    for task in tasks:
        task_dir = V1_ROOT / task / "C1" / V1_SUFFIX
        generation = read_json(task_dir / "generation_run.json")
        validation_rows = read_jsonl(task_dir / "validation.jsonl")
        mutation_records = load_mutant_records(generation["mutation_record_set_path"])
        for mutant_id, mutant in mutation_records.items():
            v1_mutants[task, mutant_id] = mutant
        for row in validation_rows:
            v1_candidate_decisions[str(row.get("status", "unknown"))] += 1
            v1_validation_by_test[task, relative_filename(row["candidate_path"])] = row

        actual = generation.get("actual_usage", {})
        calls = int(actual.get("calls", 0))
        hints = list(generation.get("guided_mutant_ids", []))
        v1_task_runs[task] = generation
        v1_calls += calls
        v1_candidates += int(generation.get("candidate_count", 0))
        if calls:
            v1_hint_appearances += len(hints)
            prompt_runs[task, "v1", "task_prompt"] = {
                "task_id": task,
                "domain": domain_for_task[task],
                "version": "v1",
                "prompt_slot": "task_prompt",
                "model_calls": calls,
                "candidate_count": int(generation.get("candidate_count", 0)),
                "eligible_survivor_count": int(generation.get("eligible_mutant_count", 0)),
                "survivor_hint_count": len(hints),
                "survivor_hint_mutant_ids": hints,
                "retained_test_count": int(generation.get("retained_count", 0)),
                "budget_id": generation.get("budget_id"),
            }

    # Read canonical v2 generation runs for call-summary slots with calls > 0.
    v2_hint_appearances = 0
    v2_calls = 0
    v2_candidates = 0
    v2_mutants: dict[tuple[str, str], dict[str, Any]] = {}
    v2_generation_by_slot: dict[tuple[str, str], dict[str, Any]] = {}
    v2_candidate_decisions: Counter[str] = Counter()
    for (task, slot), slot_summary in v2_call_summary.items():
        calls = int(slot_summary.get("calls", 0))
        v2_candidates += int(slot_summary.get("candidate_count", 0))
        if not calls:
            continue
        slot_dir = V2_ROOT / task / "C1" / "mutation_guided" / slot
        run_paths = list(slot_dir.rglob("generation_run.json"))
        matches: list[tuple[Path, dict[str, Any]]] = []
        for run_path in run_paths:
            run = read_json(run_path)
            if run.get("task_id") == task and int(run.get("candidate_count", -1)) == int(slot_summary.get("candidate_count", -2)):
                matches.append((run_path, run))
        if len(matches) != 1:
            raise ValueError(f"{task}/{slot}: expected one canonical generation record, found {len(matches)}")
        run_path, generation = matches[0]
        hints = list(generation.get("guided_mutant_ids", []))
        if len(hints) != int(generation.get("eligible_mutant_count", -1)):
            raise ValueError(f"{task}/{slot}: selected hint count disagrees with eligible mutant count")
        v2_calls += calls
        v2_hint_appearances += len(hints)
        v2_generation_by_slot[task, slot] = generation
        prompt_runs[task, "v2", slot] = {
            "task_id": task,
            "domain": domain_for_task[task],
            "version": "v2",
            "prompt_slot": slot,
            "model_calls": calls,
            "candidate_count": int(generation.get("candidate_count", 0)),
            "eligible_survivor_count": int(generation.get("eligible_mutant_count", 0)),
            "survivor_hint_count": len(hints),
            "survivor_hint_mutant_ids": hints,
            "retained_test_count": 0,
            "budget_id": generation.get("budget_id"),
            "generation_record_path": str(run_path.relative_to(ROOT)).replace("\\", "/"),
        }
        for mutant_id, mutant in load_mutant_records(generation["mutation_record_set_path"]).items():
            v2_mutants[task, mutant_id] = mutant

    if v1_calls != 25 or v1_candidates != 32:
        raise ValueError(f"unexpected v1 generation totals: calls={v1_calls}, candidates={v1_candidates}")
    if v2_calls != 45 or v2_candidates != int(v2_generation["records"] and sum(int(r.get("candidate_count", 0)) for r in v2_generation["records"])):
        raise ValueError("v2 generation-call totals do not match the frozen recovery summary")
    if v2_candidates != int(v2_reconciliation["candidate_count"]):
        raise ValueError("v2 candidate count differs from the target-kill reconciliation cohort")

    # Later confirmed regression cases keyed by the generated test file.
    v1_events_by_test: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    v1_mg_events = [
        event for event in v1_event_ledger["events"]
        if "guided_generated_test" in event.get("test_origins", [])
    ]
    v1_mg_only_events = [event for event in v1_mg_events if event.get("detected_by_added_guided_test") and not event.get("also_hit_by_base_test")]
    for event in v1_mg_events:
        for nodeid in event.get("test_nodeids", []):
            v1_events_by_test[event["task_id"], relative_filename(nodeid)].append(event)

    v2_cases_by_test: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for case in v2_judge.get("candidate_cases", []):
        v2_cases_by_test[case["task_id"], case["candidate_test_file"]].append(case)
    v2_events_by_test: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in v2_judge.get("confirmed_events", []):
        for filename in event.get("candidate_test_files", []):
            v2_events_by_test[event["task_id"], filename].append(event)

    lineages: list[dict[str, Any]] = []
    composite_records = {row["task_id"]: row for row in composite_summary["records"]}
    for task in tasks:
        composite_dir = ROOT / composite_records[task]["composite_dir"]
        provenance = read_json(composite_dir / "composite_provenance.json")
        for test in provenance["tests"]:
            filename = test["filename"]
            version = test["version"]
            target_links: list[dict[str, Any]] = []
            if version == "v1":
                validation = v1_validation_by_test.get((task, filename))
                if not validation or validation.get("status") != "retained":
                    raise ValueError(f"{task}/{filename}: composite v1 test lacks a retained validation record")
                if validation.get("content_sha256") != test.get("source_sha256"):
                    raise ValueError(f"{task}/{filename}: composite v1 test content hash mismatch")
                target_rows = validation.get("target_mutant_results", [])
                for target in target_rows:
                    mutant = v1_mutants.get((task, target["mutant_id"]))
                    if not mutant:
                        raise ValueError(f"{task}/{filename}: target mutant record is missing")
                    target_links.append({
                        "mutant_id": target["mutant_id"],
                        "mutant_status": target.get("mutmut_status", mutant.get("normalized_status")),
                        "target_killed": target.get("killed") is True,
                        "repeat_runs": target.get("repeat_runs", 1),
                        "target_function": target_function(target["mutant_id"]),
                        "mutmut_operator": mutant.get("operator"),
                        "inferred_mutation_edit_kind": infer_mutation_edit_kind(mutant.get("diff") or ""),
                        "diff": mutant.get("diff"),
                    })
                prompt_slot = "task_prompt"
                generation = v1_task_runs[task]
                prompt_hints = list(generation.get("guided_mutant_ids", []))
                retention = "retained"
            else:
                slot = str(test.get("call_slot"))
                slot_reconciliation = v2_slots.get((task, slot))
                if not slot_reconciliation:
                    raise ValueError(f"{task}/{filename}: missing v2 target-kill reconciliation slot")
                candidate = next((
                    row for row in slot_reconciliation.get("records", [])
                    if row.get("candidate_content_sha256") == test.get("source_sha256")
                ), None)
                if not candidate:
                    raise ValueError(f"{task}/{filename}: candidate not found in v2 target-kill reconciliation")
                for target in candidate.get("target_checks", []):
                    mutant = v2_mutants.get((task, target["mutant_id"]))
                    if not mutant:
                        raise ValueError(f"{task}/{filename}: v2 target mutant record is missing")
                    target_links.append({
                        "mutant_id": target["mutant_id"],
                        "mutant_status": target.get("mutmut_status", mutant.get("normalized_status")),
                        "target_killed": target.get("killed") is True,
                        "repeat_runs": target.get("repeat_runs"),
                        "target_function": target_function(target["mutant_id"]),
                        "mutmut_operator": mutant.get("operator"),
                        "inferred_mutation_edit_kind": infer_mutation_edit_kind(mutant.get("diff") or ""),
                        "diff": mutant.get("diff"),
                    })
                prompt_slot = slot
                generation = v2_generation_by_slot.get((task, slot))
                if not generation:
                    raise ValueError(f"{task}/{slot}: retained test has no executed generation prompt")
                prompt_hints = list(generation.get("guided_mutant_ids", []))
                retention = candidate.get("decision", test.get("retention_decision"))
                prompt_key = (task, "v2", slot)
                prompt_runs[prompt_key]["retained_test_count"] += 1

            if not target_links or any(link["target_killed"] is not True for link in target_links):
                raise ValueError(f"{task}/{filename}: retained test is missing a verified target-mutant kill")
            v1_hits = v1_events_by_test.get((task, filename), [])
            v2_cases = v2_cases_by_test.get((task, filename), [])
            v2_hits = v2_events_by_test.get((task, filename), [])
            lineage = {
                "task_id": task,
                "domain": domain_for_task[task],
                "version": version,
                "filename": filename,
                "candidate_sha256": test.get("source_sha256"),
                "prompt_slot": prompt_slot,
                "survivor_hint_count": len(prompt_hints),
                "survivor_hint_mutant_ids": prompt_hints,
                "target_mutant_count": len(target_links),
                "target_mutants": target_links,
                "retention_decision": retention,
                "v1_confirmed_event_ids": sorted({event["event_id"] for event in v1_hits}),
                "v1_mg_only_event_ids": sorted({event["event_id"] for event in v1_hits if event.get("detected_by_added_guided_test") and not event.get("also_hit_by_base_test")}),
                "v2_adjudication_cases": [{
                    "case_id": case.get("case_id"),
                    "effective_verdict": case.get("effective_verdict"),
                    "issue_key": case.get("issue_key"),
                    "target_checkpoint": case.get("target_checkpoint"),
                    "v2_event_id": next((event["event_id"] for event in v2_hits if case.get("case_id") in event.get("case_ids", [])), None),
                } for case in v2_cases],
                "v2_confirmed_event_ids": sorted({event["event_id"] for event in v2_hits}),
            }
            lineage["mutmut_operator_metadata_missing"] = all(link["mutmut_operator"] is None for link in target_links)
            lineages.append(lineage)

    lineages.sort(key=lambda row: (row["domain"], row["task_id"], row["version"], row["filename"]))
    retained_mutant_links = [
        (row["task_id"], target["mutant_id"], row["version"])
        for row in lineages for target in row["target_mutants"]
    ]
    distinct_target_mutants = {(task, mutant) for task, mutant, _version in retained_mutant_links}
    target_check_count = len(retained_mutant_links)
    killed_check_count = sum(
        target["target_killed"] is True
        for row in lineages for target in row["target_mutants"]
    )
    operator_values = [target["mutmut_operator"] for row in lineages for target in row["target_mutants"]]
    if len(lineages) != 25 or len(v1_mg_only_events) != 6 or target_check_count != 41 or killed_check_count != 41:
        raise ValueError(
            f"unexpected RQ3 lineage totals: tests={len(lineages)}, MG-only events={len(v1_mg_only_events)}, "
            f"target checks={target_check_count}, killed={killed_check_count}"
        )

    unique_hint_pairs = {
        (run["task_id"], mutant_id)
        for run in prompt_runs.values()
        for mutant_id in run["survivor_hint_mutant_ids"]
    }
    domain_summary: dict[str, dict[str, Any]] = {}
    for domain in domains["domains"]:
        domain_tasks = [task for task in tasks if domain_for_task[task] == domain]
        domain_tests = [row for row in lineages if row["domain"] == domain]
        domain_prompts = [row for row in prompt_runs.values() if row["domain"] == domain]
        domain_events = [event for event in v1_mg_only_events if domain_for_task[event["task_id"]] == domain]
        domain_summary[domain] = {
            "task_count": len(domain_tasks),
            "tasks_with_retained_mg_tests": len({row["task_id"] for row in domain_tests}),
            "v1_candidate_test_count": sum(
                int(row["candidate_count"]) for row in domain_prompts if row["version"] == "v1"
            ),
            "v2_candidate_test_count": sum(
                int(row["candidate_count"]) for row in domain_prompts if row["version"] == "v2"
            ),
            "v1_retained_test_count": sum(row["version"] == "v1" for row in domain_tests),
            "v2_retained_test_count": sum(row["version"] == "v2" for row in domain_tests),
            "executed_prompt_count": sum(int(row["model_calls"]) for row in domain_prompts),
            "survivor_hint_appearances": sum(int(row["survivor_hint_count"]) for row in domain_prompts),
            "distinct_task_mutant_hints": len({
                (run["task_id"], mutant_id)
                for run in domain_prompts
                for mutant_id in run["survivor_hint_mutant_ids"]
            }),
            "target_kill_checks": sum(row["target_mutant_count"] for row in domain_tests),
            "distinct_target_mutants": len({
                (row["task_id"], target["mutant_id"])
                for row in domain_tests for target in row["target_mutants"]
            }),
            "v1_mg_only_confirmed_event_count": len(domain_events),
            "v1_mg_only_confirmed_event_ids": [event["event_id"] for event in domain_events],
        }

    edit_family_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "target_kill_checks": 0,
        "target_survivor_mutants": set(),
        "retained_tests": set(),
        "tasks": set(),
        "v1_mg_only_confirmed_event_ids": set(),
    })
    for row in lineages:
        test_key = (row["task_id"], row["version"], row["filename"])
        for target in row["target_mutants"]:
            family = target["inferred_mutation_edit_kind"].split(":", 1)[0]
            stats = edit_family_stats[family]
            stats["target_kill_checks"] += 1
            stats["target_survivor_mutants"].add((row["task_id"], target["mutant_id"]))
            stats["retained_tests"].add(test_key)
            stats["tasks"].add(row["task_id"])
            if row["version"] == "v1":
                stats["v1_mg_only_confirmed_event_ids"].update(row["v1_mg_only_event_ids"])
    inferred_edit_family_summary = {
        family: {
            "target_kill_checks": stats["target_kill_checks"],
            "distinct_target_survivor_mutants": len(stats["target_survivor_mutants"]),
            "retained_tests_linked": len(stats["retained_tests"]),
            "tasks_linked": len(stats["tasks"]),
            "v1_mg_only_confirmed_event_ids": sorted(stats["v1_mg_only_confirmed_event_ids"]),
            "v1_mg_only_confirmed_event_count": len(stats["v1_mg_only_confirmed_event_ids"]),
        }
        for family, stats in sorted(edit_family_stats.items())
    }

    function_counts = Counter(
        target["target_function"]
        for row in lineages for target in row["target_mutants"]
    )
    task_function_counts = Counter(
        (row["task_id"], target["target_function"])
        for row in lineages for target in row["target_mutants"]
    )
    v2_decision_counts = v2_reconciliation["decision_counts"]
    v2_case_counts = v2_judge["effective_verdict_counts"]
    report = {
        "format": "rq3-mg-lineage-analysis/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Mutation-Guided only; v1 formal cohort plus v2 recovery tests. No Original or Vanilla test lineage is included.",
        "taxonomy": domains,
        "generation_summary": {
            "v1": {
                "executed_model_calls": v1_calls,
                "candidate_tests": v1_candidates,
                "retained_tests": sum(row["version"] == "v1" for row in lineages),
                "retained_tasks": len({row["task_id"] for row in lineages if row["version"] == "v1"}),
                "survivor_hint_appearances": v1_hint_appearances,
                "candidate_decision_counts": dict(v1_candidate_decisions),
            },
            "v2": {
                "executed_model_calls": v2_calls,
                "candidate_tests": v2_candidates,
                "retained_tests": sum(row["version"] == "v2" for row in lineages),
                "retained_tasks": len({row["task_id"] for row in lineages if row["version"] == "v2"}),
                "survivor_hint_appearances": v2_hint_appearances,
                "candidate_decision_counts": v2_decision_counts,
                "unevaluated_or_not_run_slots": Counter(row.get("status") for row in v2_generation["records"] if not int(row.get("calls", 0))),
            },
            "combined_descriptive_totals": {
                "executed_model_calls": v1_calls + v2_calls,
                "candidate_tests": v1_candidates + v2_candidates,
                "retained_tests": len(lineages),
                "retention_rate_percent": round(len(lineages) / (v1_candidates + v2_candidates) * 100.0, 2),
                "survivor_hint_appearances": v1_hint_appearances + v2_hint_appearances,
                "distinct_task_mutant_hints_across_batches": len(unique_hint_pairs),
                "interpretation_note": "v1 and recovery-v2 use distinct budgets and cohorts; combined yield is descriptive and is not a method comparison.",
            },
        },
        "target_mutant_summary": {
            "retained_test_count": len(lineages),
            "target_kill_checks": target_check_count,
            "successful_target_kill_checks": killed_check_count,
            "distinct_target_survivor_mutants": len(distinct_target_mutants),
            "mutmut_operator_field_non_null_target_checks": sum(value is not None for value in operator_values),
            "mutmut_operator_field_missing_target_checks": sum(value is None for value in operator_values),
            "mutmut_operator_analysis_status": "unavailable_from_structured_records; all target-mutant `operator` fields are null",
            "replacement_descriptive_breakdown": "target function parsed from the mutmut mutant ID; this is not a mutmut operator label",
            "target_function_link_counts": dict(sorted(function_counts.items())),
            "task_target_function_link_counts": {f"{task}:{function}": count for (task, function), count in sorted(task_function_counts.items())},
            "inferred_edit_family_method": "Deterministic AST diff classifier already used by the project mutant-family selection pipeline; labels are inferred edit kinds, not native mutmut operators.",
            "inferred_edit_family_summary": inferred_edit_family_summary,
        },
        "later_event_summary": {
            "v1_canonical_confirmed_event_count": int(v1_event_ledger["event_count"]),
            "v1_events_hit_by_added_mg_tests": len(v1_mg_events),
            "v1_mg_only_confirmed_event_count": len(v1_mg_only_events),
            "v1_mg_only_confirmed_event_ids": [event["event_id"] for event in v1_mg_only_events],
            "v1_mg_only_by_task": dict(Counter(event["task_id"] for event in v1_mg_only_events)),
            "v2_effective_case_verdicts": v2_case_counts,
            "v2_batch_internal_confirmed_event_count": int(v2_judge["unique_confirmed_event_count"]),
            "v2_confirmed_event_ids": [event["event_id"] for event in v2_judge.get("confirmed_events", [])],
            "v2_incremental_unique_event_count_after_cross_batch_dedup": 0,
            "cross_batch_note": "The v2 mocked_http case corroborates the v1 generated-test event family; the v2 test_translator cases map to the v1 official-base-test inequality/discovery issue family. v2 adds evidence, not a new unique event in the canonical v1 ledger.",
        },
        "domain_summary": domain_summary,
        "prompt_runs": sorted(prompt_runs.values(), key=lambda row: (row["domain"], row["task_id"], row["version"], row["prompt_slot"])),
        "test_lineage": lineages,
    }

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    (OUTPUT_ROOT / "rq3_mg_lineage_summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    md = [
        "# Mutation-Guided RQ3 lineage analysis (v1 + recovery v2)",
        "",
        "## Coverage and lineage status",
        "",
        "- Combined cohort: **25 retained MG tests across 16/36 tasks** (13 v1 tests and 12 v2 additions).",
        f"- Generation trail: **{v1_calls} v1 + {v2_calls} v2 model calls**, {v1_candidates} + {v2_candidates} candidate files, and {v1_hint_appearances + v2_hint_appearances} survivor-hint appearances across both batches.",
        f"- Retained tests link to **{target_check_count} target-mutant kill checks** across **{len(distinct_target_mutants)} distinct surviving mutants**; all {killed_check_count} recorded checks killed their target mutant.",
        f"- Later confirmed events: v1's canonical ledger contains {v1_event_ledger['event_count']} total episodes; **{len(v1_mg_only_events)} were hit only by MG-added tests**, all in `file_backup` and `mocked_http`. V2's two internal confirmed event clusters add **0 unique episodes** after the existing cross-batch deduplication.",
        "",
        "## Domain breakdown",
        "",
        "Domain labels below are an exploratory manual taxonomy based on the sanitized C1 specification. They are not official benchmark labels.",
        "",
        "| Domain | Tasks | Tasks with MG tests | Candidate tests (v1/v2) | Retained tests (v1/v2) | Survivor hint appearances | Target kill checks | MG-only confirmed v1 events |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for domain, row in domain_summary.items():
        md.append(
            f"| {domain} | {row['task_count']} | {row['tasks_with_retained_mg_tests']} | "
            f"{row['v1_candidate_test_count']}/{row['v2_candidate_test_count']} | "
            f"{row['v1_retained_test_count']}/{row['v2_retained_test_count']} | {row['survivor_hint_appearances']} | "
            f"{row['target_kill_checks']} | {row['v1_mg_only_confirmed_event_count']} |"
        )
    md.extend([
        "",
        "## Survivor-to-test-to-event crosswalk",
        "",
        "| Version | Task | Test | Prompt slot | Hints shown | Target mutants killed | Later confirmed event evidence |",
        "| --- | --- | --- | --- | ---: | --- | --- |",
    ])
    for row in lineages:
        targets = ", ".join(target["mutant_id"] for target in row["target_mutants"])
        events = list(row["v1_mg_only_event_ids"])
        events.extend(row["v2_confirmed_event_ids"])
        events.extend(
            f"{case['case_id']}:{case['effective_verdict']}"
            for case in row["v2_adjudication_cases"]
            if case["effective_verdict"] != "confirmed_regression"
        )
        event_text = ", ".join(sorted(set(events))) or "none"
        md.append(
            f"| {row['version']} | {row['task_id']} | `{row['filename']}` | {row['prompt_slot']} | "
            f"{row['survivor_hint_count']} | {targets} | {event_text} |"
        )
    md.extend([
        "",
        "## Mutmut operator field limitation",
        "",
        f"All **{sum(value is None for value in operator_values)}/{target_check_count}** target-kill records have a null `operator` value in the source mutant records. Exact mutmut-operator totals cannot be reported from this dataset. The JSON includes each target mutant's ID, diff, and target function so the operator can be manually or programmatically reconstructed; target-function counts are provided as a descriptive substitute and must not be labeled as operator counts.",
        "",
        "The next table provides a coarse, deterministic AST edit-family summary using the classifier already used for mutant-family selection; it must not be interpreted as native mutmut operators.",
        "",
        "| Inferred AST edit family | Distinct target mutants | Target kill checks | Retained tests linked | Tasks linked | MG-only confirmed v1 events |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ])
    for family, row in inferred_edit_family_summary.items():
        md.append(
            f"| `{family}` | {row['distinct_target_survivor_mutants']} | {row['target_kill_checks']} | "
            f"{row['retained_tests_linked']} | {row['tasks_linked']} | {row['v1_mg_only_confirmed_event_count']} |"
        )
    md.extend([
        "",
        "## Interpretation limits",
        "",
        "The v1 and v2 candidate-to-retained yields are descriptive; v2 was a recovery batch with a different budget and selected cohort. A generated test killing its targeted survivor demonstrates mutation sensitivity at C1, while the later-event linkage shows whether that test exposed a later confirmed behavior. These facts do not establish that an operator or domain caused regression detection.",
        "",
        "The v2 judge reviewed six earliest-failure cases: three file_backup C3 cases (two expected specification changes, one inconclusive after a consistency conflict), one mocked_http confirmed case, and two test_translator confirmed cases merged into one event. The file_backup C3 conflict remains excluded from confirmed-event counts.",
        "",
        "Machine-readable prompts, test-to-mutant links, diffs, verdicts, and domain totals are in `rq3_mg_lineage_summary.json`. Domain mapping is in `config/rq3_task_domain_taxonomy_v1.json`.",
        "",
    ])
    (OUTPUT_ROOT / "rq3_mg_combined_report.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps({
        "status": "complete_with_operator_metadata_gap",
        "retained_tests": len(lineages),
        "target_kill_checks": target_check_count,
        "distinct_target_mutants": len(distinct_target_mutants),
        "v1_mg_only_confirmed_events": len(v1_mg_only_events),
        "v2_incremental_unique_events": 0,
        "operator_metadata_missing": sum(value is None for value in operator_values),
        "report": str(OUTPUT_ROOT / "rq3_mg_combined_report.md"),
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

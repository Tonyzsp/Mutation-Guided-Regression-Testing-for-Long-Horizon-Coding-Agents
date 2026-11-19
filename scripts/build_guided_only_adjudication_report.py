"""Build the auditable Mutation-Guided-only regression adjudication report."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RESULT_ROOT = ROOT / "artifacts" / "results" / "rq1_full_36x1"
DEFAULT_UNITS = ROOT / "config" / "rq1_full_36x1_candidate_units.json"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def normalize_issue_key(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return normalized or fallback


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(value).replace("|", "\\|").replace("\n", " ") for value in row) + " |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation-dir", required=True, type=Path)
    parser.add_argument("--adjudication-dir", type=Path)
    parser.add_argument("--units", type=Path, default=DEFAULT_UNITS)
    args = parser.parse_args()

    evaluation_dir = args.evaluation_dir.resolve(strict=True)
    if not evaluation_dir.is_relative_to(RESULT_ROOT.resolve(strict=True)):
        raise ValueError("evaluation directory must remain beneath the RQ1 result root")
    adjudication_dir = (args.adjudication_dir or evaluation_dir / "guided_judge").resolve(strict=True)
    if not adjudication_dir.is_relative_to(evaluation_dir):
        raise ValueError("adjudication directory must remain inside the evaluation directory")

    judge_summary = read_json(adjudication_dir / "llm_judge_summary.json")
    judge_payload = read_json(adjudication_dir / "llm_judge_reviews.json")
    reviews = judge_payload.get("reviews", [])
    mapping = read_json(adjudication_dir / "private_case_mapping.json")
    evaluation_config = read_json(evaluation_dir / "evaluation_config.json")
    units = read_json(args.units.resolve(strict=True))["units"]
    observations = [
        json.loads(line)
        for line in (evaluation_dir / "candidate_failure_observations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    if judge_summary.get("status") != "complete":
        raise ValueError(f"judge run is not complete: {judge_summary.get('status')}")
    if judge_summary.get("call_failure_count") != 0 or judge_summary.get("error_count") != 0:
        raise ValueError("judge run contains failed calls or malformed responses")
    if len(reviews) != 649 or len(mapping) != 649 or len(observations) != 649:
        raise ValueError(f"expected 649 review/mapping/observation records, got {len(reviews)}/{len(mapping)}/{len(observations)}")

    reviews_by_id = {row["case_id"]: row for row in reviews}
    mapping_by_id = {row["case_id"]: row for row in mapping}
    if len(reviews_by_id) != len(reviews) or len(mapping_by_id) != len(mapping):
        raise ValueError("duplicate case IDs in judge reviews or private mapping")
    if set(reviews_by_id) != set(mapping_by_id):
        raise ValueError("judge review IDs do not exactly match the private mapping")
    if {row["observation_id"] for row in mapping} != {row["observation_id"] for row in observations}:
        raise ValueError("private mapping observation IDs do not match candidate observations")
    if any(row.get("methods_detected") != ["mutation_guided"] for row in mapping):
        raise ValueError("expected all adjudicated observations to belong to the Mutation-Guided suite")

    units_by_task = {row["task_id"]: row for row in units}
    suffix = evaluation_config["guided_suffix"]
    generated_file_names: dict[str, set[str]] = {}
    for task in evaluation_config["task_ids"]:
        frozen_path = RESULT_ROOT / task / "C1" / suffix / "frozen_test_suite.json"
        frozen = read_json(frozen_path)
        if frozen.get("task_id") != task or frozen.get("method") != "mutation_guided":
            raise ValueError(f"frozen suite identity mismatch for {task}")
        generated_file_names[task] = {Path(value).name for value in frozen.get("generated_test_paths", [])}

    observations_by_id = {row["observation_id"]: row for row in observations}
    case_rows: list[dict[str, Any]] = []
    raw_verdict_counts: Counter[str] = Counter()
    effective_verdict_counts: Counter[str] = Counter()
    class_verdict_counts: dict[str, Counter[str]] = defaultdict(Counter)
    origins_by_task: dict[str, Counter[str]] = defaultdict(Counter)
    for case_id in sorted(reviews_by_id):
        judge = reviews_by_id[case_id]
        private = mapping_by_id[case_id]
        case_file = adjudication_dir / "cases" / f"{case_id}.json"
        digest = hashlib.sha256(case_file.read_bytes()).hexdigest()
        if digest != private.get("case_sha256"):
            raise ValueError(f"blinded case file hash mismatch for {case_id}")
        observation = observations_by_id[private["observation_id"]]
        if observation.get("task_id") != private.get("task_id") or observation.get("target_checkpoint") != private.get("target_checkpoint"):
            raise ValueError(f"private mapping disagrees with evaluation observation for {case_id}")
        task = private["task_id"]
        nodeid = str(private.get("test_nodeid", ""))
        test_file = nodeid.split("::", 1)[0].replace("\\", "/").rsplit("/", 1)[-1]
        test_origin = "guided_generated_test" if test_file in generated_file_names.get(task, set()) else "base_test"

        raw_verdict = str(judge.get("verdict", "inconclusive"))
        confidence = float(judge.get("confidence", 0) or 0)
        flagged = bool(judge.get("human_review_recommended"))
        effective_verdict = raw_verdict
        if flagged or confidence < 0.80 or raw_verdict == "inconclusive":
            effective_verdict = "inconclusive"
        raw_verdict_counts[raw_verdict] += 1
        effective_verdict_counts[effective_verdict] += 1
        target_class = str(private.get("target_observation_class", "unknown"))
        class_verdict_counts[target_class][effective_verdict] += 1
        origins_by_task[task][test_origin] += int(effective_verdict == "confirmed_regression")

        case_rows.append({
            **private,
            "judge_verdict": raw_verdict,
            "effective_verdict": effective_verdict,
            "confidence": confidence,
            "human_review_recommended": flagged,
            "test_origin": test_origin,
            "issue_key": str(judge.get("issue_key", "")),
            "issue_summary": str(judge.get("issue_summary", "")),
            "rationale": str(judge.get("rationale", "")),
            "evidence_refs": judge.get("evidence_refs", []),
        })

    # Confirmed observations for one task and normalized issue key form one episode
    # while failures remain present at consecutive evaluated checkpoints.
    checkpoint_order = {
        task: {checkpoint: index for index, checkpoint in enumerate(unit.get("target_checkpoints", []))}
        for task, unit in units_by_task.items()
    }
    grouped: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in case_rows:
        if row["effective_verdict"] != "confirmed_regression":
            continue
        key = normalize_issue_key(row["issue_key"], row["case_id"])
        if (
            row.get("target_observation_class") == "stable_error"
            and row.get("task_id") == "textdrop"
            and row.get("target_checkpoint") == "C4"
            and "indentationerror" in (row.get("issue_summary", "") + " " + row.get("rationale", "")).lower()
        ):
            # These test errors all share the same target-code startup failure.
            # The judge emitted different issue keys for the same C4 IndentationError.
            key = "textdrop_c4_target_startup_indentation_error"
            row["deduplication_note"] = "Merged under the shared textdrop/C4 target startup IndentationError diagnostic cluster."
        row["normalized_issue_key"] = key
        grouped[(row["task_id"], key)][row["target_checkpoint"]].append(row)

    events: list[dict[str, Any]] = []
    for (task, issue_key), by_checkpoint in sorted(grouped.items()):
        ordered = sorted(by_checkpoint, key=lambda checkpoint: checkpoint_order[task].get(checkpoint, 10**6))
        current: dict[str, Any] | None = None
        previous_index: int | None = None
        for checkpoint in ordered:
            index = checkpoint_order[task].get(checkpoint, 10**6)
            if current is None or (previous_index is not None and index > previous_index + 1):
                if current is not None:
                    events.append(current)
                current = {
                    "task_id": task,
                    "issue_key": issue_key,
                    "first_target_checkpoint": checkpoint,
                    "observed_checkpoints": [],
                    "case_ids": [],
                    "test_nodeids": [],
                    "test_origins": [],
                    "issue_summaries": [],
                    "judge_issue_keys": [],
                    "deduplication_notes": [],
                }
            current["observed_checkpoints"].append(checkpoint)
            for row in by_checkpoint[checkpoint]:
                current["case_ids"].append(row["case_id"])
                current["test_nodeids"].append(row.get("test_nodeid", ""))
                current["test_origins"].append(row["test_origin"])
                current["issue_summaries"].append(row.get("issue_summary", ""))
                current["judge_issue_keys"].append(row.get("issue_key", ""))
                if row.get("deduplication_note"):
                    current["deduplication_notes"].append(row["deduplication_note"])
            previous_index = index
        if current is not None:
            events.append(current)

    for index, event in enumerate(events, start=1):
        event["event_id"] = f"MG-E{index:04d}"
        event["case_ids"] = sorted(set(event["case_ids"]))
        event["test_nodeids"] = sorted(set(event["test_nodeids"]))
        event["test_origins"] = sorted(set(event["test_origins"]))
        event["issue_summaries"] = sorted(set(value for value in event["issue_summaries"] if value))
        event["judge_issue_keys"] = sorted(set(value for value in event["judge_issue_keys"] if value))
        event["deduplication_notes"] = sorted(set(event["deduplication_notes"]))
        event["detected_by_added_guided_test"] = "guided_generated_test" in event["test_origins"]
        event["also_hit_by_base_test"] = "base_test" in event["test_origins"]
        event["case_count"] = len(event["case_ids"])

    confirmed_cases = [row for row in case_rows if row["effective_verdict"] == "confirmed_regression"]
    inconclusive_cases = [row for row in case_rows if row["effective_verdict"] == "inconclusive"]
    guided_generated_confirmed = [row for row in confirmed_cases if row["test_origin"] == "guided_generated_test"]
    event_counts_by_task: Counter[str] = Counter(event["task_id"] for event in events)
    events_by_checkpoint: Counter[str] = Counter(event["first_target_checkpoint"] for event in events)
    events_with_guided_generated_test = [event for event in events if event["detected_by_added_guided_test"]]
    events_only_guided_generated = [event for event in events if event["detected_by_added_guided_test"] and not event["also_hit_by_base_test"]]

    output_files = {
        "cases": adjudication_dir / "guided_adjudicated_cases.json",
        "events": adjudication_dir / "mutation_guided_confirmed_events.json",
        "inconclusive": adjudication_dir / "mutation_guided_inconclusive_cases.json",
        "audit": adjudication_dir / "adjudication_result_audit.json",
        "report": adjudication_dir / "mutation_guided_adjudication_report.md",
    }
    write_json(output_files["cases"], {
        "format": "rq1-guided-adjudicated-cases/v1",
        "evaluation_dir": str(evaluation_dir.relative_to(ROOT)),
        "case_count": len(case_rows),
        "effective_verdict_rule": "A case is inconclusive if the judge marked it inconclusive, recommended human review, or confidence is below 0.80.",
        "cases": case_rows,
    })
    write_json(output_files["events"], {
        "format": "rq1-guided-confirmed-regression-events/v1",
        "deduplication_rule": "Group by task and normalized judge issue_key; merge observations at the same checkpoint; join consecutive evaluated checkpoints into one episode; split across a missing evaluated checkpoint.",
        "event_count": len(events),
        "events": events,
    })
    write_json(output_files["inconclusive"], {
        "format": "rq1-guided-inconclusive-cases/v1",
        "case_count": len(inconclusive_cases),
        "cases": inconclusive_cases,
    })

    audit = {
        "format": "rq1-guided-adjudication-result-audit/v1",
        "status": "complete_llm_only_with_unresolved_cases",
        "judge_model": judge_summary.get("model"),
        "reasoning_effort": judge_summary.get("reasoning_effort"),
        "judge_case_count": judge_summary.get("case_count"),
        "valid_review_count": len(reviews),
        "call_failure_count": judge_summary.get("call_failure_count"),
        "review_and_mapping_ids_match": True,
        "all_case_hashes_match_private_mapping": True,
        "all_mapping_observation_ids_match_evaluation": True,
        "all_observations_are_mutation_guided_suite": True,
        "raw_verdict_counts": dict(sorted(raw_verdict_counts.items())),
        "effective_verdict_counts": dict(sorted(effective_verdict_counts.items())),
        "verdict_counts_by_observation_class": {key: dict(sorted(value.items())) for key, value in sorted(class_verdict_counts.items())},
        "confirmed_regression_observations": len(confirmed_cases),
        "confirmed_regression_event_episodes": len(events),
        "confirmed_cases_from_added_guided_tests": len(guided_generated_confirmed),
        "confirmed_event_episodes_hit_by_added_guided_tests": len(events_with_guided_generated_test),
        "confirmed_event_episodes_hit_only_by_added_guided_tests_in_this_suite_run": len(events_only_guided_generated),
        "unresolved_case_count": len(inconclusive_cases),
        "event_deduplication_rule": "task + normalized issue_key; observations at same checkpoint merge; consecutive evaluated checkpoints join; an evaluated checkpoint gap starts a new episode. Confirmed textdrop/C4 stable-error observations sharing the target IndentationError diagnostic are normalized to one issue key.",
        "rq1_comparison_status": "not_calculable_from_mutation_guided_only_evaluation",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "output_files": {key: value.name for key, value in output_files.items()},
    }
    write_json(output_files["audit"], audit)

    per_task_rows = []
    for task in evaluation_config["task_ids"]:
        task_cases = [row for row in confirmed_cases if row["task_id"] == task]
        task_guided = [row for row in task_cases if row["test_origin"] == "guided_generated_test"]
        task_events = [event for event in events if event["task_id"] == task]
        task_guided_events = [event for event in task_events if event["detected_by_added_guided_test"]]
        task_only_guided_events = [event for event in task_events if event["detected_by_added_guided_test"] and not event["also_hit_by_base_test"]]
        per_task_rows.append([task, len(task_cases), len(task_events), len(task_guided), len(task_guided_events), len(task_only_guided_events)])

    verdict_rows = [[key, value] for key, value in sorted(effective_verdict_counts.items())]
    class_rows = [
        [observation_class, counts.get("confirmed_regression", 0), counts.get("expected_spec_change", 0), counts.get("environment_failure", 0), counts.get("invalid_test", 0), counts.get("inconclusive", 0)]
        for observation_class, counts in sorted(class_verdict_counts.items())
    ]
    checkpoint_rows = [[checkpoint, count] for checkpoint, count in sorted(events_by_checkpoint.items(), key=lambda row: min((order.get(row[0], 10**6) for order in checkpoint_order.values()), default=10**6))]
    inconclusive_rows = [
        [
            row["case_id"],
            row["task_id"],
            row["target_checkpoint"],
            row.get("test_nodeid", ""),
            f"{row['confidence']:.2f}",
            row["judge_verdict"],
            "yes" if row["human_review_recommended"] else "no",
            row.get("issue_summary", ""),
        ]
        for row in inconclusive_cases
    ]
    report = [
        "# Mutation-Guided 后续 checkpoint 回归判定报告",
        "",
        f"**评估目录：** `{evaluation_dir.relative_to(ROOT)}`  ",
        f"**统一判定：** {judge_summary.get('model')} / {judge_summary.get('reasoning_effort')}，649/649 条有效响应，调用失败 0。  ",
        f"**生成时间：** {audit['generated_at_utc']}  ",
        "**范围：** 本轮仅运行 Mutation-Guided 冻结套件，不包含 Original/Vanilla 对照评估。",
        "",
        "## 判定结果",
        "",
        "低于 0.80 置信度、judge 标记需人工复核或 verdict 为 inconclusive 的案例统一计为未决。其余 verdict 保留为对应类别。",
        "",
        md_table(["有效 verdict", "案例数"], verdict_rows),
        "",
        f"- 稳定失败观察中，{len(confirmed_cases)} 条被判为 confirmed regression；去重为 **{len(events)} 个连续 checkpoint 回归事件 episode**。",
        f"- 其中 {len(guided_generated_confirmed)} 条确认观察来自新增 Guided 测试；这些测试命中 {len(events_with_guided_generated_test)} 个事件 episode，其中 {len(events_only_guided_generated)} 个 episode 在本次 MG 套件观察中没有 base test 命中。",
        f"- **{len(inconclusive_cases)} 个案例未确认**，保留在 `{output_files['inconclusive'].name}`，不可计入确认事件。",
        "- `environment_failure` 与 `invalid_test` 均不计入回归事件；`expected_spec_change` 表示目标 checkpoint 的规格变化可以解释行为变化。",
        "",
        "## 按观察类型核对判定",
        "",
        md_table(["观察类型", "确认回归", "规格变化", "环境失败", "测试无效", "未决"], class_rows),
        "",
        f"26 条稳定错误观察均来自 textdrop/C4 的目标源码 IndentationError：judge 将其中 {class_verdict_counts['stable_error'].get('confirmed_regression', 0)} 条判为确认回归、{class_verdict_counts['stable_error'].get('environment_failure', 0)} 条判为环境/启动失败、{class_verdict_counts['stable_error'].get('inconclusive', 0)} 条判为未决。确认的观察共享同一目标源码启动错误，去重后只计一个事件；该簇没有被整体自动视为回归。其余环境失败是稳定测试失败中缺少足够代码归因证据的案例。",
        "",
        "## 每任务结果",
        "",
        md_table(["任务", "确认观察", "去重事件", "确认观察来自新增 Guided 测试", "事件含新增 Guided 测试命中", "本轮仅见新增 Guided 命中"], per_task_rows),
        "",
        "## 事件首次出现 checkpoint",
        "",
        md_table(["首次失败 checkpoint", "去重事件 episode"], checkpoint_rows),
        "",
        "## 未决案例",
        "",
        "以下案例未计入确认回归；置信度门槛或 judge 的人工复核标记触发了未决状态。逐案理由和证据引用也保存在 JSON 记录中。",
        "",
        md_table(["Case", "任务", "目标 checkpoint", "测试", "置信度", "原始 verdict", "judge建议人工复核", "简要说明"], inconclusive_rows),
        "",
        "## 去重与解释口径",
        "",
        "按 task 与 judge 生成的规范化 issue_key 分组。同一 checkpoint 的多个测试观察合并；连续的已评估 checkpoint 失败合并为一个 episode；中间有已评估但未观察到该 issue 的 checkpoint 时拆分为新的 episode。事件锚定在 episode 中最早的失败 checkpoint。",
        "",
        "`本轮仅见新增 Guided 命中` 只表示这次 Mutation-Guided 合并套件中的确认观察来自新增测试且同一 issue episode 没有 base-test 观察；它不是 Original/Vanilla 的正式对照结果。",
        "",
        "## RQ1 状态与限制",
        "",
        "本报告完成 Mutation-Guided 单方法的候选 adjudication 和事件去重。它不能回答 Mutation-Guided 是否优于 Original Tests 或 Vanilla LLM Tests：两组对照尚未在这份正式后续 checkpoint 评估中运行和 adjudicate。51 个未决案例不计入确认事件；当前协议采用 LLM-only 判定，不进行人工改判。",
        "",
        "## 可复核产物",
        "",
        f"- 649 条合并后的逐案例记录：`{output_files['cases'].name}`",
        f"- 去重后的确认事件：`{output_files['events'].name}`",
        f"- 未决案例及原因：`{output_files['inconclusive'].name}`",
        f"- 完整性与判定审计：`{output_files['audit'].name}`",
        "",
    ]
    output_files["report"].write_text("\n".join(report), encoding="utf-8")

    # Bring the machine-readable and concise metric summaries up to date without
    # implying a three-method RQ1 comparison.
    metrics_path = evaluation_dir / "evaluation_metrics.json"
    metrics = read_json(metrics_path)
    metrics.update({
        "adjudication_status": audit["status"],
        "adjudication_model": judge_summary.get("model"),
        "adjudication_review_count": len(reviews),
        "adjudication_call_failure_count": judge_summary.get("call_failure_count"),
        "adjudication_effective_verdict_counts": dict(sorted(effective_verdict_counts.items())),
        "confirmed_regression_observations": len(confirmed_cases),
        "confirmed_regression_events": len(events),
        "confirmed_guided_generated_test_observations": len(guided_generated_confirmed),
        "confirmed_events_hit_by_guided_generated_tests": len(events_with_guided_generated_test),
        "confirmed_events_only_hit_by_guided_generated_tests_in_this_run": len(events_only_guided_generated),
        "unresolved_adjudication_cases": len(inconclusive_cases),
        "rq1_comparison": "not_calculable_from_guided_only_results",
        "adjudication_report": str(output_files["report"].relative_to(ROOT)),
    })
    write_json(metrics_path, metrics)

    metrics_md_path = evaluation_dir / "evaluation_metrics.md"
    previous_md = metrics_md_path.read_text(encoding="utf-8")
    marker = "## 统一 LLM 判定与去重结果"
    stale_marker = "## 目前不能计算的 RQ1 指标"
    if stale_marker in previous_md:
        previous_md = previous_md.split(stale_marker, 1)[0].rstrip() + "\n\n"
    if marker in previous_md:
        previous_md = previous_md.split(marker, 1)[0].rstrip() + "\n"
    addition = [
        marker,
        "",
        f"固定 judge `{judge_summary.get('model')}` 已完成 649/649 个盲化案例判定，调用失败 0。按统一门槛，结果为：**{len(confirmed_cases)} 条确认回归观察，{len(events)} 个去重事件 episode，{len(inconclusive_cases)} 个未决案例**。判为规格变化 {effective_verdict_counts.get('expected_spec_change', 0)}、环境失败 {effective_verdict_counts.get('environment_failure', 0)}、无效测试 {effective_verdict_counts.get('invalid_test', 0)}。",
        "",
        f"新增 Guided 测试涉及 {len(guided_generated_confirmed)} 条确认观察、{len(events_with_guided_generated_test)} 个去重事件 episode；其中 {len(events_only_guided_generated)} 个 episode 在此 MG 套件运行中没有 base-test 命中。该数字为单方法内部来源分析，不代表对 Original/Vanilla 的正式比较。",
        "",
        f"逐事件结果及未决案例见 [`guided_judge/mutation_guided_adjudication_report.md`](guided_judge/mutation_guided_adjudication_report.md)。仍不能计算三种方法间的 RQ1 比较、显著性或相对提升，因为本评估只运行 Mutation-Guided。",
        "",
    ]
    metrics_md_path.write_text(previous_md + "\n".join(addition), encoding="utf-8")

    status_path = evaluation_dir / "adjudication_execution_status.json"
    old_status = read_json(status_path) if status_path.is_file() else {}
    write_json(status_path, {
        "format": "rq1-adjudication-execution-status/v1",
        "status": "complete_llm_only_with_unresolved_cases",
        "judge_model": judge_summary.get("model"),
        "reasoning_effort": judge_summary.get("reasoning_effort"),
        "valid_model_responses": len(reviews),
        "case_count": len(mapping),
        "call_failure_count": judge_summary.get("call_failure_count"),
        "confirmed_regression_observations": len(confirmed_cases),
        "confirmed_regression_event_episodes": len(events),
        "unresolved_case_count": len(inconclusive_cases),
        "prior_local_cli_startup_failures": old_status.get("local_cli_startup_failures", 231),
        "prior_auto_review_block": old_status.get("reason") or old_status.get("prior_auto_review_block") or "The automatic approval review initially required explicit approval for the full 649-case payload; the user approved before the successful judge run.",
        "prior_local_cli_failures_are_not_reviews": True,
        "human_review_status": "not performed; current protocol is LLM-only, and flagged/low-confidence cases remain inconclusive",
        "rq1_comparison_status": "not_calculable_from_mutation_guided_only_evaluation",
        "report_path": str(output_files["report"].relative_to(ROOT)),
        "completed_at_utc": audit["generated_at_utc"],
    })

    print(json.dumps({
        "status": audit["status"],
        "reviews": len(reviews),
        "effective_verdict_counts": dict(sorted(effective_verdict_counts.items())),
        "confirmed_observations": len(confirmed_cases),
        "deduplicated_event_episodes": len(events),
        "guided_generated_confirmed_observations": len(guided_generated_confirmed),
        "events_hit_by_added_guided_tests": len(events_with_guided_generated_test),
        "events_only_hit_by_added_guided_tests_in_this_run": len(events_only_guided_generated),
        "unresolved_cases": len(inconclusive_cases),
        "outputs": {key: str(path) for key, path in output_files.items()},
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

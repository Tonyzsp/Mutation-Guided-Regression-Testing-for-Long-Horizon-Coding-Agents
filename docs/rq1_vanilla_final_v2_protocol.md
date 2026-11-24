# RQ1 Final V2 Protocol: Vanilla Evaluation

**Status:** Governing protocol for the final Vanilla comparator. Use one final Vanilla cohort in future analysis; do not report historical generation batches as separate Vanilla conditions. The frozen suite path below keeps its original storage suffix for provenance only.

## 1. Purpose and interpretation

Evaluate the existing frozen Vanilla C1 suite on the same later checkpoints and under the same execution and adjudication rules as the final Mutation-Guided (MG) composite suite.

This protocol makes **no additional Vanilla generation calls**. It reuses the audited frozen Vanilla v5 suite: 65 retained generated tests from 89 candidates across 26 tasks, with a frozen suite manifest for each of the 36 tasks. These are the physical manifests and cohort audit available in this repository; do not substitute an unmaterialized historical identifier or expand the suite during evaluation.

The comparison is generation-budget asymmetric: the final MG suite includes the accepted MG recovery tests, while Vanilla receives no new tests. Results may be described as a comparison of the available frozen suites under matched evaluation conditions. They must not be described as a controlled comparison under equal test-generation budgets or as a causal effect of mutation guidance.

## 2. Frozen suite inputs

- Source checkpoint: C1 for all 36 task trajectories.
- Vanilla suite for task `T`: `artifacts/results/rq1_full_36x1/T/C1/vanilla_formal_v5_20261116/`.
- Cohort audit: `artifacts/results/rq1_full_36x1/c1_vanilla_cohort_audit_formal_v1_20261117.json`.
- Shared C1 base-suite audit: `artifacts/results/rq1_full_36x1/c1_base_audit_current_sanitized_20261116/c1_base_suite_audit.json`.
- Final MG comparator: `artifacts/results/rq1_recovery_v2/T/C1/mutation_guided_composite_v2/`, containing the unchanged legal C1 base tests plus all accepted MG tests in the final composite.

The `vanilla_formal_v5_20261116` directory name is a frozen storage identifier. It does not define a separate current protocol cohort.

Before any later-checkpoint package is read, the runner must verify:

1. exactly 36 Vanilla suite manifests exist and identify the correct task and C1 source;
2. the audited retained-test total is 65 across 26 tasks, and each test file matches its frozen suite hash;
3. every suite uses the legal C1 `base_tests` hash in the complete baseline audit;
4. the baseline audit and package hashes match the sanitized C1 checkpoint packages; and
5. the final MG composite has 36 matching suite manifests and its previously completed evaluation covers all 160 applicable later-checkpoint pairs.

Any mismatch blocks the cohort. Do not edit, repair, or regenerate a frozen Vanilla test during this evaluation.

## 3. Later-checkpoint execution

Run the Vanilla suite unchanged on every applicable later checkpoint for all 36 task trajectories: 160 task/checkpoint pairs in total. Use the same runner, Docker image (`rq1-b-tools:python312` with the C1 audit's pinned image ID), timeout, infrastructure retry rule, and test environment used for the completed final MG evaluation.

- Suite timeout: 900 seconds.
- Run each frozen suite once at each target checkpoint.
- Retry a suite target only for a recorded infrastructure failure, at most once. Do not rerun a full suite because of an application assertion failure.
- A candidate regression observation requires the same test ID to be a stable pass in the C1 baseline and to fail in the complete target-suite run.
- Repeat each eligible target failure three times by exact test ID. Count it as a stable failure only when all three reproduce the same application assertion failure. Setup errors, collection errors, timeouts, test errors, and inconsistent outcomes remain infrastructure or inconclusive evidence.
- Do not modify test assertions or use target-checkpoint tests when executing the frozen C1 suite.

Example command from the repository root; choose a new output directory that does not already contain a run:

```powershell
python scripts/run_rq1_later_checkpoint_evaluation.py --method vanilla_llm --problems-root external/scb-problems --timeout-seconds 900 --output-dir artifacts/results/rq1_full_36x1/eval_vanilla_final_v2_20261119
```

Use the pinned `scb-problems` revision `9cd9ca3a51c3d3e2a99d2488a25baf73a2204451`. The runner must finish with 36 tasks, 160 completed pairs, and zero unresolved infrastructure failures before adjudication begins.

## 4. Blinded adjudication and event accounting

Use the same frozen judge model, reasoning setting, prompt, schema, confidence threshold, and attempt policy for final MG and Vanilla cases. The judge is GPT-6 Luna, medium reasoning, method-blinded. It receives the source and target specifications, frozen test, C1 pass evidence, relevant code diff, package/trajectory evidence, and repeated failure traces. It must not receive the method label, hidden/future tests, or mutant diffs.

A verdict is countable only with a valid structured response and confidence of at least 0.80. If the response is invalid or no structured verdict is returned, allow at most one protocol-authorized technical retry. No human review is budgeted; low-confidence or flagged cases remain inconclusive.

Before combining verdicts, verify that the MG and Vanilla cases use the same judge prompt/schema hashes and rubric. Reuse existing MG verdicts only if they match. If they do not, prepare one new method-blinded adjudication cohort using the common prompt/schema for the MG and Vanilla cases used in the comparison; retain earlier verdicts as provenance, not as the final paired result.

Merge cases across methods, tests, and later checkpoints by underlying task behavior and introducing code change. Count one event once, anchored at its earliest failing checkpoint. Preserve observation-level records and report which methods detected each shared or unique event.

## 5. Required report and claim boundary

Report for both final suites:

- frozen suite hash and generated-test count;
- completed suite runs, stable failure observations, errors, infrastructure retries, and reproduced failures;
- LLM verdict totals, inconclusive cases, and deduplicated confirmed event counts;
- shared and method-unique events, with per-task counts; and
- actual evaluation and adjudication calls, token usage when available, and execution cost.

The primary interpretation is: **which available frozen suite detects more LLM-confirmed regression events under the same later-checkpoint evaluation conditions?** Since the Vanilla suite receives no incremental generation budget while the final MG suite contains recovered MG tests, this is not the original equal-generation-budget RQ1 contrast. State that limitation anywhere the comparison is reported. Do not claim absolute regression recall because there is no exhaustive ground-truth event oracle.

## 6. Frozen provenance

Keep the original Vanilla generation, review, validation, and suite manifests unchanged. Store this evaluation in a new output directory and record the source suite hashes, C1 baseline hash, checkpoint hashes, runner configuration, judge prompt/schema hashes, and final event-ledger hash. Future reports use the single final Vanilla cohort defined here; historical run identifiers remain provenance only.

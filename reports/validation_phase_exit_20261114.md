# RQ1 Validation Phase Exit Review

**Review date:** 2026-11-14 (America/New_York; model-call artifacts are timestamped 2026-11-15 UTC)

**Decision:** **GO** — close method validation and enter full development.

**RQ1 evaluation:** **NO-GO for now** — the full C1 test/mutant package is not complete, and no later-checkpoint regression comparison is reported here.

## What this decision means

The small-scale experiments establish that the core approach can run end to end: sanitized C1 packages can be audited, coverage and mutation analysis can be linked to the official base suite, Guided candidates can be generated and checked against a mutant, and an LLM reviewer can distinguish a clearly supported assertion from a clearly contradictory one.

Validation is therefore complete for **method feasibility and workflow design**. Full development now means producing the final, traceable artifacts across all 36 tasks. It does not mean RQ1 results are ready to calculate.

## Evidence reviewed

| Area | Verified evidence | Status and limit |
| --- | --- | --- |
| Benchmark packages and access boundary | Release snapshot has 196 packages across 36 task trajectories. Each C1 generator package contains its specification, workspace, and legal base tests; isolated hidden/future/excluded paths remain separate. | Suitable as the fixed input cohort. No later-checkpoint files were used in generation. |
| C1 base suite | Local evidence: `artifacts/results/rq1_full_36x1/c1_base_audit_isolation_revalidated_20261019/c1_base_suite_audit.json`. It covers 36/36 tasks, repeats each suite three times, records 26 `stable_with_failures`, 10 `stable_pass`, and zero unusable tasks. Final audit SHA-256: `dffad23696d1f9c7c7732edbc54b1b6e87de42cea889356398cce195ebc375eb`. | Base behavior is stable enough to use as the common comparison suite. Stable baseline failures must remain recorded; they are not newly generated regressions. |
| Coverage | Local evidence: `artifacts/results/rq1_full_36x1/c1_coverage_integrity_audit_20261019.json`. It reports 36/36 tasks, `complete`, and no errors, linked to the final baseline audit. | Cohort coverage integrity is complete. |
| Vanilla | Local evidence: `artifacts/results/rq1_full_36x1/c1_vanilla_cohort_audit_20261019.json`. It reports 36/36 tasks, 76 candidates, 48 retained, 28 rejected, and 7 empty generations. Latest final-audit-hash-linked suite manifests exist for all 36 tasks; the 48 retained test files appear in 25 suites. | Generation and validation artifacts are substantially complete. Full-cohort development still needs a final integrity pass against the single canonical paired budget and reviewer record format. |
| Mutmut | Mutation summaries matching both the final base-audit hash and current task base-test hashes exist for 8/36 tasks. The revised direct pipeline completed pilots on `cfgpipe` (233 mutants: 172 killed, 61 survived) and `eve_route_planner` (605 mutants: 375 killed, 230 survived). | Instrumentation and direct mutant execution are feasible. These results do not establish a 36-task mutant cohort. Older retries and the separate v3 pilots must not be counted twice. |
| Guided generation and target execution | Final-audit-hash-linked Guided manifests exist for 4/36 tasks; two manifests contain generated tests, three files total. A route-planner candidate was collected, passed the C1 suite, and killed its selected target mutant in three repeated target runs. | The target-kill path works. The route-planner six-hour boundary assertion remains semantically `uncertain` under the public specification, so it is a technical pilot and must not be counted as an accepted correct test. The Guided cohort is not complete. |
| Independent LLM candidate review | Local evidence under `artifacts/results/rq1_full_36x1/candidate_review_calibration_eve_route_bidirectional_v2_20261115/`: the calibrated reviewer labeled a test of the explicit bidirectional-jump rule `correct` and a candidate that contradicts the required `START` line `incorrect`. Prompts, model settings, and token counts are archived there. | The standalone reviewer passes a basic positive/negative calibration. The cohort generation wrappers still need to call it and archive its output for every candidate. |

## Validation exit criteria

| Criterion | Result |
| --- | --- |
| Fixed cohort and sanitized, checkpoint-local inputs | Met: 36 tasks; C1 source/spec/base-test boundary established. |
| Repeatable shared C1 baseline and environment records | Met: 36/36 audited in three repetitions; zero unusable tasks. |
| Coverage integrity across the cohort | Met: 36/36 complete and hash-linked. |
| Mutmut execution and subprocess mutation association | Met at representative scale; direct runs and target application were exercised. Full 36-task output remains development work. |
| Guided generation, validator, freeze record, and target-mutant check | Met as a pilot; cohort-scale generation and semantic acceptance remain development work. |
| LLM semantic reviewer is usable for no-human workflow | Met for basic positive/negative calibration; integration and full candidate review remain development work. |
| Formal three-method RQ1 comparison | Not met by design; belongs after full-development exit. |

Because the remaining gaps concern **cohort-scale production and integration**, not whether the method can work, the project may close validation and move into development. Keep the evaluation gate closed until all development exit criteria below pass.

## Full-development plan

### D1. Freeze the formal configuration

- Choose one canonical paired Vanilla/Guided budget and record its content hash in both method runs. Exploratory pilot budgets remain separately labeled.
- Freeze the final direct-mutmut configuration, timeout/retry behavior, Python/runtime versions, mutation scope, and status vocabulary.
- Pin reviewer model/version, reasoning setting, prompt hash, output schema, and confidence policy.

**Exit record:** approved protocol/config hashes referenced by every later formal cohort run.

### D2. Complete the 36-task mutant cohort

- Re-run the finalized direct-mutmut workflow on each task against the exact C1 source and complete official base suite.
- For every task, emit normalized records and hashes for the source, base tests, baseline audit, mutmut configuration, and run output.
- Preserve `killed`, `survived`, `invalid`, `unreachable`, `suspected_equivalent`, `out_of_scope`, and `deferred` outcomes with reasons. Do not silently discard slow or ambiguous mutants.
- Triage survivors before any diff is sent to Guided generation.

**Exit record:** one auditable mutant status package or explicit non-runnable explanation for each of 36 tasks.

### D3. Finish and audit Vanilla suites

- Keep all 36 hash-linked suites and the current 48 retained candidates as inputs to the formal audit; do not regenerate a task merely because it has zero candidates.
- Integrate the independent reviewer into the cohort runner. Review every candidate using only the corresponding C1 spec and complete legal C1 base tests.
- Retain only reviewer-accepted candidates that pass collection, the trusted C1 correctness check, and the required stability repetitions. Record rejected/uncertain candidates and why.

**Exit record:** per-task frozen suite or explicit zero-retained record, with budget, reviewer, validation, and file hashes.

### D4. Complete Guided generation under the paired budget

- Generate tests from only C1 specification, source, legal base tests, and eligible surviving-mutant diffs.
- Use the same per-method call/token/candidate limits and C1 validation repetitions as Vanilla. Record an empty response as an outcome, not as a missing run.
- Run the same independent semantic review as Vanilla. In addition, verify each retained Guided test kills its declared target mutant and passes the unmodified C1 implementation.
- Do not treat an ambiguous mutant diff or an unsupported boundary oracle as an accepted test.

**Exit record:** one auditable Guided suite or explicit zero-retained record per task, with target-mutant execution records where applicable.

### D5. Pre-evaluation integrity gate

Build one machine-checkable 36-task manifest that verifies for each unit:

1. matched C1 package, source, complete base-suite, and final baseline hashes;
2. one canonical budget and pinned model/configuration for Vanilla and Guided;
3. a mutant package or documented non-runnable status;
4. candidate review and validation outcomes with no unclassified failures;
5. a frozen Original/Vanilla/Guided suite or an explicit, justified zero-generated-test result; and
6. no generator input path containing hidden, future, excluded, reference, or later-checkpoint data.

**Only after this gate passes** should the team begin later-checkpoint execution, blinded LLM regression adjudication, and RQ1 counting. The regression evaluator must remain blinded to method and use one frozen judge protocol for all methods.

## Current exclusions and cautions

- No C2+ regression-evaluation run or RQ1 detection count was started as part of this phase closeout.
- The Guided count above is an artifact inventory, not a claim that three semantically accepted tests exist.
- The initial Zarzakh boundary candidate was classified `uncertain` because the public spec does not fully pin the exact boundary-time oracle. It remains useful evidence that C1 execution and target-mutant killing work, but it should not be promoted as a correct formal test without stronger specification evidence.
- Generated run outputs under `artifacts/results/` are intentionally ignored by Git. This report preserves the summarized evidence and exact local artifact paths.

# RQ1 Pre-evaluation Preparation

**Status date:** 2026-11-14 (America/New_York)

**Phase decision:** Validation phase closed; proceed to full development.

**Evaluation decision:** Not ready to run the RQ1 later-checkpoint comparison.

## Current state

The Release supplies **196 checkpoint packages across 36 tasks**. Generator inputs are the sanitized C1 specification, source, and legal `base_tests`; hidden, future, excluded, and reference data remain outside that input path.

- The C1 base-suite audit covers **36/36 tasks**, with three repetitions and no unusable task. It records 26 tasks with stable failures and 10 with stable passes; those failures are baseline behavior to preserve and classify, not reasons to remove official tests.
- Coverage integrity is complete for **36/36 C1 packages**, linked to the final base-audit hash, with no audit errors.
- Vanilla generation and audit cover **36/36 tasks**: 76 candidates, 48 retained, 28 rejected, and 7 tasks with no candidate output. Hash-linked Vanilla manifests exist for all 36 tasks; the retained candidates appear in 25 suites.
- Mutant summaries linked to the final base-audit hash and current base-test hashes exist for **8/36 tasks**. The revised direct mutmut pipeline has completed representative runs on `cfgpipe` and `eve_route_planner`; the latter produced 605 mutants, with 375 killed and 230 surviving. This is a pilot, not full-cohort mutant coverage.
- Guided manifests linked to the final base-audit hash exist for **4/36 tasks**. Two contain generated tests, with three tests total. These are pilot/cohort artifacts; they do not constitute a complete, budget-matched 36-task Guided cohort.
- The independent candidate reviewer passed a small blind calibration on one specification-supported candidate (`correct`) and one contradictory candidate (`incorrect`). Its positive used the explicit bidirectional-jump rule and made no uncertain travel-time claim.

The detailed evidence, paths, and limitations are in [the validation phase exit report](../reports/validation_phase_exit_20261114.md).

## Phase decision

The method-validation exit criteria are met: the data boundary and shared C1 suite are established, coverage runs are linked across the cohort, mutation execution and mutant application have been exercised on representative tasks, a Guided test has been collected/frozen and shown to kill a target mutant, and the LLM correctness-review path has passed positive/negative calibration.

This permits the project to move from **method validation** into **full development**. It does not mean the RQ1 experiment is complete. The 36-task mutation and Guided outputs are unfinished, and the later-checkpoint regression evaluation has not started.

## Full-development work, in order

1. Lock one canonical, paired generation budget and one final direct-mutmut configuration. Keep exploratory pilots labeled separately from formal cohort records.
2. Run and audit the finalized direct-mutmut pipeline for all 36 C1 tasks. Preserve killed, surviving, invalid, unreachable, suspected-equivalent, out-of-scope, and deferred statuses with hashes and reasons.
3. Integrate the calibrated independent LLM review into both Vanilla and Guided cohort workflows. Store prompts, model/version, verdicts, rationale, and token usage. Route `uncertain` or conflicting reviews to a documented resolution path.
4. Produce and validate/freeze the full Vanilla and Mutation-Guided C1 suites under the same per-method generation budget, input visibility, candidate cap, and repetition rules. Keep empty-generation and zero-retained outcomes as explicit records.
5. Run a 36-task integrity audit: every unit must have matching C1 package/base hashes, budget and model records, mutant status or documented non-runnable status, candidate review and validation records, and a frozen suite or an explicit reason it has none.
6. Only after that audit passes, begin later-checkpoint regression collection and RQ1 comparison. Keep regression adjudication and detection counts out of the current development run until the pre-evaluation package is complete.

## Shared budget and implementation references

The existing cohort budgets are under [`config/`](../config/); before full-cohort continuation, declare a single canonical paired budget and reference its hash in both method records. Core implementation and interface notes:

- [Experiment protocol](experiment_protocol.md)
- [B generation protocol](b_generation_protocol.md)
- [Checkpoint runtime adapter](checkpoint_runtime_adapter.md)
- [Gate 2 checkpoint integration](gate2_checkpoint_integration.md)

Generated run outputs under `artifacts/results/` are intentionally ignored by Git; the current
status and key counts are summarized in the committed phase-exit report.

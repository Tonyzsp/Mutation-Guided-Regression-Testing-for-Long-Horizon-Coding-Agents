# Unified Mutation-Guided Protocol for RQ1 Preparation and Evaluation

**Status:** Governing protocol for the combined Mutation-Guided (MG) C1 test cohort and its later-checkpoint evaluation. The prior run IDs, budget locks, and raw artifacts remain immutable provenance; future descriptions and combined analyses use this single protocol. This is an MG-only protocol and does not make an Original-versus-MG or Vanilla-versus-MG comparison.

## 1. Scope and information boundary

The unit is one SlopCodeBench task trajectory. Use C1 as the source checkpoint for all 36 tasks and evaluate the frozen C1 suites on all 160 applicable later-checkpoint pairs. A task remains in the cohort if it has no survivor, no eligible hint, no generated candidate, or no retained test; represent these cases with an explicit empty MG suite.

The generator, survivor triage, and correctness reviewer may use only sanitized C1 specification, C1 source, and the complete legal C1 `base_tests`. Exclude hidden tests, future checkpoint files or specifications, later failures, reference solutions, and `file_backup/errors`. Preserve the official C1 base suite unchanged. Hash every package, input view, prompt, candidate, and frozen suite.

## 2. Survivor inventory and hint selection

Run the pinned mutation pipeline against C1 and retain the raw result for every mutant. Normalize outcomes as killed, survived, or inconclusive; errors, timeouts, invalid mutations, and not-tested mutants are not survivors. Group survivors into deterministic mechanical families using task, source file, enclosing function, raw mutmut operator when available (otherwise a clearly marked inferred AST edit kind), and normalized edit. Select the lexicographically smallest mutant ID per family, stratify by file and edit kind, and round-robin up to 16 representatives per task.

A fixed GPT-6 Luna medium triage reviews only selected representatives against the C1 specification and public behavior. A hint is eligible only when the response identifies an explicit C1 requirement, a deterministic public input, and an observable expected result. `suspected_equivalent`, `out_of_scope`, deferred, invalid, or missing decisions are not shown to the generator. Never infer exact-line reachability or semantic non-equivalence from a function-level hit alone.

## 3. One staged generation budget

Use GPT-6 Luna medium with the Codex service's default decoding. Each candidate file contains exactly one top-level pytest test. The budget is cumulative per task:

1. **Initial opportunity:** up to 3 calls and 8 candidate files, requesting 4,096 output tokens per call (12,288 requested tokens maximum).
2. **Conditional supplement:** only if the initial opportunity retained zero MG tests, allow up to 2 independent calls, at most 4 candidate files per call (8 additional), and 4,096 requested output tokens per call.

Thus the absolute per-task ceiling is 5 calls, 16 candidate files, and 20,480 requested output tokens. Calls in the supplement receive the same permitted C1 context and cannot see previous candidate outputs, review decisions, validation results, or later-checkpoint outcomes. Launch no MG call when there is no eligible hint; report the unused opportunity and do not transfer it. No repair calls or retries are allowed; any failed launch consumes its slot, and unused opportunities are not transferred. Report actual service usage when available; requested output tokens are not guaranteed service-side limits.

This adaptive supplement is a recovery rule, not an equal-budget cross-method comparison. Its cohort membership is determined by the initial retained-test outcome. Report the full 36-task MG cohort and the number of calls/candidates at each stage; do not treat the supplemental subset as an independently sampled confirmatory cohort.

**Budget and evaluation are separate dimensions.** The call/candidate ceilings are additive only for tasks that had zero retained MG tests after the initial opportunity; tasks with at least one retained initial test receive no supplemental generation calls. The later-checkpoint run is a separate evaluation execution, not a subset evaluation: it covers all 36 tasks and all 160 target pairs. Each task's evaluation suite is the composite of its legal C1 base tests, its already retained MG tests, and any newly retained supplemental MG tests. Therefore, successful initial tasks were evaluated again using their initial MG tests in the full composite run, but they received no supplemental generation budget. The two evaluation runs share many suite/test/target observations; retain both run records, but merge and deduplicate their event evidence rather than treating them as independent task cohorts or adding raw counts.

## 4. Candidate review, validation, and freezing

Review each non-empty candidate batch with a fixed GPT-6 Luna medium reviewer using method-blinded prompts. The reviewer sees the candidate, C1 specification, and legal C1 test/fixture inventory, but not the mutation hint, mutant ID, or method label. Retain only candidates judged correct: every assertion must be directly supported by the C1 specification and have a deterministic public oracle. This is automated review evidence, not an independent ground-truth oracle.

Apply deterministic safety checks, require pytest collection, then run the hash-matched C1 base suite plus each candidate three times. A candidate must pass in all three runs and introduce no new stable failure beyond the accepted C1 baseline. Run the complete frozen composite suite (base plus every retained MG test) three times on C1 before later-checkpoint evaluation.

For a Guided candidate, verify that at least one named, eligible C1 survivor changes the source and causes the candidate's assertion failure in all three fresh target-mutant checks. Mutmut runner error or timeout may use a narrowly scoped fallback: apply the exact cached survivor diff to a fresh C1 copy, verify the source changed, and require the same candidate failure in all three runs. Record this fallback separately from mutmut-based checks. Errors, timeouts, collection failures, or setup failures never count as mutant kills. Freeze accepted files and validation evidence under content hashes.

## 5. Later-checkpoint execution and adjudication

For each task, overlay the frozen C1 test suite read-only onto each applicable later checkpoint; never alter assertions based on future code. Run the suite once, then repeat each eligible application-test failure or target-code syntax error by exact test ID three times. Reproducible assertion failures and target-code syntax failures may proceed to adjudication. Classify test/setup/environment errors, timeouts, flaky outcomes, and infrastructure failures separately; they do not automatically count as regressions.

Use one fixed, method-blinded GPT-6 Luna medium judge for all cases. Provide the C1 and target specifications, frozen test, C1 pass evidence, relevant code diff, package/trajectory evidence, and repeated failure traces. Do not provide hidden/future tests, mutant diffs, or the method label. A verdict is countable only when the structured response is valid, confidence is at least 0.80, and it does not request human review; otherwise mark it inconclusive. Stable test/setup/environment errors remain diagnostic unless the evidence establishes a source-code regression. Deduplicate multiple tests and persistent failures of one task-level behavior into one event, anchored at the earliest failing checkpoint. Preserve raw observations and the mapping from test to event.

## 6. Reporting and current conformance note

Report the combined MG cohort as one result: tasks and empty suites, mutants and triage, generation calls/candidates/retained tests, C1 validation, later-checkpoint observations, LLM verdicts, deduplicated events, coverage, and RQ3 lineage. Report MG-only findings as MG-only. The current combined materials record 25 retained MG tests across 16 tasks and 372 unique confirmed event episodes, including 6 events attributed only to MG-generated tests; these do not establish improvement over Original or Vanilla.

The latest validation rule requires three target-mutant repetitions. The older frozen records for 13 retained tests document only one target-mutant repetition; this protocol does not retroactively upgrade those records. Keep their actual evidence visible in the combined report, and re-run those checks if a claim requires every retained test to satisfy the uniform three-run target-kill gate. Existing run IDs and locked configurations are not edited when this consolidated policy is adopted.

## 7. Primary implementation and evidence sources

- `config/rq1_full_study_budget_v1.json` and its lock: initial-cohort limits and analysis design.
- `config/rq1_recovery_v2_mg_only_amendment.json` and its lock: MG-only supplemental scope and latest three-run validation rule.
- `config/generation_budget_rq1_recovery_v2_call.json`: per-call model, candidate, and repeat limits.
- `scripts/run_guided_c1_cohort.py`, `scripts/run_rq1_recovery_v2.py`, and `scripts/run_rq1_recovery_v2_mg_validation.py`: generation and validation orchestration.
- `scripts/build_rq1_recovery_v2_mg_composite_suites.py` and `scripts/validate_rq1_recovery_v2_mg_composite_c1.py`: immutable suite composition and composite C1 gate.
- `scripts/run_rq1_later_checkpoint_evaluation.py` and MG adjudication/report scripts: later-checkpoint execution and event processing.

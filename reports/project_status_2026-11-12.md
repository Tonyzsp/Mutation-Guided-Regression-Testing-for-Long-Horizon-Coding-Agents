# Project Status — 2026-11-12

## Remote synchronization

- Fetched `origin` successfully.
- Local `main` and `origin/main` both point to `c11035a` (`Add reusable checkpoint mutation runtime`); there are no incoming or outgoing commits.
- No commit or push was made.
- The worktree contains 76 modified/untracked entries. They include implementation, schemas, experiment configuration, prompts, reports, and local artifacts. They have not been reviewed as a single publishable change set.

## Completed and evidenced work

### Benchmark inputs and baseline

- The RQ1 release data contains 36 C1 checkpoint packages with source, specification, and legal base tests.
- The isolation-revalidated C1 base-suite audit contains 36 records: 10 `stable_pass` and 26 `stable_with_failures`. The latter are stable known baseline failures, not unstable runs.
- Audit record: `artifacts/results/rq1_full_36x1/c1_base_audit_isolation_revalidated_20261019/c1_base_suite_audit.json`.

### Vanilla generation

- 35 task-level Vanilla C1 suites were generated and frozen under `vanilla_formal_v4_20261019`.
- The suites retain the official base tests and record their generation and validation metadata.
- The remaining one task is not part of the completed frozen cohort recorded in the 2026-10-19 progress report.

### Direct mutation classification

- Four tasks have direct mutant classification records: `cfgpipe`, `code_search`, `eve_industry`, and `xjq`.
- The 2026-10-19 audit counted 1,110 mutants total: 852 killed and 258 survived (76.8% aggregate mutation score across those four tasks only).
- This score is not a 36-task cohort score and does not answer RQ1.

### Initial task-level Mutation-Guided generation

- Three task-level Guided runs were recorded for `cfgpipe`, `code_search`, and `eve_industry`.
- The runs returned two candidates total. Both were stopped at a local specification review marked `uncertain`; neither entered pytest collection or target-mutant validation. The other two task responses contained no candidate.
- These are generation diagnostics, not evidence that candidates are unexecutable or cannot kill mutants.

### Controlled paired generation diagnostic

- A 16-target pool was frozen before generation: 1 `cfgpipe`, 9 `code_search`, and 6 `eve_industry` behavior units.
- All 16 targets received one Vanilla and one Guided generation call (32 calls). Vanilla returned 12 candidates and Guided returned 13; seven calls returned no candidate.
- A local method-blind specification review covered 25 candidates: 20 `correct`, 4 `incorrect`, and 1 `uncertain`. This was a local Codex review, not an independent human review. The external review call was rejected by automatic approval review.
- For the first 10 targets (`cfgpipe` plus all 9 `code_search` targets), both methods completed the original validator: 20/20 method conditions. Stable valid targeted tests were 1/10 Vanilla and 5/10 Guided.
- This subset is a small, preliminary diagnostic of whether mutant guidance helps generate a valid same-checkpoint killing test. It is not the later-checkpoint regression comparison in RQ1.
- Evidence: `artifacts/results/cpw2/validation_dispatch.jsonl` and `reports/controlled_pairwise_failure_analysis_partial_20261019.md`.

## Incomplete work and blockers

- The first paired EVE validation attempt used `C1-runtime-adapter-probe`, whose subprocess entrypoint was `python industry.py`; that interpreter lacked PyYAML. All 12 EVE method conditions in that attempt failed before candidate adjudication.
- The audited runtime `C1-20261019T084049Z` has matching source/base-test hashes and passed the repeated C1 audit. With that runtime, two EVE Vanilla conditions completed, but one EVE Guided condition timed out in the mutmut runner. A subsequent Guided attempt timed out while checking the source baseline. The remaining EVE pairs are incomplete.
- The proposed fast path that applies the archived mutant diff and executes only the generated candidate node is experimental, disabled by default, and unverified. It must not be used for reported results until syntax and end-to-end checks pass.
- No complete paired-experiment funnel report exists yet.
- No complete later-checkpoint regression evaluation or confirmed regression-event count is evidenced in the current reports. Therefore the current evidence does not answer RQ1.

## Work remaining, in order

1. **Recover the EVE validation environment.** Re-run a single EVE pair with the audited runtime and the existing validator; record timings and confirm the source baseline before processing the other five targets.
2. **Complete the paired diagnostic.** Finish both methods for all six EVE targets. If the existing mutmut path remains too slow, verify the optional diff-based path on one known `code_search` case and one EVE case first, then rerun every paired condition under the same validated procedure.
3. **Resolve independent review.** Have a human reviewer or an approved fixed judge review the method-blind specification verdicts, especially the 4 incorrect and 1 uncertain decisions. Preserve the current local review as provenance; do not describe it as an independent review.
4. **Produce the complete diagnostic funnel.** Summarize calls, candidate emission, review, collection, C1 pass, target kill, and three-run stability for all 16 paired targets. Keep this result labeled as a same-checkpoint generation diagnostic.
5. **Run the actual RQ1 evaluation.** Execute the frozen Original, Vanilla, and Guided suites against later applicable checkpoints; extract new failures, screen environment/test issues, adjudicate candidate regressions, deduplicate events, and calculate the preregistered detection metrics under equal generation budgets.
6. **Finalize reproducibility and release changes.** Review the 76 local worktree entries, separate core reusable code from experiment outputs, run the required validation, and prepare a reviewed core-only change set. The current branch has not been committed or pushed.

## Current claim boundary

The project has substantial input, baseline, Vanilla, and mutation infrastructure in place. The 10-target paired subset favors Guided generation on its diagnostic metric (5/10 vs 1/10), but the paired experiment is incomplete, its EVE conditions remain unresolved, and its review process is not yet independent. No RQ1 conclusion should be stated until the later-checkpoint regression evaluation and adjudication are complete.

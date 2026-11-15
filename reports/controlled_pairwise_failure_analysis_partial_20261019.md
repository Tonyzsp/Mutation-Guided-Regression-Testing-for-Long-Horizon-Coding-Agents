# Controlled Pairwise Failure Analysis — Partial Results

**Experiment:** `controlled_pairwise_v1_20261019`

**Status:** incomplete; do not present as the final paired experiment or as RQ1 evidence.

## Frozen design and generation

- The pre-generation pool contains 16 eligible mutant behavior units: 1 `cfgpipe`, 9 `code_search`, and 6 `eve_industry`.
- All 16 targets received one Vanilla and one Mutation-Guided generation call (32 calls total).
- Vanilla returned 12 candidates; Mutation-Guided returned 13. Seven calls returned no candidate.
- An anonymized local specification review recorded 20 `correct`, 4 `incorrect`, and 1 `uncertain` verdict among the 25 candidates. The four incorrect cases overconstrained unspecified diagnostics or JSON formatting; the uncertain case required unspecified overlapping-match behavior.
- The reviewer was a local Codex specification review. It was method-blind, but it was not an independent human reviewer. The intended external review call was rejected by automatic approval review.

## Completed target validation subset

The original paired validator completed both conditions for the first 10 targets (`cfgpipe` plus all 9 `code_search` targets):

| Method | Stable valid targeted tests | Rate |
| --- | ---: | ---: |
| Vanilla | 1 / 10 | 10% |
| Mutation-Guided | 5 / 10 | 50% |

For this subset, a retained test passed the source C1 candidate check and killed its paired target mutant in all three fresh mutant runs. These are diagnostic results for converting known C1 test gaps into tests; they do not measure later-checkpoint regression detection.

## Why the run stopped

The first 12 EVE method runs used `C1-runtime-adapter-probe`, whose `--entrypoint` was `python industry.py`. That interpreter did not have PyYAML, so all 12 failed during the source-baseline check before candidate adjudication. They are environment failures and are not candidate failures.

The runtime used for the repeated stable C1 audit, `C1-20261019T084049Z`, has matching source and base-test hashes and passed its 12-test base suite in the repeated audit. On that runtime, two EVE Vanilla conditions completed, but the first EVE Guided condition timed out in the repeated targeted-mutmut runner. A further EVE Guided attempt timed out while checking the base suite. The remaining EVE pairs were not completed.

The attempted fast path that applies the archived mutmut diff and runs only the candidate node was left **disabled by default and unverified**. No results from that path are included above. The local execution host subsequently stopped returning even trivial shell commands, so the fast path could not be syntax-checked or run end to end.

## Interpretation and limits

The completed 10-target subset favors Mutation-Guided generation on the primary diagnostic measure, but the estimate is incomplete and small. The six EVE targets still need both conditions validated under the audited runtime. The method-blind correctness verdicts also need independent human review before use as formal study evidence. This experiment does not answer RQ1 because it does not measure later-checkpoint regressions.

## Evidence

- Frozen pool and pairs: `artifacts/results/controlled_pairwise_v1_20261019/sample_manifest.json`
- Blind review mapping and verdicts: `artifacts/results/controlled_pairwise_v1_20261019/blind_reviews/local_blind_review/`
- First validation attempt and EVE runtime errors: `artifacts/results/cpw2/validation_dispatch.jsonl` and per-run `pairwise_validation.stderr.log`
- Corrected-runtime EVE attempts: `artifacts/results/cpw3/validation_dispatch.jsonl` and per-run logs
- Repeated C1 baseline audit: `artifacts/results/rq1_full_36x1/c1_base_audit_isolation_revalidated_20261019/c1_base_suite_audit.json`

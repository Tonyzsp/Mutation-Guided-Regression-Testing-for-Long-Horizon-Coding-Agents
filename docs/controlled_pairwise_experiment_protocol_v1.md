# Controlled Paired Mutation-Guided Test Generation — Protocol v1

**Experiment ID:** `controlled_pairwise_v1_20261019`

**Purpose:** Measure whether showing an LLM one already-known, specification-grounded surviving-mutant diff helps it produce a valid test that kills that mutant. This is a diagnostic experiment; it does not measure later-checkpoint regression detection and does not answer RQ1 directly.

## Design

The experiment uses the C1 checkpoints for `cfgpipe`, `code_search`, and `eve_industry`. It compares two calls for each target mutant:

- **Vanilla:** current C1 specification, complete workspace source, and complete legal C1 base tests.
- **Mutation-Guided:** the exact same context plus one target mutant diff.

Both conditions use GPT-6 Luna, medium reasoning effort, the same Codex service decoding defaults, a requested 4,096 output-token budget, at most one call, and at most one candidate file containing one test function. Each output is judged and executed using the same method-blind specification rubric and the same C1 runtime.

The generator never receives hidden tests, later checkpoints, future failures, regression labels, or adjudication results. Vanilla receives no mutant ID, location, or diff. The Guided condition receives only the selected mutant ID and diff as its additional prompt section. The eligibility rationale and sample-selection file are not included in either generation prompt.

## Eligibility and sample

Eligibility was fixed before generation using six requirements: the target survived the complete C1 base suite and direct revalidation; its difference conflicts with an explicit public specification requirement; it is observable through the documented CLI; a deterministic distinguishing input exists; required data are present in the pinned task fixture or can be constructed from it; and the target is not equivalent or unreachable for that input.

To avoid treating near-identical mutmut edits as independent behavior families, the fixed pool uses one representative per distinct specification-level behavior family. All 16 eligible representatives are included; no post-generation sampling or replacement is allowed. Six additional survivors are recorded as redundant behavior variants, while other survivors remain explicitly deferred rather than being asserted equivalent. The preparation script verifies every selected ID against the complete direct-revalidated survivor records and freezes hashes for the package, mutation records, triage, and prompt inputs.

## Validation stages

For each generated candidate, in order:

1. Validate response shape, Python syntax, and the one-test-file limit.
2. Obtain a blind specification review. The reviewer sees only anonymized candidate code, the current C1 specification, and complete legal C1 base tests. It does not see method labels, target IDs, mutant diffs, or generation metadata. `uncertain` cases are not counted as valid unless a later human review resolves them.
3. Collect the candidate with pytest.
4. Run the candidate with the full C1 base suite three times against unmodified C1, respecting stable pre-existing baseline failures.
5. Apply the same target mutant to a clean runtime copy and run the candidate three times. It must fail specifically as a candidate assertion in all three runs; setup errors, timeouts, and unrelated failures do not count as kills.

A **valid targeted test** is a candidate that passes all five stages. The primary measure is the number and proportion of eligible mutants for which a method produces a valid targeted test. The report also gives a paired stage-by-stage funnel and per-mutant outcomes.

## Review execution note

The candidate review bundles were anonymized by task and candidate alias and contained candidate code, the current specification, and the complete legal C1 base tests. Local specification verdicts were recorded before opening the separate alias-to-method mapping. This produced 20 `correct`, 4 `incorrect`, and 1 `uncertain` verdict across 25 candidates; empty-generation responses received an empty review array. The intended external blind-review call was rejected by the app's automatic approval review, so the applied reviewer is recorded as a local Codex specification review, not as an independent human reviewer or a separately dispatched judge. The review evidence and rationales are retained under `artifacts/results/controlled_pairwise_v1_20261019/blind_reviews/local_blind_review/`.

## Reporting limits

This experiment tests the model’s ability to translate a known testing blind spot into a valid same-checkpoint test. It does not establish that the test detects a future regression. Results must not be reported as an RQ1 answer. Generator token usage is reported only when the Codex service supplies it; the requested budget is still identical across conditions.

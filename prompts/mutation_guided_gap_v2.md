# Mutation-Guided LLM Test Generation Prompt — coverage-gap pilot v2

You are generating additional pytest tests for one frozen Python checkpoint.

Use only the current checkpoint specification, source snapshot, complete legal base test
suite, shared specification-gap brief, and surviving-mutant records below. They all refer
to the same current checkpoint. Do not infer future changes, hidden tests, regression
labels, or expected failures from later checkpoints. Treat mutant diffs as test-generation
hints, never as requirements.

First compare the brief's gap IDs with the supplied base tests. Generate one candidate for
each brief item marked required. In particular, C1-GAP-01 is a directly specified example
that is absent from the base suite; the response must contain a test for it. Then use
surviving mutants only to prioritize tests for those public behaviors. Do not return an
empty test array merely because some mutants may be equivalent or the task is complex.
Never make up behavior or force a test for an out-of-contract mutant.

Write deterministic tests for externally observable behavior. Every candidate must pass on
the unmodified checkpoint and have a clear specification basis. Every candidate must name
one or more eligible mutant IDs that it is intended to kill; name only IDs the test is
expected to kill. A candidate must not rely on network access, wall-clock timing,
randomness, machine-specific paths, or private state. Use the provided pytest fixtures and
command style when appropriate. If a brief item is not tested, name its ID and give a short
reason in the required `assessment` field; do not include private chain-of-thought.

Return at most `{{MAX_CANDIDATE_FILES}}` candidate files and at most
`{{MAX_CANDIDATE_TEST_FUNCTIONS}}` top-level pytest test functions total. Put exactly one
top-level `test_...` function in each candidate file. Do not use parameterization to exceed
the candidate limit. Keep the response under `{{MAX_OUTPUT_TOKENS}}` output tokens. Return
exactly one JSON object matching the supplied candidate-output schema. Do not include
Markdown fences or prose outside the JSON. The object must include both `tests` and a short
string field `assessment`.

Do not use tools, inspect external files, or infer behavior from data not included below.

## Shared specification-gap brief

{{TEST_OPPORTUNITIES}}

## Current checkpoint specification

{{SPECIFICATION}}

## Current checkpoint source snapshot

{{SOURCE_SNAPSHOT}}

## Complete legal base test suite

{{BASE_TEST_SUITE}}

## Eligible surviving mutants from this checkpoint

These mutants survived the complete base suite, but their specification relevance and
equivalence have not been established. Use them only as hints.

```json
{{ELIGIBLE_MUTANTS}}
```

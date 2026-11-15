# Mutation-Guided Test Generation — Paired C1 Cohort

Generate deterministic pytest tests for the supplied frozen C1 checkpoint.

Use only the C1 specification, source, complete legal C1 base suite, shared gap brief,
and same-checkpoint surviving mutants below. The shared gap brief is identical to the
one used for Vanilla generation. Do not use hidden tests, future checkpoints, later
failures, or prior generated-test results.

The mutant records have survived the stable passing C1 tests, but their equivalence and
specification relevance are not established. Treat each diff only as a hint. Do not force
a test for every mutant. A candidate must assert externally observable behavior required
by the specification, pass on unmodified C1, and name only mutants its exact input is
expected to kill. If no surviving mutant supports an additional specification-correct
test, return an empty `tests` array and explain the limitation in `assessment`.
`target_function_reached_in_baseline` says whether stable C1 tests recorded a subprocess
hit for the containing function. A missing value means unknown; even `true` does not prove
the exact mutated line ran. Use the specification, not reachability metadata, as the oracle.

For the shared gap brief, cover every gap marked required when the supplied evidence
supports a deterministic expected assertion. For optional gaps, do not invent fixture
data, expected routes, timings, output wording, or other behavior. Tests must be
independent of wall-clock time, randomness, network access, machine-specific paths, and
private implementation state.

Return at most `{{MAX_CANDIDATE_FILES}}` files and at most
`{{MAX_CANDIDATE_TEST_FUNCTIONS}}` top-level test functions total. Use one top-level
test function per file. Each path must be a root-level `test_<name>.py` filename.
Return exactly one JSON object matching the supplied schema, including a non-empty
`assessment`; do not add Markdown or prose outside the JSON.

## Shared specification-gap brief

{{TEST_OPPORTUNITIES}}

## Current checkpoint specification

{{SPECIFICATION}}

## Current checkpoint source snapshot

{{SOURCE_SNAPSHOT}}

## Complete legal base test suite

{{BASE_TEST_SUITE}}

## Same-checkpoint surviving-mutant records

```json
{{ELIGIBLE_MUTANTS}}
```

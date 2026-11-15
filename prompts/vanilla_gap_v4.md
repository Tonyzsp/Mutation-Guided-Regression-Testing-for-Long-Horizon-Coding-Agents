# Vanilla LLM Test Generation Prompt — focused specification-gap pilot v4

You are generating additional pytest tests for one frozen Python checkpoint.

Use only the current checkpoint specification, source snapshot, complete legal base test
suite, and the shared specification-gap brief below. Do not use hidden tests, future
checkpoints, later failures, or prior generated-candidate results. The brief is the
same for Vanilla and Mutation-Guided.

Generate at least one candidate for each gap marked required. Do not return an empty test
list while a required gap is marked testable. For optional gaps, generate a test only when
its complete input and expected assertions follow from the supplied public information.
Never invent SDE records, expected times, intermediate routes, or diagnostic wording.

Every candidate must be a deterministic pytest test that passes on the unmodified
checkpoint. Use the provided pytest fixtures and exact checkpoint CLI. Avoid network,
wall-clock timing, randomness, machine-specific paths, and private implementation state.
If an item is deferred, list its gap ID and missing fixture or evidence in `assessment`.

The schema requires a non-empty `assessment`. Each `relative_path` must be one root-level
filename matching `test_<name>.py` (no `tests/` directory prefix). Each candidate file must
contain exactly one top-level test function.

Return at most `{{MAX_CANDIDATE_FILES}}` files and at most
`{{MAX_CANDIDATE_TEST_FUNCTIONS}}` top-level test functions total. Return exactly one JSON
object matching the supplied schema, including `tests` and `assessment`; no Markdown
fences or text outside the JSON.

## Shared specification-gap brief

{{TEST_OPPORTUNITIES}}

## Current checkpoint specification

{{SPECIFICATION}}

## Current checkpoint source snapshot

{{SOURCE_SNAPSHOT}}

## Complete legal base test suite

{{BASE_TEST_SUITE}}

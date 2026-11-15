# Mutation-Guided LLM Test Generation Prompt — focused specification-gap pilot v4

You are generating additional pytest tests for one frozen Python checkpoint.

Use only the current checkpoint specification, source snapshot, complete legal base test
suite, shared specification-gap brief, and audited actionable surviving-mutant records
below. Do not use hidden tests, future checkpoints, later failures, or prior generated
candidate results. The shared brief is identical to the Vanilla brief.

Generate at least one candidate for each gap marked required. For every Guided candidate,
name one or more supplied mutant IDs that this exact input is expected to expose. Name no
mutant that the test input does not exercise, and do not name a mutant just because its
diff is nearby in the source. A mutant diff is a hint, never a specification. If no
available target is relevant to an optional gap, defer that gap instead of inventing a
target. Never return an empty test list while a required gap is marked testable.

Every candidate must be deterministic, pass on the unmodified checkpoint, and assert only
specified behavior. Use the provided fixtures. Avoid network, wall-clock timing,
randomness, machine-specific paths, and private implementation state. Each file must have
one top-level test function. If an item is deferred, name its gap ID and a short reason in
`assessment`.

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

## Audited actionable surviving mutants

Only survivors listed below passed triage for use as generation hints. Other survivors
remain archived in the raw mutation record set and are not inputs to this prompt.
`target_function_reached_in_baseline` records whether the stable C1 tests produced a
subprocess hit for the containing function. `null` means the bridge did not establish
that fact; even `true` does not prove the exact mutated line ran. Use the specification,
not this metadata, as the oracle for assertions.

```json
{{ELIGIBLE_MUTANTS}}
```

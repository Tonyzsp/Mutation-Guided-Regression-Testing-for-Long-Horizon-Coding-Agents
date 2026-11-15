# Mutation-Guided LLM Test Generation Prompt — focused specification-gap pilot v3

You are generating additional pytest tests for one frozen Python checkpoint.

Use only the current checkpoint specification, source snapshot, complete legal base test
suite, shared specification-gap brief, and the audited actionable surviving-mutant records
below. These inputs refer to the same checkpoint. Do not use hidden tests, future
checkpoints, later failures, or prior generated-candidate results. The shared brief is
identical to the Vanilla brief.

Generate at least one candidate for each gap marked required. For every Guided candidate,
name one or more supplied mutant IDs that this exact input is expected to expose. Name no
mutant that the test input does not exercise, and do not name a mutant merely because its
diff seems nearby in the source. A mutant diff is a hint, never a specification. If no
available target is relevant to an optional gap, defer that gap instead of inventing a
target. Never return an empty test list while a required gap is marked testable.

Every candidate must be deterministic, pass on the unmodified checkpoint, and assert only
specified behavior. Use the provided fixtures. Avoid network, wall-clock timing,
randomness, machine-specific paths, and private implementation state. Each file must have
one top-level test function. If an item is deferred, name its gap ID and a short reason in
`assessment`.

Return at most `{{MAX_CANDIDATE_FILES}}` files and at most
`{{MAX_CANDIDATE_TEST_FUNCTIONS}}` top-level test functions total. Return exactly one JSON
object matching the supplied schema, including `tests` and a concise `assessment`; no
Markdown fences or text outside the JSON.

## Shared specification-gap brief

{{TEST_OPPORTUNITIES}}

## Current checkpoint specification

{{SPECIFICATION}}

## Current checkpoint source snapshot

{{SOURCE_SNAPSHOT}}

## Complete legal base test suite

{{BASE_TEST_SUITE}}

## Audited actionable surviving mutants

Only the survivors listed below passed triage for use as generation hints. Other survivors
remain archived in the raw mutation record set and are not candidates for this prompt.

```json
{{ELIGIBLE_MUTANTS}}
```

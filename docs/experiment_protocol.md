# RQ1 Experiment Protocol

## Scope and frozen benchmark inputs

This protocol covers RQ1 only: whether tests generated at checkpoint `C_i` detect more
later regression candidates than the Original base suite under the same generation budget.
The SlopCodeBench runner and problem definitions are pinned in
[`config/benchmarks.lock.json`](../config/benchmarks.lock.json). Do not silently update either
checkout during an experiment.

The Release provides 196 checkpoint packages across 36 task trajectories. The first runtime
integration example is `eve_route_planner/C1`; the supplied packages are candidate frozen
trajectories and do not need to be regenerated unless the team rejects their provenance. The
upstream `hidden` group is excluded from every experiment flow and is isolated from generator
inputs.

## Data-access policy

| Artifact | Agent | Part B / test generator | Part A evaluator |
| --- | --- | --- | --- |
| Current checkpoint specification | Yes | Yes | Yes |
| Current checkpoint workspace | Yes | Yes | Yes |
| Complete legal `base_tests` from the source checkpoint package | Yes | Yes | Yes |
| Generated tests frozen at the source checkpoint | No | Yes | Yes |
| Upstream `hidden` cases from any checkpoint | No | No | No - excluded from this study |
| Future checkpoint specs, workspaces, and tests | No | No | Yes, only after a suite is frozen |
| Reference solutions | No | No | No - excluded from regression adjudication |

`hidden` files must never be copied into a checkpoint package, generated-test prompt, test
generation workspace, adjudication bundle, or version-controlled experiment output.

Original is the complete legal `base_tests` bundle supplied for the source checkpoint. Do not
create an additional visible/held-out split. The Release isolation manifest places
`file_backup/errors` under `data/excluded_tests/`; it stays outside all generators and RQ1
evaluation inputs. The exact package `base_tests` hashes are recorded in the C1 baseline audit.

## Candidate-regression and adjudication rule

A candidate event is created when a test frozen at `C_i` passes on `C_i`, fails on a later
checkpoint `C_j`, and fails again in three targeted reruns. A candidate has a stable `event_id`, a
neutral test identifier, and a minimal reproduction log. Failures caused by test collection,
timeout infrastructure, or an unfrozen test are not candidates.

Each candidate is adjudicated after all suites and checkpoints for the experimental unit are
frozen. A confirmed regression has a `review_status` of `confirmed_regression` and satisfies all
conditions below:

1. the frozen test passed on the source checkpoint and fails reproducibly on the target checkpoint;
2. the behavior asserted by the test is required by the source specification;
3. the earlier requirement remains applicable at the later checkpoint; and
4. the failure reproduces in the frozen environment and is not an infrastructure error.

Adjudication is LLM-only because the project does not have enough human-review capacity. Before
opening any later-checkpoint failure results, freeze the judge model/version, reasoning setting,
prompt, output schema, and confidence rule. The judge receives the same review bundle for every
method and is blinded to whether the test is Original, Vanilla, or Mutation-Guided. The bundle
contains the source and target specifications, checkpoint manifests and hashes, a neutralized
failing test/reproduction, failure logs, and the C1-to-target code diff.

Record the judge's raw verdict, confidence, rationale, evidence references, and whether it would
recommend human review. No human review will be performed. Map any case with confidence below
`0.80`, a malformed/failed judge response, or `human_review_recommended=true` to
`inconclusive`; do not count it as a confirmed regression or another resolved outcome. Only
LLM-adjudicated cases meeting the frozen confidence rule contribute to the reported counts. Label
the results as LLM-adjudicated and report the confidence threshold and unreviewed uncertainty as a
limitation. Resolved verdicts are `confirmed_regression`, `expected_spec_change`,
`preexisting_defect`, `invalid_test`, or `environment_failure`; unresolved cases are
`inconclusive` and are not counted.

Duplicate tests diagnosing the same behavioral defect are assigned one `event_id`; one event can
be detected by many tests but is counted once per method.

## Analysis boundary without a held-out oracle

The candidate-event denominator is the deduplicated union of candidates exposed by at least one
frozen suite and confirmed by adjudication. Report each method's count of detected confirmed events
over this observed union, but do **not** call it an estimate of every real regression in the
trajectory or claim an absolute Regression Detection Rate. A method that finds no candidate cannot
contribute an event to this denominator; this selection effect is a limitation to report.

The headline RQ1 result is therefore: confirmed, adjudicated candidate regressions detected per
method under equal generation budgets. Report raw candidate counts, adjudication outcomes, and the
number of events shared or unique to each method.

## Pilot workflow

1. Verify the sanitized checkpoint packages with `python scripts/validate_checkpoint_package.py`.
2. Confirm that the Release snapshots and run metadata satisfy the team's frozen-trajectory
   provenance requirements; do not regenerate the coding-agent trajectory by default.
3. B receives only the sanitized source checkpoint package, its specification, workspace, and
   complete legal base tests. Benchmark static assets are mounted read-only through the runtime
   adapter; the upstream problem test directory is not exposed.
4. Record the agent/model, environment, prompt, checkpoint hashes, and available run metadata.
   The Release excludes raw agent conversation logs, so decide whether the supplied manifest and
   aggregate records are sufficient.
5. After B freezes all three suites, A runs them unchanged on applicable later checkpoints.
6. Create blinded candidate bundles for reproducible failures, adjudicate them using the fixed
   LLM-only judge, mark low-confidence or flagged cases inconclusive, then map deduplicated event
   IDs back to methods.

## Formal cohort execution status (updated 2026-11-14)

| Requirement | Status | Evidence / next action |
| --- | --- | --- |
| Runner and problem source pinned | Complete | `config/benchmarks.lock.json`; the Release snapshots and run manifests are accepted as the frozen trajectories for this cohort |
| Checkpoint packages and visibility | Complete | 196 packages across 36 tasks; sanitized C1 `base_tests` are the shared legal suite; hidden, future, excluded, and reference inputs stay outside generators |
| C1 baseline | Complete | 36/36 tasks audited for three repetitions; 26 stable-with-failures, 10 stable-pass, zero unusable; audit SHA-256 is recorded in [pre-evaluation readiness](pre_evaluation_readiness.md) |
| Coverage integrity | Complete | 36/36 C1 tasks linked to the final base-audit hash; no audit errors |
| Runtime and mutation execution | Validated at representative scale | Direct mutmut and subprocess association work on `cfgpipe` and `eve_route_planner`; hash-matched mutant summaries exist for 8/36 tasks |
| Vanilla C1 suites | Generation and validation artifacts available | 36 hash-linked manifests; 48 retained candidates across 25 suites. Complete the final audit against the canonical paired budget and candidate-review records |
| Mutation-Guided C1 suites | Pilot only | 4 hash-linked manifests across 36 tasks; two have test files. Complete survivor triage and budget-matched generation/validation across the cohort |
| Candidate correctness review | Pilot calibrated, integration pending | Standalone LLM reviewer distinguishes one supported and one contradictory candidate; integrate into both generation cohort workflows and archive every verdict |
| Formal paired generation budget | Needs final lock | Existing configs include formal and pilot variants. Select one canonical paired budget and reference its hash in both method runs |
| Later-checkpoint evaluation | Not started | Begin only after all Original/Vanilla/Guided suites and the 36-task pre-evaluation integrity manifest pass |
| Regression adjudication and RQ1 report | Not started | Use the same frozen method-blind LLM judge for every suite; low-confidence or malformed reviews remain inconclusive |

The validation phase is closed for method feasibility; the project is in full development for the remaining cohort outputs. See the [phase exit review](../reports/validation_phase_exit_20261114.md) for detailed evidence, limits, and development gates.

### Historical Release revalidation note (2026-10-19)

The original Release archive was restored before reapplying isolation because an earlier sanitizer
mistook a generic JSON field name for a test-case ID and removed legal test data. The corrected
sanitizer uses explicit category markers and case-ID fields; package validation covers all 196
packages. Historical outputs whose input hashes do not match the final sanitized packages are not
eligible for the formal comparison.

The Release metadata for `file_merger` checkpoints C1-C4 did not match the archived `base_tests`
content. The exporter records the actual sanitized content hashes; any formal run must use those
hash-linked package values rather than silently trusting the upstream metadata.

## Earlier Gate 2 package integration record

See [Gate 2 Checkpoint Integration](gate2_checkpoint_integration.md) for the package/schema
alignment and 196-package isolation audit. Its runtime status is historical; use the formal
cohort status table above for the current execution state.

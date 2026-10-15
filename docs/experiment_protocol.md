# RQ1 Experiment Protocol

## Scope and frozen benchmark inputs

This protocol covers RQ1 only: whether tests generated at checkpoint `C_i` detect more
later regression candidates than the Original visible suite under the same generation budget.
The SlopCodeBench runner and problem definitions are pinned in
[`config/benchmarks.lock.json`](../config/benchmarks.lock.json). Do not silently update either
checkout during an experiment.

The first pilot candidates are `file_backup` and `eve_route_planner`. Both expose an explicit
`tests/data/checkpoint_N/{core,hidden}` layout. The project does **not** use the upstream
`hidden` group as an oracle: it is excluded from every experiment flow. Other problems are
deferred until their visible test inputs can be documented and packaged reproducibly.

## Data-access policy

| Artifact | Agent | Part B / test generator | Part A evaluator |
| --- | --- | --- | --- |
| Current checkpoint specification | Yes | Yes | Yes |
| Current checkpoint workspace | Yes | Yes | Yes |
| `core` cases from the source checkpoint | Yes | Yes | Yes |
| Generated tests frozen at the source checkpoint | No | Yes | Yes |
| Upstream `hidden` cases from any checkpoint | No | No | No - excluded from this study |
| Future checkpoint specs, workspaces, and tests | No | No | Yes, only after a suite is frozen |
| Reference solutions | No | No | No - excluded from regression adjudication |

`hidden` files must never be copied into a checkpoint package, generated-test prompt, test
generation workspace, adjudication bundle, or version-controlled experiment output.

For the pilot, Original means the source checkpoint's `core` cases only. The `errors` group in
`file_backup` is excluded until the team documents a separate policy for expected-error
behavior. This prevents a post-hoc change to the baseline.

## Candidate-regression and adjudication rule

A candidate event is created when a test frozen at `C_i` passes on `C_i` and fails on a later
checkpoint `C_j`. A candidate has a stable `event_id`, a neutral test identifier, and a minimal
reproduction log. Failures caused by test collection, timeout infrastructure, or an unfrozen test
are not candidates.

Each candidate is adjudicated after all suites and checkpoints for the experimental unit are
frozen. A confirmed regression has a `review_status` of `confirmed_regression` and satisfies all
conditions below:

1. the frozen test passed on the source checkpoint and fails reproducibly on the target checkpoint;
2. the behavior asserted by the test is required by the source specification;
3. the earlier requirement remains applicable at the later checkpoint; and
4. the failure reproduces in the frozen environment and is not an infrastructure error.

The primary adjudicator is a human reviewer. An LLM reviewer may provide an independent second
opinion, but it receives the same review bundle and cannot overrule the human. The bundle contains
the source and target specifications, source and target workspace snapshots, a neutralized failing
test/reproduction, and logs. It must not identify whether the test was Original, Vanilla, or
Mutation-Guided. Reviewer outcomes are `confirmed_regression`, `expected_spec_change`,
`preexisting_defect`, `invalid_test`, `environment_failure`, or `inconclusive`. Disagreements and
all `inconclusive` outcomes require human resolution before analysis.

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

1. Verify the pinned problem checkout with `python scripts/audit_problem_layout.py`.
2. Create a separate agent workspace for `C1`; supply only the current specification, static
   assets, and permitted context. Do not mount the problem test directory.
3. Run the agent with a recorded model, agent version, prompt, environment, timeout, and seed.
4. Freeze the workspace, its commit/hash, logs, and original-core test results as a checkpoint
   package.
5. Create the visible-test bundle from `core` only. Part B receives that bundle and the package,
   never the upstream problem checkout.
6. After Part B freezes a suite, run Original, Vanilla, and Mutation-Guided suites unchanged on
   later frozen checkpoints.
7. Create and blind-review candidate bundles for reproducible later-checkpoint failures, then map
   confirmed event IDs back to methods for aggregation.

## Gate 1 status (2026-10-15)

| Requirement | Status | Evidence / next action |
| --- | --- | --- |
| Runner source pinned | Complete | `config/benchmarks.lock.json` |
| Problem source pinned | Complete | `config/benchmarks.lock.json` |
| Candidate tasks identified | Complete | `manifests/task_inventory.csv` |
| Visible/excluded partition audited | Complete for the two pilot candidates | `manifests/test_visibility.csv` |
| Python/`uv` environment | Blocked locally | `uv` was not available on this machine |
| Docker execution environment | Blocked locally | Docker CLI was not available on this machine |
| Agent/model/version/prompt fixed | Pending team decision | Record in the first checkpoint package |
| Pilot trajectory | Pending environment and agent credentials | Start with `file_backup` |

The two local-environment blockers must be resolved before any result is treated as an
experimental result. They do not block the repository, visibility, or schema work above.

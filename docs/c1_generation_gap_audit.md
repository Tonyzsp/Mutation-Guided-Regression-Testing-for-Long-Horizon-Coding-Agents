# C1 Test-Gap and Generation-Configuration Audit

Date: 2026-10-18

## Cohort data-integrity correction (2026-10-19)

A Windows long-path traversal bug affected recursive checks and prompt construction. The
first corrected dry run found 2,891 later-checkpoint or hidden fixture files still present
in three package trees: `layered_config_synthesizer`, `metric_transform_lang`, and
`sheeteval`. The isolation tool now walks those trees with extended-length paths. The files
were moved to `data/future_tests` or `data/hidden_tests`, and the package, index, and
isolation-manifest hashes were refreshed. The full audit now passes for all 196 packages
across 36 tasks.

The same traversal bug had caused the completed Vanilla prompts to omit legal C1 files for
those three tasks. The corrected enumeration sees 152/152 files for
`layered_config_synthesizer` (the archived prompt recorded 31), 185/185 for
`metric_transform_lang` (182), and 17/17 for `sheeteval` (12). The missing files were not
hidden or future inputs; they were deep legal C1 fixtures. Path-aware enumeration is fixed
in the Vanilla and shared Guided prompt pipeline, and shared gap briefs were rebuilt for
these tasks. Their archived generation responses are retained, but their frozen outputs
must be superseded by one corrected generation call each before they can join the final
Vanilla cohort. The replacements remain within the declared per-unit generation budget.

The complete three-repeat C1 baseline audit is being rerun against the newly sanitized
package hashes. Its outputs, not the earlier partial run, will be the baseline references
for the repaired cohort.

## Scope

This audit compares only the sanitized `eve_route_planner/C1` specification, source,
and `base_tests` supplied to the generators. It does not use hidden or future test data.
The package has one populated core case: a route from the Jita station in the base fixture
to the Tama station. The hidden-case parametrization has no case data in the sanitized
package; the real C1 base-suite run therefore reports one pass and one skip.

## Explicit specification gaps

| Specification behavior | Current base-test coverage | C1 evidence / generation opportunity |
| --- | --- | --- |
| Start at a station, route to and dock at a station | One Jita-to-Tama case | Covered once; this is the only populated core case. |
| Start at a system and end at a station in that same system | Not covered | Specification Example 2 gives the exact Jita-to-Jita-station case. Running it on C1 produced the specified `START`, `DOCK`, and `DONE` output. This is the clearest first generated test. |
| Start at a system and route to a station in another system | Not covered | A C1 probe from Jita to the Tama station succeeds without `UNDOCK`. The test can assert the specified start/output shape and destination without inventing SDE data. |
| End at a system rather than a station | Not covered | A C1 probe from the Jita station to Tama succeeds without a `DOCK` line. The specification says arrival in the end system completes the route. |
| Treat stargate connections as bidirectional | Only the Jita-to-Tama direction appears | A reverse Tama-to-Jita C1 probe succeeds. A test can check that the reverse trip is reachable and has the expected system endpoints. |
| Zarzakh six-hour return lock, docking not resetting the lock, re-entry changing the lock, and waiting when faster | Not covered | Clear specification requirements, but require a small synthetic SDE graph to make each route and timing expectation deterministic. |
| Numeric CLI bounds (`align`, `warp-speed`, `dock-time`, `gate-time` positive; `top-speed` nonnegative) | Only valid numeric values | Direct CLI error-path candidates; assert nonzero exit for invalid inputs without pinning unspecified error wording. |
| Warp timing and minute rounding across short/no-cruise and long/cruise distances | One parameter combination and one expected route time | Not systematically covered. A synthetic SDE with controlled coordinates is preferable to guessing distances from the external SDE asset. |

## Why the v1 generation calls returned empty

The final Vanilla and Guided calls returned valid structured `{"tests": []}` responses;
the generator and schema did not fail. The prompt templates explicitly permitted an empty
array whenever the model judged that it could not establish a safe test. They asked for
additional tests in general but did not require a clause-by-clause coverage-gap pass or
provide a ranked list of concrete uncovered requirements. The output schema also had no
field for explaining which requirements were considered and why they were skipped.

The Guided call received all 26 raw survivors. Some are likely irrelevant or equivalent by
inspection: for example, changing the default argument of `adjacency.setdefault` does not
change behavior for system IDs already inserted into `adjacency`; the distance-zero warp
mutants also need careful treatment because the specification does not clearly define a
zero-length warp. These should be triaged before generation rather than presented as 26
equally actionable goals. The six system-start mutants and the mutations that redirect
stargate adjacency are more promising specification-linked targets.

The configured `gpt-6-luna` reasoning effort is `low`. The run's output-token cap was not
binding (the Guided answer used 14 output tokens), and the eight-file cap was not binding
(zero candidates were emitted). The nominal three-call allowance is also not three
generation attempts: two calls are reserved only for failures before structured output, so
a valid empty response ends that run. Increasing candidate or output limits alone would not
address the observed problem.

## Exploratory generation results (2026-10-18)

The original v1 empty responses remain unchanged. A shared gap brief plus medium reasoning
produced a Vanilla v2 candidate for Example 2; it passed validation and was frozen. Guided
v2 emitted five candidates but retained none. Review showed the main configuration problems:

- The model targeted `hauling.x_main__mutmut_216` on two system-start tests, but the mutant
  survived. Source review suggests `None` to `""` is equivalent on the specified valid
  system-start routes, so this mutant should not have been offered as actionable.
- One station-start test named system-start mutants it did not exercise and its target run
  timed out.
- One reverse-route candidate failed its C1 run because it compared the route token while
  it still included the `GO: ` output label.
- One numeric-bound candidate appended duplicate CLI flags, relying on behavior the
  specification does not define.

The first v3 retry also exposed a tooling configuration mistake. The run used the legacy
output schema, which did not require `assessment`; the model returned three candidate tests
without that field and with paths outside the gap schema. The response and call record are
preserved, but the run is not treated as valid. The generation tools now take the output
schema from the budget config and record its hash in the input manifest.

All 26 survivors are now classified in [`c1_mutant_triage_v1.json`](c1_mutant_triage_v1.json).
The raw survivor file is unchanged and hash-checked. Seven mutations tied to public system
start and bidirectional-gate behavior were sent as Guided hints; six warp mutations were
deferred until a controlled synthetic SDE exists; five are marked suspected equivalent;
eight are outside the current public-behavior target.

The paired medium-reasoning v4 pilot used one model call and three candidate files per
method under the same call, token, file, and repeat-run limits:

- Vanilla v4b retained all three tests: Example 2; system start to the Tama station; and
  the reverse Tama-to-Jita route. Each passed collection and three C1 runs. Frozen suite
  hash: `0c12a2b0d198141847528876545beba69fb6bb1b65345179c44bc23120dc20cf`.
- Guided v4 retained two system-start tests. Each killed all five declared system-start
  mutants (`211`–`215`) and passed three C1 runs. A third candidate passed C1 but failed to
  kill either adjacency target (`154`, `159`), so it was correctly rejected. Its frozen
  suite hash is `da64de84fbad274eba5ef41789a34c95bc7f8bc1486bb35a7b7607acb0c3db18`.
- The underlying base suite remained `1 passed, 1 skipped` in both validators. The added
  tests are useful C1 behavior checks, but no later-checkpoint regression evaluation has
  been performed.

An isolated engineering probe was then added at
[`../fixtures/eve-route-planner-synthetic-sde-v1/`](../fixtures/eve-route-planner-synthetic-sde-v1/).
It creates two systems and a single directed jump row, then checks that both route directions
work. On the unchanged C1 source, both directions matched the expected output. Temporary
copies with the exact recorded diffs for survivors `154` and `159` each failed with `no route
found`, so this fixture distinguishes both mutations. The result record is
`artifacts/results/rq1_full_36x1/eve_route_planner/C1/synthetic_sde_probe_v1/probe_results.json`.
This was a direct local source-diff probe using Python 3.13, not a mutmut run: Docker
Desktop's named pipe denied access in this process. The fixture is engineering evidence
only; it has not been sent to an LLM, validated through the formal Guided pipeline, or added
to either frozen suite.

The same fixture was rerun on 2026-11-14 in an isolated copy of the v3 mutmut runtime with
the pinned `rq1-b-tools:python312` Docker image. The unchanged source passed, and
`mutmut apply` followed by the exact pytest node killed both mutants 154 and 159. The source
hash was restored afterward. Machine-readable evidence is in
`artifacts/results/rq1_full_36x1/eve_route_planner/C1/synthetic_sde_probe_v1_docker/probe_results.json`.
This upgrades the engineering probe's execution evidence; it remains outside the official
base suite and frozen method suites.

On 2026-11-14, a separate one-call GPT-6 Luna Guided pilot generated a candidate for this
same synthetic bidirectional-jump requirement, using one selected representative survivor
(154) from the behavior family. The candidate creates its own temporary two-system SDE,
checks both route directions through the public CLI, passed the complete legal C1 suite in
3/3 runs, and killed survivor 154 in 3/3 fresh mutant applications. It is frozen separately
with suite SHA-256
`d6a10291c5629a8bc562c65c9d385bbf9058f176b9f9a50568570df375c8b621`; generation and
validation evidence is under
`artifacts/results/rq1_full_36x1/eve_route_planner/C1/guided_synthetic_bidirectional_pilot_freeze_retry_20261114/`.
The correctness verdict is a local Codex specification review, not independent human
review. This remains an engineering-only synthetic fixture and is not included in either
official RQ1 method suite.

Two more isolated Guided probes were completed on 2026-11-14. The Zarzakh lock-duration
candidate uses a three-system graph with zero gate distances; it passed C1 in 3/3 runs and
killed survivors 385 and 393 in 3/3 direct applications each, distinguishing `00:01` and
`07:01` from the required `06:01`. The separately frozen suite is under
`artifacts/results/rq1_full_36x1/eve_route_planner/C1/guided_zarzakh_wait_pilot_20261114/`.

The warp pilot preflighted four station-distance/speed cases against unchanged C1, then
generated one test covering them. It passed C1 in 3/3 runs and killed seven selected
formula mutants in 3/3 applications each. The four original outputs are retained in
`artifacts/results/rq1_full_36x1/eve_route_planner/C1/guided_warp_formula_pilot_20261114/warp_case_oracle_preflight.json`;
the frozen candidate and mutant logs are in
`artifacts/results/rq1_full_36x1/eve_route_planner/C1/guided_warp_formula_pilot_20261114/`.
Both probes used local Codex specification review rather than independent human review
and remain outside official RQ1 suites.

The warp-boundary follow-up audit reclassified survivor 35 as suspected equivalent. At
`D = d_min`, the reduced-peak formula simplifies to `v_max` and the full-speed branch has
zero cruise, so changing `<` to `<=` does not change the specified result. The updated
triage is archived separately under
`artifacts/results/rq1_full_36x1/eve_route_planner/C1/mutant_triage_v3_direct_warp_boundary_audit_20261114/`.

A separate output-rounding probe targeted survivor 394, which changes the six-hour
Zarzakh lock by six seconds. With 27-second gate use and one-second alignment, the
synthetic Alpha–Zarzakh–Beta route takes 21,656 seconds on C1 (`DONE: 06:01`) and
21,662 seconds under the exact diff (`DONE: 06:02`). The paired Docker oracle record is
`artifacts/results/rq1_full_36x1/eve_route_planner/C1/guided_zarzakh_rounding_pilot_20261114/zarzakh_rounding_oracle_preflight.json`.
That preflight exposed function-relative mutmut hunk coordinates, plus a record whose
last context line had no serialized newline. `apply_frozen_mutation_diff` now falls back
to a unique exact old-context match and interprets a missing serialized newline as a
no-newline source marker only when the unified diff explicitly says so.

The first one-call generation attempt was rejected because it located `hauling.py`
relative to the generated test file, which validation places under `base_tests/`. A
separate one-call v2 run explicitly required the supplied `entrypoint_argv` fixture. It
passed the legal C1 suite in 3/3 runs and killed survivor 394 in 3/3 fresh applications.
Although the mutmut summary itself said `survived`, direct source-change plus candidate
failure checks confirmed all three kills. The follow-up semantic triage has 111 eligible,
72 out-of-scope, 39 suspected-equivalent, and 8 deferred survivors; 389 is suspected
equivalent and 372 is deferred pending a public-CLI counterexample. The archived triage is
under `artifacts/results/rq1_full_36x1/eve_route_planner/C1/mutant_triage_v3_direct_zarzakh_followup_audit_20261114/`.
The retained bundle and its separate budget
are under `artifacts/results/rq1_full_36x1/eve_route_planner/C1/guided_zarzakh_rounding_pilot_v2_20261114/`;
the first rejected attempt is preserved under
`artifacts/results/rq1_full_36x1/eve_route_planner/C1/guided_zarzakh_rounding_pilot_20261114/`.
These are additional synthetic engineering pilots, not official RQ1 method suites.

## Remaining work before formal RQ1

1. Synthetic bidirectional, Zarzakh lock-duration, Zarzakh minute-rounding, and warp-formula
   probes now pass under the pinned Docker/mutmut environment and are frozen separately.
   The remaining Zarzakh work is to find an externally observable public-CLI case for
   entry-gate return behavior and re-entry from a different gate. “Docking does not reset
   the lock” is not represented by this single-start/single-end CLI because it has no
   intermediate docking state. Keep all such probes outside official suites unless the
   protocol is updated.
2. Keep the numeric-bound tests in a separate Vanilla-oriented gap run unless a public
   survivor is triaged that the same test is expected to kill.
3. Keep all v1, v2, v3, v4, and v4b records separate. The exploratory outputs do not replace
   frozen formal RQ1 artifacts. A-side still needs to approve the budget and Release
   trajectory provenance before these suites can enter formal checkpoint comparison.

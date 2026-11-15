# Eve Route Planner C1 Mutant Quality Pilot — 2026-11-14

## Scope

This is an exploratory single-task pilot for improving survivor quality before RQ1 evaluation. It does not compare Original, Vanilla, and Guided regression detection, and it does not run later-checkpoint evaluation. The generator saw only the sanitized C1 specification, workspace, legal base tests, a shared gap brief, and the selected same-checkpoint mutant hints. Hidden and future tests were excluded.

## Direct mutation run

The v3 mutmut run produced 605 raw mutants. Raw mutmut labelled all 605 `survived`; the subprocess bridge's raw status was therefore not used as the final outcome. Applying each mutant to the staged C1 source and executing the stable C1 base-test ID classified all 605 without errors:

- Directly killed: 375
- Directly survived: 230
- Inconclusive/errors: 0
- Direct mutation score: 375 / (375 + 230) = 0.6198

The exact source hash was restored after classification and matches the staged C1 manifest. The normalized bundle is `artifacts/results/rq1_full_36x1/eve_route_planner/C1/mutmut_direct_v3_20261114/`.

## Semantic triage and selection

A first-pass review classified all 230 direct survivors against the C1 specification and source. The initial triage recorded 116 eligible, 71 out of scope, 37 suspected equivalent, and 6 deferred; 31 representatives were selected for this exploratory Guided call. The complete decisions and hash are in `artifacts/results/rq1_full_36x1/eve_route_planner/C1/mutant_triage_v3_direct_20261114/`.

A follow-up audit found two zero-bound hints did not have a distinct, specified output oracle: zero-valued timing parameters still fail later in the program, while top-speed zero yields unbounded warp time. The revised triage reclassifies those two as out of scope/deferred and contains 29 selected hints. It is intended for future generation; it does not replace the exact triage hash in the already-frozen pilot.

## Guided generation and validation

GPT-6 Luna (medium) received 31 selected hints under a separate exploratory budget with the same ceilings as the RQ1 config: at most 3 calls, 12,288 output tokens, and 8 candidate files. The run used 1 call (32,673 input tokens; 886 output tokens) and emitted one candidate:

- `test_same_system_station_example.py` checks the specification's exact Example 2 input/output.
- It passed the unchanged C1 checkpoint in 3/3 runs.
- It killed the selected surviving mutant `hauling.x_main__mutmut_501` in 1/1 target run.
- Retained candidates: 1/1.
- Frozen suite SHA-256: `0e43b173156bd589d2202bfe454f4b27c8f6ed11706221bd8cd07913a9a88478`.

See `artifacts/results/rq1_full_36x1/eve_route_planner/C1/guided_mutant_v3_direct_pilot_20261114/`. The model assessment explains that the other hints need controlled SDE topology/coordinates or timing oracles not present in the legal fixture. The selected hints therefore remain a pilot shortlist, not a claim that all 31 are equally actionable.

## Synthetic bidirectional Guided probe

After the isolated Docker probe established a deterministic SDE fixture for survivors 154
and 159, a separate one-call GPT-6 Luna pilot was run using only representative 154 from
that behavior family. The prompt described the synthetic fixture and the public CLI oracle;
it did not include hidden, future, or regression data. The call used 23,530 input tokens and
1,332 output tokens and emitted one candidate, `test_bidirectional_jump_rows.py`.

The candidate passed the full legal C1 suite in 3/3 runs and killed target 154 in 3/3 fresh
mutant applications. The failure was the expected `no route found` behavior after the
forward adjacency insertion was mutated. The candidate was frozen separately with suite
SHA-256 `d6a10291c5629a8bc562c65c9d385bbf9058f176b9f9a50568570df375c8b621`; see `artifacts/results/rq1_full_36x1/eve_route_planner/C1/guided_synthetic_bidirectional_pilot_freeze_retry_20261114/`.
The specification review was performed locally in Codex and is not independent human
review. An initial validation attempt completed its source and target runs but failed while
writing the manifest because the exploratory budget omitted optional provenance fields.
The validator now tolerates absent optional metadata; the completed retry reused the exact
model response and records that recovery in the bundle.

## Zarzakh six-hour lock pilot

A second one-call GPT-6 Luna pilot used a synthetic Alpha–Zarzakh–Beta graph with
co-located gate coordinates. Each gate leg costs two seconds; after entering Zarzakh at
two seconds, the earliest legal departure for Beta is at six hours plus two seconds, so
the CLI must report `DONE: 06:01`. One candidate was emitted and retained. It passed C1
in 3/3 runs and killed both selected survivors in 3/3 direct applications each:

- `hauling.x_main__mutmut_385` reversed which exit is delayed; its route completed at
  `00:01` instead of `06:01`.
- `hauling.x_main__mutmut_393` changed the lock from six to seven hours; its route
  completed at `07:01`.

The frozen suite SHA-256 is
`ec3d9fb07d4a0fc8bf552f38959fc0f2611efb9d627789cee97b55a304291aa8`. The generation used
one call (24,217 input tokens; 1,095 output tokens). Its correctness verdict is a local
Codex specification review, not independent human review. See `artifacts/results/rq1_full_36x1/eve_route_planner/C1/guided_zarzakh_wait_pilot_20261114/`.

## Controlled warp-formula pilot

Before generation, four station-to-station synthetic coordinate cases were run against
unchanged C1 and matched their precomputed CLI minute outputs. One GPT-6 Luna call then
emitted one test function covering those four cases. The candidate passed the full legal
C1 suite in 3/3 runs and directly killed all seven named same-checkpoint mutants in 3/3
runs each:

| Target mutant | Specified change | C1 output | Mutant output |
|---|---|---:|---:|
| `hauling.x_warp_time__mutmut_19` | Dropout-speed divisor | `14:12` | `14:33` |
| `hauling.x_warp_time__mutmut_22` | Positive dropout treated as zero | `14:12` | CLI `OverflowError` |
| `hauling.x_warp_time__mutmut_40` | Short-distance peak-speed denominator | `14:12` | `14:59` |
| `hauling.x_warp_time__mutmut_51` | Acceleration-time logarithm | `14:12` | `10:22` |
| `hauling.x_warp_time__mutmut_30` | Minimum-distance calculation | `22:39` | `23:42` |
| `hauling.x_warp_time__mutmut_43` | Cruise-time numerator | `23:27` | `25:40` |
| `hauling.x_warp_time__mutmut_12` | Deceleration-rate cap | `00:02` | `00:01` |

The suite was frozen separately with SHA-256
`63c5b270a199a7cd8d7f4e578c6f647a48de54a6b2be7bc6e0541d88905c14e9`. The call used one
request (25,813 input tokens; 1,098 output tokens). The review is local Codex review,
not independent human review. The `artifacts/results/rq1_full_36x1/eve_route_planner/C1/guided_warp_formula_pilot_20261114/warp_case_oracle_preflight.json`
records the original-C1 outputs, and the `artifacts/results/rq1_full_36x1/eve_route_planner/C1/guided_warp_formula_pilot_20261114/`
contains the candidate, exact mutant diffs, repeated logs, and suite manifest.

The warp audit also reclassified `hauling.x_warp_time__mutmut_35` from eligible to
suspected equivalent. At `D = d_min`, the short-distance peak expression simplifies to
`v_max`; the long-distance branch has zero cruise. The `<` versus `<=` change therefore
does not create a distinct specified result at that boundary. The follow-up triage has
113 eligible, 72 out-of-scope, 38 suspected-equivalent, and 7 deferred survivors; the
original mutation records remain unchanged.

## Zarzakh minute-rounding follow-up

Survivor `hauling.x_main__mutmut_394` changes the Zarzakh lock from `6 * 3600` to
`6 * 3601` seconds. A preflight used an Alpha–Zarzakh–Beta graph with co-located gates,
one-second alignment, and 27-second gate use. The specified route takes 21,656 seconds
and prints `DONE: 06:01`; the exact archived mutant diff adds six seconds and prints
`DONE: 06:02`. Both outcomes were confirmed in the pinned Docker environment. The
`artifacts/results/rq1_full_36x1/eve_route_planner/C1/guided_zarzakh_rounding_pilot_20261114/zarzakh_rounding_oracle_preflight.json`
also verifies that applying the archived diff by unique source context works when
mutmut's hunk coordinates are function-relative and its record omits the final newline.
The separate follow-up triage now has 111 eligible, 72 out-of-scope, 39 suspected-equivalent,
and 8 deferred survivors: mutant 389 is suspected equivalent by the heap-state equality
`t == dist[state]`, while mutant 372 remains deferred pending a public-CLI counterexample.
See the `artifacts/results/rq1_full_36x1/eve_route_planner/C1/mutant_triage_v3_direct_zarzakh_followup_audit_20261114/`.

The first one-call generation attempt emitted a semantically appropriate test but used
`Path(__file__).parent / "hauling.py"`; the validator places tests under `base_tests/`,
so that path cannot reach the workspace-root executable. It was rejected and retained as
a separate failed attempt with no test retained. A second, separately budgeted one-call
attempt made the supplied `entrypoint_argv` fixture explicit. Its candidate passed the
complete legal C1 suite in 3/3 runs and directly killed mutant 394 in 3/3 fresh
applications. The normal mutmut status said `survived`, but the direct application check
confirmed that the source changed and the candidate failed each time. The suite was
frozen separately with SHA-256
`1448ef8bf2dcdaf7174c02d40beb7158edc9c734bc469466ffd535b3de7f3d8d`; see the
`artifacts/results/rq1_full_36x1/eve_route_planner/C1/guided_zarzakh_rounding_pilot_20261114/`
and `artifacts/results/rq1_full_36x1/eve_route_planner/C1/guided_zarzakh_rounding_pilot_v2_20261114/`.
The v2 call used 23,857 input tokens and 985 output tokens. The correctness review was
performed locally in Codex and is not independent human review. Both attempts remain
synthetic engineering pilots and are excluded from formal RQ1 results.

## Run issue and accounting fix

An initial cohort-wrapper attempt failed three times before any model request because Codex could not open its local state database. Its attempt log over-counted stdout warnings as model requests. The raw log is preserved with an accounting correction in `artifacts/results/rq1_full_36x1/eve_route_planner/C1/guided_mutant_v3_direct_20261114/attempt_accounting_correction.json`. The generation runner now treats this specific app-server initialization error as a pre-request failure.

## Interpretation

The direct classifier and several targeted Guided engineering probes are now available for this C1 task. These results are not a paired RQ1 comparison, are not part of the formal 36-task cohort, and do not answer whether Guided tests detect more later-checkpoint regressions.

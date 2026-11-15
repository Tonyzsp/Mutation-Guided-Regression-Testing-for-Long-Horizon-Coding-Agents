## Required engineering probe: warp-formula outputs

Use one deterministic pytest test function that creates a one-system synthetic SDE
with two stations in that system, an empty jump table, and an empty celestial table.
For each case below, place the start station at x=0 and the destination station at the
listed x coordinate (y=z=0), invoke the public CLI with `entrypoint_argv`, and assert
successful completion plus the listed `DONE` value. Use `--align 1`, `--dock-time 1`,
and `--gate-time 1` for all cases. Station-to-station travel in the same system avoids
route-graph ambiguity. Coordinates are meters, as stated by the C1 specification.

| Case | Distance (m) | `--warp-speed` | `--top-speed` | Expected `DONE` | Selected target mutants |
|---|---:|---:|---:|---|---|
| low-speed short warp | `149597870.7` | `0.001` | `1` | `14:12` | `hauling.x_warp_time__mutmut_19`, `hauling.x_warp_time__mutmut_22`, `hauling.x_warp_time__mutmut_40`, `hauling.x_warp_time__mutmut_51` |
| minimum-distance branch | `299195741400` | `0.001` | `1` | `22:39` | `hauling.x_warp_time__mutmut_30` |
| cruise calculation | `613351269870` | `0.001` | `1` | `23:27` | `hauling.x_warp_time__mutmut_43` |
| deceleration cap | `65542660666484.06` | `10` | `250` | `00:02` | `hauling.x_warp_time__mutmut_12` |

These expected minute values are calculated from the published acceleration, cruise,
deceleration, dropout-speed, and minute-ceiling equations, including three seconds of
fixed station-trip overhead. The selected diffs alter the specified dropout speed,
zero-dropout guard, short-distance peak formula, acceleration term, minimum distance,
cruise term, or deceleration cap. Name a target only if this exact test is expected to
fail on that mutant. Do not name transition-boundary mutant 35: follow-up algebra review
found its `<` to `<=` change equivalent at `D=d_min` because the reduced peak equals
`v_max` and the cruise time is zero.

Check only public CLI output. Do not assert private helpers or implementation state.
This is a synthetic engineering probe and must remain outside official base and RQ1
method suites.

# Shared C1 specification-gap brief

This brief was prepared from the sanitized C1 specification and base tests. Give it to
Vanilla and Mutation-Guided unchanged. It is a checklist of public requirements, not
evidence from a later checkpoint. The base suite has one populated case (Jita station to
Tama station); the other hidden-case parametrization has no case data in this package.

## Required first test

**C1-GAP-01 — Specification Example 2 (system to station in the same system).** The base
suite does not cover this example. Add one deterministic pytest test for exactly this
specification example:

```text
start: Jita
end: Jita IV - Moon 4 - Caldari Navy Assembly Plant
--align 2.1 --top-speed 250 --warp-speed 10.0 --dock-time 5 --gate-time 1
```

The specified output is:

```text
START: Jita
DOCK: Jita IV - Moon 4 - Caldari Navy Assembly Plant
DONE: 00:01
```

Use the supplied `entrypoint_argv` and `sde_dir` fixtures. This is an explicit example in
the current specification, not a behavior inferred from the implementation. Do not use a
machine-specific path or copy tests from any excluded source.

## Additional uncovered requirements, in priority order

- **C1-GAP-02 — Start in a system, finish at a station in a different system.** Check the
  specified starting line, absence of `UNDOCK`, and arrival/docking at the named station.
  The base case supplies the Jita and Tama station names; do not assert unprovided
  intermediate route details.
- **C1-GAP-03 — Finish at a system.** Reuse the base case's Jita station as the start and
  Tama as the end. The route completes on entering Tama, so the output must not contain a
  station `DOCK` operation.
- **C1-GAP-04 — Bidirectional stargate connections.** If testing the reverse Jita/Tama
  route, assert reachability and the correct start/end systems; avoid assuming a unique
  intermediate route unless the test fixture establishes it.
- **C1-GAP-05 — CLI numeric bounds.** Test that `align`, `warp-speed`, `dock-time`, and
  `gate-time` reject zero, and `top-speed` rejects a negative value. Assert nonzero exit
  only; the specification does not define exact diagnostic wording.
- **C1-GAP-06 — Zarzakh lock and waiting rules.** These are explicit but require a small,
  deterministic synthetic SDE graph. Generate only if the fixture and expected result can
  be derived from the specification without relying on hidden data.
- **C1-GAP-07 — Warp timing and rounding.** The base case uses one parameter set only. Use
  controlled synthetic coordinates if testing short/long warp phases or minute rounding;
  do not guess distances from the runtime-mounted SDE.

The generation response must include a short `assessment` field naming which gap IDs were
tested and, for any omitted gap, a concise reason. At minimum, the response must contain a
candidate for C1-GAP-01. Do not manufacture unsupported behavior or force a test for every
mutant.

## Guided-only survivor hints

The C1 system-start mutants `hauling.x_main__mutmut_211` through
`hauling.x_main__mutmut_216` may be relevant to C1-GAP-01/02. A candidate should name only
the IDs it is expected to kill. The adjacency mutants `hauling.x_main__mutmut_154` and
`hauling.x_main__mutmut_159` may be relevant to C1-GAP-04. Do not target mutants merely
because they survived. In particular, the `setdefault` default-value variants are likely
equivalent for system IDs because the adjacency dictionary is prepopulated; verify before
using them as targets. Invalid-start error-message mutations and zero-distance warp changes
are not required by this brief.

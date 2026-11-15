# Shared C1 specification-gap brief — focused pilot v3

This same brief is supplied unchanged to Vanilla and Mutation-Guided. It was built from
the sanitized C1 specification and complete legal base suite. The base suite has one
populated route case: Jita station to Tama station. Do not use hidden tests, future
checkpoints, or results from later checkpoints.

## Required for this focused pilot

**C1-GAP-01 — Specification Example 2: system start to station in the same system.**
Add a deterministic test for the exact input and output shown in Example 2:

```text
start: Jita
end: Jita IV - Moon 4 - Caldari Navy Assembly Plant
--align 2.1 --top-speed 250 --warp-speed 10.0 --dock-time 5 --gate-time 1
```

Expected output:

```text
START: Jita
DOCK: Jita IV - Moon 4 - Caldari Navy Assembly Plant
DONE: 00:01
```

This example is absent from the base tests. Use `entrypoint_argv` and `sde_dir`.

**C1-GAP-02 — System start to a station in another system.** Use the named Jita start
and Tama station already present in Example 1/base fixtures. Check a successful run,
`START: Jita`, no `UNDOCK`, and the final station `DOCK` line. Do not assume a unique
intermediate route or a travel time that is not stated in the specification.

**C1-GAP-04 — Bidirectional stargate connections.** Check the reverse Tama-to-Jita
system route using the named systems already present in Example 1. The `GO: ` line starts
with that label; remove only the label before checking that the first and last system
tokens are Tama and Jita. Do not assume a unique intermediate route.

## Other uncovered requirements

- **C1-GAP-03:** a station start with a system destination completes without a station
  `DOCK` line. Keep for a later focused run; do not target system-start mutants with this
  station-start input.
- **C1-GAP-05:** numeric bounds are explicit. If generated, build each CLI invocation
  from scratch with exactly one value for each option; assert only nonzero exit status.
- **C1-GAP-06:** Zarzakh lock/reset/wait behavior needs a deterministic synthetic SDE.
- **C1-GAP-07:** warp formula and rounding need controlled synthetic coordinates.

For this pilot, prefer the three required gaps above. A test must be deterministic and
pass unchanged on C1. Do not create optional tests only to mention more gaps, and do not
target a mutation unless the input and assertion can trigger its specific changed behavior.
Use the available pytest fixtures. Each candidate file must contain one top-level test
function. The response must include an `assessment` of covered and deferred gap IDs.

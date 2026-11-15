## Required engineering probe: bidirectional jump rows

Target only the specification requirement that stargate connections are bidirectional.
The single Guided hint is one representative direct C1 survivor from this behavior
family. Produce at most one pytest file and name only the supplied target if its exact
input is expected to kill it.

Construct a tiny synthetic SDE under `tmp_path`; do not use the real SDE asset. Write
the four compressed CSV inputs expected by the CLI. Include two systems, exactly one
directed jump row from Alpha to Beta, and matching `Stargate (Beta)` / `Stargate
(Alpha)` celestial rows at the endpoints. The station file may be empty with the
required header. Invoke the public CLI using the existing `entrypoint_argv` fixture.
Check both Alpha-to-Beta and Beta-to-Alpha routes against the required route output,
with deterministic parameters that eliminate subwarp travel and do not depend on
wall-clock time. Keep all assertions at the CLI boundary.

The exact output shape for a system-to-system trip and the bidirectionality rule are
defined in the supplied C1 specification. Do not assert private state or add behavior
not required by that specification. This is an isolated synthetic engineering probe;
do not claim it is an official base test or part of the formal RQ1 suite.

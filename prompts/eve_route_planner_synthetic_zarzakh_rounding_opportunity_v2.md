## Required engineering probe: six-second lock error at the minute boundary

Generate one deterministic pytest test for the specified six-hour Zarzakh
entry-gate lock, using the synthetic Alpha–Zarzakh–Beta SDE and public CLI.
The route uses zero gate coordinates, `--align 1`, `--top-speed 250`,
`--warp-speed 10`, `--dock-time 5`, and `--gate-time 27`.

Use the existing pytest `entrypoint_argv` fixture from the supplied
`base_tests/conftest.py` to invoke the program. Do not build the executable path
from `Path(__file__)`: validation places generated tests inside `base_tests/`,
while `hauling.py` is at the workspace root. The test should call
`subprocess.run(entrypoint_argv + [...])` and pass the generated SDE path via
`--sde`.

Alpha's only neighbor is Zarzakh. Each gate leg costs one second of alignment
plus 27 seconds of gate time. Entering Zarzakh after 28 seconds, the earliest
legal departure for Beta is at 21,628 seconds; the second gate leg ends at
21,656 seconds. Correct output is:

```text
START: Alpha
GO: Alpha (0.5) -> Zarzakh (-0.9) -> Beta (0.3)
DONE: 06:01
```

The selected target is `hauling.x_main__mutmut_394`; its diff changes the lock
from `6 * 3600` to `6 * 3601` seconds, adding six seconds and changing the
rounded output to `DONE: 06:02`.

Assert the exact public route and output. Name only the supplied target mutant.
Do not inspect private functions or state. This is a synthetic engineering
probe, not an official C1 base test or formal RQ1 suite. Emit exactly one
candidate file with exactly one top-level pytest test function.

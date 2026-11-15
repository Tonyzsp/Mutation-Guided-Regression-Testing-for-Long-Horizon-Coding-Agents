## Required engineering probe: six-second lock error at the minute boundary

Generate one deterministic pytest test that checks the specified six-hour Zarzakh
entry-gate lock through the public CLI. Build a synthetic SDE under `tmp_path` with
systems Alpha (security 0.5), Zarzakh (-0.9), and Beta (0.3); jump rows Alpha to
Zarzakh and Zarzakh to Beta; matching endpoint gates; all gate coordinates zero; and
an empty station table. Run from system Alpha to system Beta with `--align 1`,
`--top-speed 250`, `--warp-speed 10`, `--dock-time 5`, and `--gate-time 27`.

Alpha has only one neighbor, so its initial position is the Alpha-to-Zarzakh gate. Each
gate leg costs one second of alignment plus 27 seconds of gate time. The pilot enters
Zarzakh after 28 seconds, may leave for Beta only after six hours have elapsed from
entry, and then spends 28 seconds on the second leg. Correct total: 21,656 seconds,
which rounds up to `DONE: 06:01`. The selected target is
`hauling.x_main__mutmut_394`; its diff changes the lock to `6 * 3601` seconds, adding
six seconds and moving the total to 21,662 seconds, which rounds up to `DONE: 06:02`.

Assert the exact public route and `DONE` output. Name only the supplied target mutant.
Do not inspect private functions or state. This is a synthetic engineering probe, not
an official C1 base test or formal RQ1 suite.

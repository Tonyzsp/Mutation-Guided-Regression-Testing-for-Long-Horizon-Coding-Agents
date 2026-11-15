## Required engineering probe: Zarzakh six-hour gate lock

Test the C1 rule that entering Zarzakh locks the ship to its entry gate for six hours:
before the lock expires, it can leave only back to the system it entered from. The lock
duration and the CLI's rounded total time are public specification requirements. The
single deterministic route below isolates the six-hour wait without depending on real
SDE data or uncertain warp distances.

Construct four compressed CSV files under `tmp_path` for a synthetic SDE:

- Systems Alpha (security 0.5), Zarzakh (-0.9), and Beta (0.3).
- Exactly two jump rows: Alpha to Zarzakh and Zarzakh to Beta.
- Stargate records on both endpoints of both jumps, with all coordinates set to zero.
- An empty station file with the required header.

Run the public CLI from system Alpha to system Beta using `entrypoint_argv`. Alpha's
only neighbor is Zarzakh, so the system-start rule selects that gate. Use positive
parameters `--align 1`, `--top-speed 250`, `--warp-speed 10`, `--dock-time 5`, and
`--gate-time 1`. The zero gate distances make each gate leg cost exactly one second of
alignment plus one second of gate time. The earliest legal itinerary enters Zarzakh
after two seconds, waits until six hours have elapsed from that entry, then spends two
seconds on the second gate leg. The required output is therefore:

```text
START: Alpha
GO: Alpha (0.5) -> Zarzakh (-0.9) -> Beta (0.3)
DONE: 06:01
```

The selected survivor `hauling.x_main__mutmut_385` reverses which Zarzakh exit is
delayed; `hauling.x_main__mutmut_393` changes the wait from six to seven hours. Name
both only if this exact candidate input is expected to kill both. Keep assertions at
the public CLI boundary and do not add undocumented behaviors. This is a synthetic
engineering probe, not an official base test or formal RQ1 suite.

# Eve Route Planner synthetic SDE probe

This probe checks the specification's bidirectional-stargate requirement against a
minimal SDE containing exactly one directed jump row. It is an engineering fixture for
testing whether the route test can distinguish C1 survivors 154 and 159. It is not an
official benchmark input, an LLM-generated candidate, or part of any frozen Vanilla or
Mutation-Guided suite.

The pytest case creates the four required compressed CSV files in `tmp_path`; no official
or hidden benchmark data is modified. To exercise the probe with the pinned C1 environment,
copy the test file into a temporary copy of the C1 runtime's `base_tests` directory and
run the normal checkpoint or targeted-mutant runner against that temporary runtime.

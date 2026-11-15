# Gate 2 Checkpoint Integration

**Date:** 2026-10-17

**Status:** Static package integration, B-tool container smoke, and the real
`eve_route_planner/C1` base-suite/coverage/subprocess-mutmut checks have run. The
subprocess bridge is validated on one targeted mutant; the complete checkpoint
mutation analysis and RQ1 comparison remain outstanding.

## Package contract

The checkpoint package schema now matches the concrete Part A exporter:

- Package-relative source, specification, test, and run-manifest paths.
- base_tests_path and base_tests_sha256.
- test_command represented as a string.

The schema change removes the earlier visible_tests_path naming mismatch. The
standard-library audit in scripts/validate_checkpoint_package.py passed for all 196
packages across 36 tasks. It checked each source/test digest, package path, isolation
manifest entry, and absence of hidden, deferred file_backup/errors, and later
checkpoint test files under generator inputs.

## Isolation correction

The first audit found that the earlier isolation pass had only handled the
data/checkpoint_N layout. It missed hidden and future fixtures in cases, assets, and
other test-data layouts. A further 3,378 files were moved out of generator inputs:

- 347 hidden files moved to data/hidden_tests/rq1_full_36x1/.
- 3,031 future-checkpoint files moved to data/future_tests/rq1_full_36x1/.
- No additional deferred file_backup/errors files remained.

All package base-test hashes and the filtered hashes in
data/checkpoints/ISOLATION_MANIFEST.json were refreshed. The source archive was not
modified. The move summary is in data/checkpoints/ISOLATION_MOVES.json. Both manifests
and the checkpoint data are ignored by Git.

The audit computes hashes in sorted POSIX-relative-path order, so the same package
digest is obtained on Windows and Linux.

The raw Release tree's published test hash also disagrees with the actual archive
contents for file_merger C1-C4. Those four original hashes remain recorded in the
isolation manifest; generator inputs and index entries now use the hashes computed
from the sanitized package contents.

## Runtime smoke status

Docker Desktop's Linux engine is now running, and the package-declared Python 3.12
runtime image has been pulled and pinned by digest. CPython 3.12.15, uv 0.13.1, pytest,
coverage, and mutmut 3.8.0 were verified in an isolated fixture: 2 base tests passed,
four mutants were generated (2 killed, 2 survived), and the candidate boundary test
killed its target mutant. This confirms toolchain execution only, not RQ1 evidence.

The sanitized `eve_route_planner/C1` package was then mounted read-only and its visible
base tests were run with the required `--entrypoint` and `--checkpoint` options. The
test failed because `base_tests/assets/sde_dir/mapSolarSystems.csv.bz2` is missing. Its
`problem_config.yaml` identifies the source asset directory as `sde`; the package's
declared `test_command` omits the two required pytest options. These details are now
captured in [`config/checkpoint_runtime.json`](../config/checkpoint_runtime.json) and
implemented as a staging adapter in [`scripts/prepare_checkpoint_runtime.py`](../scripts/prepare_checkpoint_runtime.py).
The adapter mounts the asset directory read-only and runs the reconstructed command
without changing the source package. Its use is documented in
[`checkpoint_runtime_adapter.md`](checkpoint_runtime_adapter.md).

At the time of this initial check, the workspace did not contain the pinned
`scb-problems` checkout or its SDE files, so the adapter could not stage or execute the
real checkpoint. That resource blocker has since been resolved; see the follow-up
validation below.

## Checkpoint subprocess mutmut follow-up (2026-10-17)

The pinned `scb-problems` checkout and SDE assets were made available. The sanitized
`eve_route_planner/C1` package now runs its visible base suite successfully (1 passed,
1 skipped), and coverage reports 74% line coverage.

The first mutmut run exposed a process-boundary issue: the base tests launch the
benchmark CLI as a child, while mutmut collects function hits in the pytest parent. The
new [`run_checkpoint_mutmut.py`](../scripts/run_checkpoint_mutmut.py) adds a
`sitecustomize` recorder in child processes and a pytest plugin that replays those hits
into mutmut before its stats collector runs. It uses an importable CLI entrypoint and
passes the required checkpoint options in both stats and clean-test phases.

A real targeted run of `hauling.x_read_csv__mutmut_1` completed successfully. The
official C1 test failed on that mutant, and mutmut classified it `killed`; the JSONL log
also maps child trampoline hits to the exact pytest node ID. The run reports 1 killed
and 604 `not checked`, since it deliberately selected only one mutant. This validates
subprocess association but is not a full mutation score. All run artifacts remain
under ignored `tmp/checkpoint-runtime/`.

## Release trajectory evidence

The package contains `agent_run_manifest.json`, a workspace digest, and checkpoint
metadata. The versioned `results/rq1_full_36x1/` directory contains the resolved runner
environment/configuration and per-checkpoint aggregate metrics. Together these provide
substantial provenance for the supplied 36-task trajectory. The Release documentation
states that raw agent conversation logs were excluded, so the team should confirm
whether the supplied manifest and aggregate records satisfy the frozen-trajectory
evidence requirement. Re-generating the trajectory is not required unless the team
rejects the supplied snapshots or requires missing provenance.

## Gate 1 generation settings

B has recorded first-pilot settings in [`gate1_decisions_v1.md`](gate1_decisions_v1.md):
`gpt-6-luna` with low reasoning effort, at most three launches, up to eight candidate
files, no repair calls, and three stable runs. Output token limits are requested targets
because Codex CLI exposes no hard token override. A still needs to confirm the supplied
Release trajectory provenance and the excluded `file_backup/errors` scope before these
settings are treated as jointly ratified for the formal RQ1 comparison.

The first full C1 mutmut status result is preserved under
`artifacts/results/rq1_full_36x1/eve_route_planner/C1/mutmut_full_20261018/`.
Vanilla candidate generation is handled by [`run_vanilla_generation.py`](../scripts/run_vanilla_generation.py);
candidate validation and suite freezing are handled by
[`validate_and_freeze_tests.py`](../scripts/validate_and_freeze_tests.py). These tools
keep generated artifacts local and do not commit them. The authorized C1 Vanilla pilot
completed with zero generated candidates; its frozen suite contains the unchanged base
tests only. See the result record and freeze manifest under
`artifacts/results/rq1_full_36x1/eve_route_planner/C1/vanilla_gpt6luna_low_20261018/`.
Mutation-Guided generation and target-mutant validation are implemented. The C1 survivor
set is source-hash matched (26 mutants). After explicit authorization, the Guided pilot call
to GPT-6 Luna returned no generated candidates; the unchanged base suite passed and the
base-only Guided suite was frozen. See its run record and freeze manifest under
`artifacts/results/rq1_full_36x1/eve_route_planner/C1/mutation_guided_gpt6luna_low_20261018/`.

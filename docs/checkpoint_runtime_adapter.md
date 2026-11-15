# Checkpoint Runtime Adapter

## Scope

[`scripts/prepare_checkpoint_runtime.py`](../scripts/prepare_checkpoint_runtime.py)
stages a sanitized checkpoint package into an isolated Docker workspace. The same
runtime manifest is consumed by the base-suite audit, coverage runner, mutmut runner,
and generated-test validator. The adapter does not modify checkpoint packages or the
pinned benchmark checkout.

The runtime image and benchmark revisions are pinned in
[`config/checkpoint_runtime.json`](../config/checkpoint_runtime.json) and
[`config/benchmarks.lock.json`](../config/benchmarks.lock.json). The Python 3.12 B-tool
image includes mutmut, coverage.py, pytest and the shared SCBench test plugins. The
adapter installs each task's declared `requirements.txt` and test dependencies into
that image's isolated interpreter before execution.

## Data and task configuration

The sanitized Release contains 196 checkpoint packages across 36 task trajectories.
`config/rq1_full_36x1_candidate_units.json` selects only C1 as each trajectory's
generation source and lists later checkpoints as potential evaluation targets. The
generator and mutation analysis use C1 package inputs only.

At runtime, the adapter reads `problem_config.yaml` from the package for the entry file,
Python source files, declared test dependencies and static assets. If the task exists in
the pinned `scb-problems` checkout, the corresponding `config.yaml` fields must match.
Fifteen task directories are absent from that checkout; those packages can use their
own configuration only when no external assets are declared. The five packages that
declare static assets mount them read-only from the pinned checkout. Each asset file is
hashed into the runtime manifest. Missing task directories, undeclared assets, LFS
pointers, unsafe paths, and configuration mismatches stop staging.

## Prepare and run one C1 package

Build the pinned image if needed:

```powershell
docker build --tag rq1-b-tools:python312 --file fixtures/mutmut-smoke/Dockerfile.python312 .
```

The locked benchmark checkout is `external/scb-problems` at commit
`9cd9ca3a51c3d3e2a99d2488a25baf73a2204451`. Stage and run a representative task:

```powershell
python scripts/prepare_checkpoint_runtime.py `
  --package data/checkpoints/checkpoint_packages_rq1_full_36x1/eve_industry/C1 `
  --problems-root external/scb-problems `
  --run
```

The adapter derives the entrypoint and `--checkpoint checkpoint_1` argument from the
package configuration, applies the task's pytest options, and records the exact command,
package and asset hashes, image ID, logs and exit status in `runtime_manifest.json`.
Package-declared base tests stay together; hidden, future, and deferred excluded tests
are not staged.

## C1 baseline audit

Before calculating mutation outcomes or freezing generated tests, record which official
base tests pass at the source checkpoint. Some C1 implementations may have stable
pre-existing failures, and some official test expectations may conflict with the
written specification. Do not count those failures as regressions or let them make every
mutant appear killed.

Run the repeated machine-readable cohort audit with:

```powershell
python scripts/audit_c1_base_suites.py --repeats 3
```

The audit records each test node ID and outcome for every repetition. It distinguishes
stable pass, stable fail, unstable and infrastructure-error outcomes. This record is
the source-checkpoint baseline needed to select green tests for mutmut and to check
generated candidates without treating already-failing base tests as new failures.

## Coverage

`scripts/run_checkpoint_coverage.py` runs branch coverage with subprocess tracing,
combines child-process data, and writes the raw JSON report, text report and manifest to
`artifacts/results/rq1_full_36x1/<task>/<checkpoint>/`. The manifest records numeric line
and branch totals, source hashes, the image and the command.

## Subprocess-aware mutmut

Several SCBench tests invoke the program as a child process. Plain mutmut stats then miss
function hits from that process and can label every mutant `not checked`. Use
[`scripts/run_checkpoint_mutmut.py`](../scripts/run_checkpoint_mutmut.py), which installs
a small child-process tracer and replays those hits into mutmut's stats phase. It also
uses the configured entry module so the trampoline observes the mutated source. Run the
full set without `--mutant`; a targeted run only validates the bridge.

```powershell
python scripts/run_checkpoint_mutmut.py `
  --runtime-dir tmp/checkpoint-runtime/eve_industry/C1-REPLACE_WITH_STAGING_ID `
  --max-children 4
```

The runner stores all statuses, raw logs, source and bridge hashes, subprocess hits and
the command in a fresh ignored `tmp/` staging directory. Export records with
`scripts/export_mutant_records.py` only after a complete run. When the C1 baseline audit
finds stable failures, mutation scoring must select only tests that passed consistently
at C1; preserve the full official suite separately as the Original evaluation suite.

## Verified and pending evidence

- The first repeated base-suite pass covered all 36 C1 units. It recorded 8 stable-pass
  suites, 26 stable-with-failures suites, and 2 runtime staging errors. A follow-up audit
  is correcting three records: `execution_server` needed its declared `httpx` test
  dependency installed; `layered_config_synthesizer` and `metric_transform_lang` needed
  Windows extended-length paths when copying deep base-test trees.
- The C1 runtime now handles top-level, unindented `test_dependencies` lists in the
  pinned `problem_config.yaml` and uses extended-length paths for deep Windows copies.
  Both fixes were checked on representative packages before the targeted audit rerun.
- `eve_industry/C1` has 12 stable passing base-test IDs. Subprocess-aware coverage is
  90.4% line and 78.125% branch. The full 480-mutant run is in progress; a complete
  normalized result set has not yet been exported.
- `eve_route_planner/C1` has a complete 605-mutant record set (579 killed, 26 survived)
  and seven survivors marked eligible by its C1 triage. This is the first available
  full mutation record set for the 36-unit cohort.
- `cfgpipe/C1` has 30 stable passing and 7 stable failing tests. The current full audit
  records these outcomes for all three repetitions.

No later checkpoint has been run as a regression target in this preparation stage.

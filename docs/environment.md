# B-side Environment Status

## Validated smoke environment

- Runtime: Ubuntu 26.04 LTS in WSL2, x86_64; CPython 3.14.4.
- Isolated environment: `~/.venvs/mutmut-smoke`.
- Locked package set: [`requirements-linux-py314.lock`](../fixtures/mutmut-smoke/requirements-linux-py314.lock).
- Runner: [`run_mutmut_smoke.sh`](../scripts/run_mutmut_smoke.sh).
- Schema checker: [`validate_shared_schemas.py`](../scripts/validate_shared_schemas.py), using `jsonschema` 4.26.0.
- Raw smoke logs are local-only outputs under `fixtures/mutmut-smoke/results/`; they are excluded from version control because they contain machine-specific paths and environment details. The committed fixture and lock file reproduce the check.

The smoke fixture completed successfully:

- pytest base suite: 2 passed.
- coverage.py: 100% line coverage of the four executable fixture statements.
- mutmut 3.8.0: generated four mutants; two were killed and two survived the base suite.
- The boundary mutant `amount > 100` → `amount >= 100` survived the base suite; the runner captures its diff and status in local smoke logs.
- The boundary candidate passed on the original source, failed on the applied mutant, and the verification subprocess reported its import path inside the isolated `/tmp` run directory.

The fixture uses `src/fixture_pkg` on disk and imports `fixture_pkg.discount`; this avoids mutmut 3.8's rejection of module names beginning with `src.`.

## Reproducing the smoke run

Install Ubuntu's `python3-pip` and `python3-venv` packages, create `~/.venvs/mutmut-smoke`, and install the exact package set from the lock file. Then run from the repository root inside Ubuntu WSL:

```sh
python3 -m venv "$HOME/.venvs/mutmut-smoke"
"$HOME/.venvs/mutmut-smoke/bin/python" -m pip install -r fixtures/mutmut-smoke/requirements-linux-py314.lock
bash scripts/run_mutmut_smoke.sh
```

The runner copies the fixture to a new `/tmp` directory, so pytest/coverage artifacts and mutmut's cache do not enter the workspace. It writes raw logs and the temp path under the fixture's local-only `results/` directory. Fixture results are toolchain validation only, not RQ1 evidence.

## Notes for the formal environment

Mutmut's current documentation requires `fork` support; the validated Linux container provides it through Docker Desktop's WSL2 backend. The smoke toolchain and base image are pinned for reproducibility, but they remain an environment candidate until Gate 1 ratifies the formal mutmut configuration. The checkpoint-specific launch options are derived in the B-side runtime adapter; benchmark SDE files are mounted read-only from the pinned `scb-problems` checkout.

Official references: [mutmut requirements and usage](https://mutmut.readthedocs.io/en/latest/index.html), [pytest Python-version support](https://docs.pytest.org/en/latest/backwards-compatibility.html#python-version-support), [coverage.py documentation](https://coverage.readthedocs.io/en/7.14.1/).

## Gate 2 runtime check (2026-10-17)

- Docker Desktop is running with its Linux engine. The task's normal sandbox could not
  access the engine pipe; read-only checks with elevated host access confirmed Docker
  Server 29.7.2 and WSL distributions `Ubuntu` and `docker-desktop`.
- Pulled the checkpoint runtime image
  `ghcr.io/astral-sh/uv:python3.12-trixie-slim`, resolved to
  `sha256:e28141cccc682844c7bed0cb7e5b2b4522c1f72f1d5116c7d22710d0d421f7dc`.
  It provides CPython 3.12.15 and uv 0.13.1 on Linux x86_64.
- Built and ran the isolated B-tool smoke under this runtime: pytest 2 passed, coverage
  ran, mutmut 3.8.0 generated four mutants (two killed and two survived), and the
  boundary candidate killed its target mutant. The pinned candidate image is described
  by [`Dockerfile.python312`](../fixtures/mutmut-smoke/Dockerfile.python312); its local
  Docker image ID is `sha256:73cb64e1e40d788158bd38a884c5a32a179615d3a6fb4f4dec4a8a71d996421a`.
- Ran the sanitized `eve_route_planner/C1` package from a read-only mount, supplying its
  required `--entrypoint` and `--checkpoint` options. The visible base test failed
  because `base_tests/assets/sde_dir/mapSolarSystems.csv.bz2` is absent. The package's
  declared `test_command` also omits those required pytest options.
- Added [`config/checkpoint_runtime.json`](../config/checkpoint_runtime.json) and
  [`scripts/prepare_checkpoint_runtime.py`](../scripts/prepare_checkpoint_runtime.py).
  The adapter verifies the pinned `scb-problems` commit, stages only the checkpoint
  workspace and base tests under ignored `tmp/`, mounts the task's SDE read-only, and
  records a runtime manifest. See
  [`checkpoint_runtime_adapter.md`](checkpoint_runtime_adapter.md) for the invocation.
- At the time of this initial Gate 2 check, the local checkout had no `external/scb-problems`
  tree or SDE assets. The follow-up checkpoint-level validation below supersedes that
  status.

## Checkpoint subprocess mutmut validation (2026-10-17)

The pinned `scb-problems` checkout and SDE files are now available locally, and the
isolated `eve_route_planner/C1` base suite runs successfully (1 passed, 1 skipped).
Coverage reports 74% line coverage (165 statements, 31 missed; 72 branches, 19 partial).

The first mutmut attempt found a harness issue: tests start the CLI in a child process,
so parent-only mutmut stats saw no association between executed functions and pytest
tests. `scripts/run_checkpoint_mutmut.py` now bridges those child hits back to the pytest
stats collector. A targeted run of `hauling.x_read_csv__mutmut_1` verified the bridge:
the official C1 case failed on the mutant and mutmut recorded it as `killed`; the hit log
records the pytest case and child function hits. The run listed 1 killed and 604 not
checked because only one mutant was selected. This verifies process association, not a
full mutation score. Omit `--mutant` to evaluate the full set. Artifacts are under the
ignored `tmp/checkpoint-runtime/` directory.

The B tool environment is available for controlled analysis. The checkpoint is not
mutation-ready until the pinned SDE files are available and the adapted base-suite run
passes. No checkpoint-level coverage, mutant, or RQ1 result has been produced.

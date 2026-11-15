# Mutmut Smoke Fixture

This isolated fixture checks the B-side toolchain only. It is not benchmark data and must not be included in RQ1 results.

The base suite covers amounts below and above the threshold. It intentionally omits exactly `100`, so a mutant that changes `amount > 100` to `amount >= 100` should survive the base suite. `tests/candidate/test_discount_boundary.py` contains the corresponding boundary test that should fail on that mutant and pass on the original implementation. The runner verifies the mutant ID/diff, runs the candidate against both versions, and checks that its subprocess imports the mutated file from the isolated Linux run directory.

From the repository root, run in the configured Ubuntu WSL environment:

```sh
bash scripts/run_mutmut_smoke.sh
```

The pinned smoke environment is listed in `requirements-linux-py314.lock`. The Python 3.12 variant was also verified in Docker using `Dockerfile.python312` and `requirements-linux-py312.lock`:

```powershell
docker build -f fixtures/mutmut-smoke/Dockerfile.python312 -t rq1-b-tools:python312 .
$fixture = (Resolve-Path 'fixtures/mutmut-smoke').Path
$runner = (Resolve-Path 'scripts/run_mutmut_smoke.sh').Path
docker run --rm -e MUTMUT_RESULTS_DIR=/tmp/mutmut-results `
  --mount "type=bind,source=$fixture,target=/input,readonly" `
  --mount "type=bind,source=$runner,target=/runner.sh,readonly" `
  rq1-b-tools:python312 bash -lc 'set -euo pipefail; mkdir -p /tmp/repo/fixtures/mutmut-smoke /tmp/repo/scripts; cp -a /input/pyproject.toml /input/src /input/tests /tmp/repo/fixtures/mutmut-smoke/; cp /runner.sh /tmp/repo/scripts/run_mutmut_smoke.sh; cd /tmp/repo; bash scripts/run_mutmut_smoke.sh'
```

The Docker run mounts only this synthetic fixture and its smoke runner; benchmark packages and hidden/future tests are not mounted. Raw logs from WSL runs are written locally to `fixtures/mutmut-smoke/results/` and are not committed; Docker smoke logs stay under `/tmp`. Mutmut's working copy and cache also stay under `/tmp`. These fixture results are not RQ1 data. The Python 3.12 image and tool versions are a reproducible environment candidate; Gate 1 still needs to ratify the final formal tool configuration.

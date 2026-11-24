# RQ2 Vanilla v5 C1 Coverage Result

**Status:** complete on November 23, 2026. This is a descriptive C1
coverage measurement, not a controlled regression-detection comparison.

## Frozen input

- Cohort: `vanilla_formal_v5_20261116`.
- Audit: 36 task manifests, 89 candidates, 65 retained tests, and 26 tasks
  with at least one retained Vanilla test.
- Each task was measured twice from the same prepared C1 runtime: once with
  the legal base suite and once after overlaying only its frozen Vanilla tests.
  The runner verifies package, base-suite, frozen-suite, source, coverage-config,
  and Docker-image consistency before aggregation.

## Cohort-weighted result

| Metric | Original C1 base | Vanilla v5 | Difference |
| --- | ---: | ---: | ---: |
| Statement coverage | 86.39% | 86.94% | +0.55 pp |
| Branch coverage | 75.05% | 76.01% | +0.96 pp |

All 36 paired task measurements completed without a coverage-run failure.
The machine-readable local output is
`artifacts/results/rq1_full_36x1/rq2_vanilla_coverage_v5_20261123/`;
it remains ignored because it includes per-task raw coverage and execution logs.

## Reproduction

```powershell
python scripts/run_vanilla_rq2_coverage.py --preflight-only
python scripts/run_vanilla_rq2_coverage.py --timeout-seconds 1800
```

The result does not supply a Vanilla mutation score. A generated-suite mutation
score requires replaying archived surviving mutants against the frozen suite;
the historical `60.98%` value has no reproducible artifact and must not be
reported.

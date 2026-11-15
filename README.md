# Mutation-Guided-Regression-Testing-for-Long-Horizon-Coding-Agents
CS 6158 PROJECT - Mutation-Guided Regression Testing for Long-Horizon Coding Agents

## Current RQ1 setup

The active research protocol is
[`RQ1_Research_Implementation_Plan_revised.md`](RQ1_Research_Implementation_Plan_revised.md).
Part A's supporting artifacts are:

- [`docs/experiment_protocol.md`](docs/experiment_protocol.md): base-test access, candidate
  regression, and LLM-only adjudication rules.
- [`config/benchmarks.lock.json`](config/benchmarks.lock.json): pinned SlopCodeBench runner and
  problem commits.
- [`manifests/task_inventory.csv`](manifests/task_inventory.csv): locked 36-task, one-trajectory-per-task sweep.
- [`results/rq1_full_36x1/runner_config.resolved.yaml`](results/rq1_full_36x1/runner_config.resolved.yaml): resolved
  configuration used for the completed benchmark trajectory sweep.
- [`results/rq1_full_36x1/`](results/rq1_full_36x1/): versioned aggregate output from the completed
  36-task, one-trajectory-per-task run.
- [`artifacts/README.md`](artifacts/README.md): download and extraction instructions for the
  checkpoint-package GitHub Release asset used by the test-generation team.
- [`schemas/`](schemas): checkpoint-package and candidate-regression contracts.

## Part B preparation

- [`docs/b_generation_protocol.md`](docs/b_generation_protocol.md): draft mutation-analysis,
  generation, budget, validation, and freezing procedure for Part B.
- [`docs/environment.md`](docs/environment.md): pinned and reproducible mutmut smoke environment;
  these checks are toolchain validation, not RQ1 results.
- [`docs/gate2_checkpoint_integration.md`](docs/gate2_checkpoint_integration.md): package
  schema alignment, data isolation audit, and current runtime integration status.
- [`docs/checkpoint_runtime_adapter.md`](docs/checkpoint_runtime_adapter.md),
  [`config/checkpoint_runtime.json`](config/checkpoint_runtime.json), and
  [`scripts/prepare_checkpoint_runtime.py`](scripts/prepare_checkpoint_runtime.py):
  pinned external-asset mapping and isolated checkpoint runtime preparation for the
  `eve_route_planner` pilot.
- [`scripts/isolate_checkpoint_tests.py`](scripts/isolate_checkpoint_tests.py) and
  [`scripts/validate_checkpoint_package.py`](scripts/validate_checkpoint_package.py):
  isolate hidden/future inputs and audit sanitized package hashes before generation.
- [`fixtures/mutmut-smoke/`](fixtures/mutmut-smoke/), [`prompts/`](prompts/), and the B-side JSON
  schemas in [`schemas/`](schemas): isolated fixture, prompt drafts, and data-record contracts.

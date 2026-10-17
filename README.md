# Mutation-Guided-Regression-Testing-for-Long-Horizon-Coding-Agents
CS 6158 PROJECT - Mutation-Guided Regression Testing for Long-Horizon Coding Agents

## Current RQ1 setup

The active research protocol is
[`RQ1_Research_Implementation_Plan_revised.md`](RQ1_Research_Implementation_Plan_revised.md).
Part A's supporting artifacts are:

- [`docs/experiment_protocol.md`](docs/experiment_protocol.md): base-test access, candidate
  regression, and LLM/human adjudication rules.
- [`config/benchmarks.lock.json`](config/benchmarks.lock.json): pinned SlopCodeBench runner and
  problem commits.
- [`manifests/task_inventory.csv`](manifests/task_inventory.csv): locked 36-task, one-trajectory-per-task sweep.
- [`config/rq1_full_sweep_36x1.yaml`](config/rq1_full_sweep_36x1.yaml): fixed configuration for the
  full benchmark trajectory sweep.
- [`results/rq1_full_36x1/`](results/rq1_full_36x1/): versioned aggregate output from the completed
  36-task, one-trajectory-per-task run.
- [`artifacts/README.md`](artifacts/README.md): download and extraction instructions for the
  checkpoint-package GitHub Release asset used by the test-generation team.
- [`schemas/`](schemas): checkpoint-package and candidate-regression contracts.

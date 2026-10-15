# Mutation-Guided-Regression-Testing-for-Long-Horizon-Coding-Agents
CS 6158 PROJECT - Mutation-Guided Regression Testing for Long-Horizon Coding Agents

## Current RQ1 setup

Part A's Phase 1 artifacts are available in:

- [`docs/experiment_protocol.md`](docs/experiment_protocol.md): data-isolation and regression-event rules.
- [`config/benchmarks.lock.json`](config/benchmarks.lock.json): pinned SlopCodeBench runner and problem commits.
- [`manifests/task_inventory.csv`](manifests/task_inventory.csv): pilot-task decision record.
- [`manifests/test_visibility.csv`](manifests/test_visibility.csv): audited `core`/`hidden` partitions.
- [`schemas/`](schemas): checkpoint-package and confirmed-regression-event contracts.

Verify the pinned pilot tasks after cloning the benchmark repositories into the ignored
`external/` directory:

```powershell
python scripts/audit_problem_layout.py --task file_backup --task eve_route_planner
```

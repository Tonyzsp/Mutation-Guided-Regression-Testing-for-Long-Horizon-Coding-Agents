# RQ1 full trajectory sweep — aggregate results

This directory is the Git-versioned, reviewable result package for the completed
Part A trajectory run. It contains aggregate and per-checkpoint metrics only; it
does **not** provide future checkpoint artifacts to a test generator.

## Run identity

| Field | Value |
| --- | --- |
| Run | `rq1_full_36x1` |
| Completed | 2026-10-17 03:51 EDT |
| Problems | 36 / 36 |
| Checkpoints | 196 / 196 |
| Trajectories | One continuous workspace trajectory per task |
| Agent | OpenCode 1.19.0 |
| Model | `gpt-6-luna`, medium thinking |
| Environment | Docker `ghcr.io/astral-sh/uv:python3.12-trixie-slim` |
| Evaluation behavior | `all-cases` plus concurrent evaluation, so a failing checkpoint does not stop later checkpoints |

The runner's reported `$0.00` is a local benchmark accounting value for an OAuth
model provider, not a claim that no ChatGPT plan allowance was consumed.

## Included files

- `run_summary.json` — complete run-level metrics emitted by SlopCodeBench.
- `checkpoint_metrics.jsonl` — 196 JSON Lines records, one per evaluated checkpoint.
  It is the machine-readable input for later Part A selection, candidate-regression
  collection, and analysis.
- `runner_config.resolved.yaml` and `runner_environment.resolved.yaml` — the frozen,
  runner-resolved configuration and environment used for this run.

Headline runner metrics: 7/196 checkpoints strictly solved (3.6%); 21/196 isolated
solved (10.7%); 80/196 core solved (40.8%); mean total test pass rate 0.651.
These are **trajectory-quality metrics**, not RQ1 regression-detection results.
The latter still require frozen test-suite execution on later checkpoints and
LLM/human adjudication, as specified in `docs/experiment_protocol.md`.

## Deliberately excluded raw artifacts

The full SlopCodeBench output is 211 MB and contains 196 code snapshots, evaluation
stdout/stderr, workspace state, and raw agent logs. It is kept locally under the
run output path and should be uploaded as a separately checksummed release/archive
if collaborators need snapshot-level replay. It is not committed to Git because it
is too large and unnecessarily exposes later-workspace material to ordinary
repository users.

## Integrity

SHA-256 hashes of the versioned payload at packaging time:

```
70d96154d2c79c695ec601f3f8eea99a5aeedc49c6b2ab3cbdec96214955ce5c  checkpoint_metrics.jsonl
c19d7f20fa1cf1dc78b9be47923c0f15651273c8ec4099af27c51b5b32589ab0  run_summary.json
e35ac30eca5dd6bb4b83d92960c326348490a7fa05b14f855cd62ffa553e7b05  runner_config.resolved.yaml
58d9e8ed5d1923f3304744edb960594e5e29c0fada1b0f87aeb187cb02042814  runner_environment.resolved.yaml
```

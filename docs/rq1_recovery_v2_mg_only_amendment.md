# RQ1 recovery v2: MG-only continuation

> **归档说明：** 本文保留 MG-only 范围调整的审计来源。当前操作与合并分析统一遵循[统一 Mutation-Guided 协议](rq1_mutation_guided_unified_protocol.md)，不再把各批次作为独立方法结果讨论；本页及其配置仅用于追溯已运行记录。

**Amendment ID:** `rq1_full_36x1_recovery_v2_mg_only_20261118`
**Parent budget:** [`config/rq1_recovery_v2_batch.json`](../config/rq1_recovery_v2_batch.json)
**Machine-readable amendment:** [`config/rq1_recovery_v2_mg_only_amendment.json`](../config/rq1_recovery_v2_mg_only_amendment.json)

## Scope

Continue only Mutation-Guided test recovery on the **27 tasks with zero retained MG tests in v1**. Do not launch further Vanilla or Original calls, and do not use the v2 Vanilla artifacts already created for outcomes or comparisons. Keep all earlier v1 and v2 files unchanged. The v2 `log_query` MG call was already started before this scope change; retain it as provenance but exclude it from the recovery cohort and do not run its second slot. `mocked_http` receives no new MG call because it already retained v1 MG tests.

For each of the 27 recovery tasks, MG keeps the parent budget of up to two independent calls, four candidate files per call, and eight per task. After v2 triage, 24 tasks have eligible hints and three do not. A task with no eligible C1 survivor hint receives no MG generation call. A model-attempted slot is never repeated; a local pre-request harness failure may be corrected only when its record proves zero model calls, with the failed artifact preserved. The maximum remaining generation allowance is 48 calls and 192 candidate files.

## Evaluation

Review and validate only v2 MG candidates. A test is retained only after blind specification review, three stable C1 runs against the hash-matched baseline, and three kills of its named target mutant. Build each task's MG suite from the base suite, immutable v1 retained MG tests, and retained v2 MG tests, recording a new hash.

If the recovered suites proceed to later checkpoints, run MG only across all 36 trajectories and 160 later-checkpoint pairs. Report MG-only outcomes; this amendment makes no comparison against Original or Vanilla.

# RQ1 C1 测试生成恢复批次 v2

> **归档说明：** 本文记录恢复批次形成时的方案及其变更背景。当前 MG 不再按批次分别描述；生成、验证、冻结和后续评估统一遵循[统一 Mutation-Guided 协议](rq1_mutation_guided_unified_protocol.md)。本页原有“尚未调用”等状态只描述本文编写时点，不代表当前状态。锁定配置和实际结果作为不可变审计材料保留。

**状态：** 协议已冻结，尚未调用模型。
**批次 ID：** `rq1_full_36x1_recovery_v2`
**机器可读预算：** [`config/rq1_recovery_v2_batch.json`](../config/rq1_recovery_v2_batch.json)；其 SHA-256 由同目录 lock 文件固定。
**结果根目录：** `artifacts/results/rq1_recovery_v2/`

**启动门槛：** 当前生成/cohort 脚本把 v1 ID、预算锁和输出路径写死，不能直接用于 v2。必须先实现并检查 v2 专用协调器；它的 preflight 阶段必须零模型调用，并验证预算锁和独立结果目录。

## 为什么建立 v2

v1 记录显示：Vanilla 有 74 个候选、49 个保留，10 个任务零保留；Mutation-Guided 有 32 个候选、13 个保留，27 个任务零保留。至少一种方法零保留的任务共有 29 个。v2 专门补齐这些任务方法单元的生成机会，同时在同一恢复任务上为 Vanilla 和 Guided 提供相同的新增预算。

v2 是单独的恢复批次。它不替换 v1 的生成、验证、回归评估或报告结果。因为任务集合按 v1 的零保留结果挑选，v2 单独结果只作为恢复/补充结果，不能冒充未筛选的全 cohort 确证性比较。

## 新增调用规则

- 恢复 cohort 是 v1 Vanilla 或 Guided 任一方法零保留的 29 个任务；清单在 JSON 配置中冻结。
- 在每个恢复任务上，Vanilla 和 Guided 各最多获得 2 次独立生成调用；每次最多产出 4 个测试文件，单个任务方法最多 8 个。两种方法使用 GPT-6 Luna、medium、相同的每次 4,096 输出 token 请求目标和相同候选上限。
- 两次调用使用完全相同的合法 C1 输入，第二次看不到第一次的输出、v1 候选、审查/验证结果或未来回归结果。空响应、格式无效、技术失败或候选被淘汰都不触发额外生成；技术失败会消耗该调用名额。
- Guided 仅在有经过审查的 eligible C1 mutant hints 时调用。v1 triage 无效或不可用的 9 个任务允许一次新的结构化 triage 调用；只有未取得有效结构化响应时，才允许一次技术重试。完成但空、无效或零 eligible 的响应不重试。
- `forge` 的 v1 triage 已有效但没有 eligible hint；`eve_route_planner` 没有 survivor。这两个任务不重复 triage，也不发 Guided 生成调用。其 Guided 名额记为未使用，不转给 Vanilla。
- 新候选走与 v1 相同的盲化正确性审查、C1 基线对照、三次稳定性验证；Guided 测试还须对目标 mutant 三次均失败。失败候选不修补、不重抽。

预算上限：Vanilla 生成最多 58 次；Guided 最多 54 次（`forge` 无 eligible hint、`eve_route_planner` 无 survivor，不会调用）；triage 最多 18 次；候选审查最多 56 次。合计最多 186 次模型调用，请求输出 token 目标合计 610,304。实际 usage 有记录时据实报告；请求上限不等于服务端硬性 token 限制。

## 冻结与后续评估

v2 的原始生成、triage、审查、验证和冻结 suite 均写入独立结果目录。每个 v2 composite suite 由只读的 v1 保留测试加 v2 新保留测试组成，单独记录哈希；v1 suite 不改写。

若将 v2 suite 用于后续回归评估，必须以新的 v2 run ID 单独运行全部 36 条轨迹、全部 160 个后续 checkpoint pair，并单独 adjudicate 新观察。不得把 v2 记录追加到 v1 评估文件。当前步骤只冻结了恢复批次的协议与预算，不启动生成或评估。

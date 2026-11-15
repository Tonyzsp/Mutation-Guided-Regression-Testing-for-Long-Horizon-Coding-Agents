# RQ1 实验进度报告：已完成部分

**项目：** Mutation-Guided Regression Testing for Long-Horizon Coding Agents

**报告日期：** 2026-10-19

**研究问题：** 在相同测试生成预算下，Mutation-Guided Tests 是否比 Original Tests 和 Vanilla LLM Tests 检测到更多后续 checkpoint 引入的回归？

## 报告范围

本报告只记录已完成并有产物可核验的工作。本文中的局部数据用于描述已完成的实验单元，不代表整个 36-task RQ1 cohort 的最终结论。

## 1. C1 输入数据与测试隔离

- 已整理 36 个任务的 C1 checkpoint package，包含当前 checkpoint 的源码、规格和可见官方 base tests。
- 已完成全 cohort 的 C1 base-suite 审计，并保存测试运行状态及输入摘要，供生成测试验证和后续执行复用。
- 生成器输入按任务隔离：合法可见输入为当前 C1 workspace 与 base tests；hidden、future 和 excluded tests 不属于生成器可见输入。
- 审计记录：`artifacts/results/rq1_full_36x1/c1_base_audit_isolation_revalidated_20261019/c1_base_suite_audit.json`。

## 2. Vanilla 测试生成与冻结

- 已完成并冻结 **35 个任务**的正式 Vanilla C1 测试套件。
- 这些套件使用统一的 formal v4 prompt 和共享预算配置；生成输入、模型调用记录、候选验证结果、冻结测试文件及其哈希均保存在对应任务目录中。
- Frozen suite 保留原始 base tests，并仅加入经验证的生成测试；生成测试需通过收集、正确性审查、重复执行稳定性等验证步骤。
- 结果目录：`artifacts/results/rq1_full_36x1/<task>/C1/vanilla_formal_v4_20261019/`。

## 3. Mutation 结果

已完成以下 4 个任务的直接 mutant 分类包。分类基于实际应用变异后的源码并执行相应 C1 tests。

| 任务 | Mutant 总数 | Killed | Survived |
|---|---:|---:|---:|
| cfgpipe | 233 | 172 | 61 |
| code_search | 303 | 244 | 59 |
| eve_industry | 480 | 376 | 104 |
| xjq | 94 | 60 | 34 |
| **合计** | **1,110** | **852** | **258** |

这 4 个任务合计的 mutation score 为 **76.8%**（852/1,110）。该数值仅描述表中已完成的任务，不能解释为整个 cohort 的 mutation score，也不能直接回答 RQ1。

各任务产物位于：`artifacts/results/rq1_full_36x1/<task>/C1/mutmut_direct_revalidated_20261019/`。

## 4. Mutation-Guided 测试生成与冻结

- 已完成 **3 个任务**的 Mutation-Guided 生成与验证流程，并保存冻结套件：cfgpipe、code_search、eve_industry。
- 这 3 个任务共输出 2 个 Guided 候选测试，均未通过保留标准；另外两个任务没有输出候选测试。因此，已完成的这 3 个任务中，当前没有可加入冻结套件的新增 Guided 测试。
- 该结果保留为生成流程的实际输出，不以空套件或未保留候选声称提升了测试能力。
- 结果目录：`artifacts/results/rq1_full_36x1/<task>/C1/guided_iso_v3_20261019/`。

## 5. 已形成的可复核产物

- 全 cohort C1 base-suite 审计记录。
- 35 个任务的正式 Vanilla 冻结套件及生成、验证元数据。
- 4 个任务的直接 mutant 分类包与逐 mutant 状态。
- 3 个任务的 Mutation-Guided 生成记录、候选验证结果和冻结套件。

## 结论边界

上述结果是已完成实验单元的进度记录。它们不构成全 cohort 的后续 checkpoint 回归检测比较，也不支持对 RQ1 作出统计结论。

# RQ1 正式实验协议与预算 v1

> **归档说明：** 本文记录原始全三方法预算与研究设计，不再作为当前 MG 生成、验证和结果汇总的独立操作协议。MG 当前统一规则以[统一 Mutation-Guided 协议](rq1_mutation_guided_unified_protocol.md)为准；本文件和锁定配置仅保留为已运行批次的来源证据，不作追溯修改。

**适用范围：** 从当前 36-task C1 Mutmut 结果继续，完成 Vanilla/Mutation-Guided 测试生成、后续 checkpoint 回归运行、LLM adjudication 和 RQ1 报告。RQ2、RQ3 不在本协议范围内。

**预算配置：** [`config/rq1_full_study_budget_v1.json`](../config/rq1_full_study_budget_v1.json)。该文件固定实验预算与判定规则。正式生成前先落实预算哈希、hint/call/candidate cap、候选审查与 C1 重复验证；后续 checkpoint 评估另有完整的三方法 suite 完整性 gate 和基础设施重试限制，未通过前不读取后续 checkpoint 结果。

## 1. 研究设计和数据边界

- 实验单位为 36 条任务轨迹，每条从 C1 开始；数据包共 196 个 checkpoint，C1 后共有 160 个可评估的 task/checkpoint pair。
- 比较三种冻结套件：Original = 完整合法 C1 `base_tests`；Vanilla = `base_tests` + 合格的 Vanilla tests；Mutation-Guided = `base_tests` + 合格的 Guided tests。
- 生成器只读当前 C1 的规范、源码和完整合法 `base_tests`。Vanilla 不接收 mutant 信息；Guided 仅额外接收经过本协议筛选的 C1 survivor-family hints。任何方法均不能看到 hidden、future、excluded tests、reference solution 或后续失败结果。
- 当前全 cohort 有 8,490 个 direct survivors 和 1,047 个 inconclusive mutants。未决项保留但不作为 survivor hint。mutant triage 是预算受限的提示选择，不宣称逐个证明 8,490 项的语义等价性。

## 2. Survivor hint 预算

为避免把数千个 diff 无上限地塞进提示，先按任务、文件、函数、mutmut operator 和规范化 AST edit 归并 family；每个 family 取 mutant ID 字典序最小者作代表。当前导出的 8,490 条记录中 `operator` 全部为空，因此分层时从 diff 的 old/new AST 推导保守的 edit-kind，并在记录中标明这是推导值，不冒充 mutmut 原生 operator。代表按 `(source_file, operator_or_inferred_ast_kind)` 分层并轮转选择，每个任务最多选 16 个。规则在接触任何后续 checkpoint 结果前固定；未选项记为 `deferred_by_preregistered_hint_budget`，不记作等价或无用 mutant。

每任务最多一次 GPT-6 Luna medium triage 调用，最多一次仅限“请求前失败且未取得结构化响应”的技术重试；全 cohort 最多 72 次 triage 调用，请求输出 token 上限合计 147,456。LLM 依据 C1 规范把选中代表标为 `eligible`、`deferred`、`suspected_equivalent` 或 `out_of_scope`，并给出简短规范依据。Guided 每任务最多接收 16 个 eligible hints。triage 的调用量和 token 用量单独报告，不占 Vanilla/Guided 的 test-generation call cap。

## 3. Vanilla 与 Guided 配对生成预算

正式生成使用现有 [36-task cohort 配置](../config/generation_budget_rq1_c1_cohort_formal.json)：OpenAI Codex GPT-6 Luna、medium reasoning、服务默认 decoding。生成评估包含在生成调用内，不另开模型调用。

| 每个任务、每种生成方法 | 预算 |
| --- | ---: |
| 已启动模型调用 | 最多 3 次 |
| candidate test 文件 | 最多 8 个；每个文件恰好一个顶层 pytest test function |
| 输出 token 请求 | 每次 4,096；每个方法/任务合计请求上限 12,288 |
| 修补已完成回答 | 0 次 |
| 失败重试 | 只允许进程在结构化响应前失败时重试，最多 2 次；有效空响应或已捕获的无效回答不重抽 |

全 cohort 上限为 216 次 Vanilla/Guided generation 调用、576 个候选文件。4,096 是 Codex CLI 的请求目标，不是服务端硬限制；实际输入/输出 token 使用情况可取得时必须记录，缺失时记为 unavailable。Vanilla 与 Guided 的绑定公平预算是同模型、每方法/任务 3-call cap、8-file cap 和相同输出 token 请求；Guided 增加的 triage 成本和实际输入 token 必须另行披露，不能宣称两种方法总 token 花费相同。

因此，回归判定前固定上限合计为 360 次 LLM 调用（triage 72 + generation 216 + correctness review 72），输出 token 请求目标合计最多 1,179,648；回归 judge 另外按 `2 × N` 次调用和 `4,096 × N` 输出 token 目标计。它们是预算目标与请求上限，不保证与服务端实际计费 token 完全相等。

当前预算文件 SHA-256：`b1ff7c44533e6ea813edad74d6b49d3365168985678acddbd7f0b70d206ae1f3`。每个生成 run manifest 必须记录此哈希及 prompt、模型、CLI 版本、输入哈希、调用和候选计数。

## 4. Candidate 正确性与执行预算

每个方法/任务最多一次 method-blind LLM correctness-review batch，最多审查 8 个候选；全 cohort 最多 72 次 review 调用，请求输出 token 上限合计 147,456。仅 `correct` 可保留；`incorrect`、`uncertain`、响应失败或格式无效都不保留，不安排人工复核。审查看候选代码、C1 规范和合法 `base_tests`，看不到方法标签或 mutant hint。该 LLM review 是固定的自动化审查步骤，不是独立正确性 oracle；报告需披露这一限制。

| 检查项 | 规则 | 全 cohort 理论上限 |
| --- | --- | ---: |
| 候选收集 | 每个候选收集一次；必须是安全路径且仅一个稳定 pytest test ID | 576 次 |
| C1 正确实现 | base suite + 每个候选分别重复 3 次；候选自身通过，且相对哈希匹配的 baseline audit 不新增稳定失败 | 1,728 次候选运行 |
| Guided target mutant | 每个保留的 Guided candidate 在干净 mutant 副本上重复 3 次，三次均为 assertion failure | 864 次 |
| 冻结组合套件 | Original、Vanilla、Guided 各在 C1 完整运行 3 次；可复用哈希一致的 Original baseline audit | 最多 324 次，其中 Original 现有 108 次可复用 |

所有 source-checkpoint 运行单次 timeout 为 900 秒。基线中已有的稳定失败单独记录，不要求整套既有 base suite 全绿；base suite 与新增候选一起运行时，候选自身必须三次稳定通过，且不能造成相对 baseline 的新失败。pytest collection/setup 错误、超时和非稳定结果不计作 mutant kill 或合格测试；保留失败日志和原因。冻结前对已保留候选组成的整套 suite 再重复 3 次，以发现候选之间的相互影响。

## 5. 后续 checkpoint 回归评估预算

只有 36 个任务的三种 C1 套件全部冻结并通过完整性 gate 后，才读取后续 checkpoint。每个 C1 冻结 suite 在其轨迹的每个后续 checkpoint 原样运行一次，不根据未来代码修改断言。160 个 target pair × 3 种方法，最多 480 次初始完整 suite 运行；哈希完全相同的 suite 可共用一次执行并映射回对应方法。

- 单次 suite timeout：900 秒；最多 480 次初始 suite 运行，基础设施失败最多额外重试一次，因此重试上限为 480 次。
- 只有日志明确表明环境/runner 基础设施失败时允许一次技术重试；应用 assertion failure 不重跑整套 suite。
- 候选 regression 必须是该 test ID 在 C1 稳定通过、在目标 checkpoint 的完整运行中失败。每个候选 failure 随后对该 test ID 做 3 次 targeted reproduction；恰好 3/3 相同应用失败才是 `stable_failure`。
- setup error、timeout、缺失 test ID、环境失败或不稳定重现均标为 infrastructure/inconclusive，不确认成回归。
- 失败复现的运行预算随观察数变化：若共有 `O` 个不同的 eligible test/checkpoint failure observations，targeted reruns 上限为 `3 × O`。不设 outcome-dependent 的候选事件截断，确保所有已观察的合格失败均可复核。

## 6. LLM 回归判定预算

全部稳定 candidate observations 冻结后再做 adjudication。使用固定 GPT-6 Luna medium；一个 case 一次调用，若没有有效结构化响应最多再尝试一次，每次请求输出 token 目标 2,048，单次 timeout 900 秒。潜在调用上限为 `2 × N`，请求输出 token 上限为 `4,096 × N`，其中 `N` 是开启 judge 前冻结的稳定候选 case 数。

Judge 对方法盲化，输入仅包括 C1 与目标 checkpoint 规范、checkpoint/hash 证据、冻结测试、代码 diff、失败 trace 和三次复现摘要。`confidence < 0.80`、证据冲突、响应无效或 judge 标记不确定均为 `inconclusive`；不依赖人工复核。判定为 expected spec change、preexisting defect、invalid test 或 environment failure 的事件不计为 confirmed regression。

同一任务、同一外部行为、同一引入代码变化，即使被多条测试、多个方法或多个后续 checkpoint 发现，也只计一个 event；把它锚定到最早失败的目标 checkpoint。保存方法映射在私有表中，judge 不接触映射。

## 7. RQ1 统计和报告

主结果为每种方法检测到的 unique LLM-confirmed regression event 数；主对比预先固定为 Mutation-Guided vs Original、Mutation-Guided vs Vanilla。以 36 条任务轨迹为配对单位，报告每任务差值、10,000 次 task-cluster bootstrap 的 95% CI，以及两项配对置换检验并对两个主对比做 Holm 校正。

没有独立的全量真实回归 oracle，因此分母是“至少由一种冻结 suite 暴露、且经 LLM 确认”的去重事件并集。不得把结果称为所有真实回归的绝对数量或绝对 Regression Detection Rate。报告原始失败观察、confirmed/其他/不确定判定、方法间共享和独有事件、每任务数据、生成与 triage 调用、token 用量及执行成本。

## 8. 当前阶段和启动 gate

- 已完成：36/36 C1 baseline、coverage 和 Mutmut 直接分类；22,337 个 mutant 中 12,800 killed、8,490 survived、1,047 inconclusive。
- 已完成 survivor hint 准备：8,490 条记录归并为 8,283 个 family，确定性选出 548 个代表；37 次 triage 调用后，26 个任务得到有效结构化结果（227 eligible、115 suspected equivalent、29 out of scope），9 个任务的所选代表按预算协议 deferred，`eve_route_planner` 没有 survivor。36/36 triage 文件通过正式 Guided loader 的哈希、ID、hint cap 和 decision 完整性检查。
- 生成器现在执行预算锁与配对预算检查。预算文件保持冻结，SHA-256 为 `7ca6f4be4a59b895feeb40eb6c1ad85b2dee61e199b9e69816ba0e0a6eeb3063`；预算配置中的 status 字段描述的是冻结时 runner enforcement 尚待实现，当前 enforcement 已实现并经过 cohort loader 检查。
- Vanilla：旧 formal-v4 记录中仅 35/36 个任务的 C1 输入哈希匹配；`execution_server/C1` 旧记录不复用。历史聚合的 76 candidates/48 retained 不视为当前正式 cohort 总数。
- Mutation-Guided：正式生成尚未开始，尚未形成完整 36-task 冻结套件。无 eligible hint 的任务仍须有明确空/无 hint 的生成记录。
- 评估尚未开始。

开始冻结 C1 套件前必须完成：Vanilla 36 个任务的正式预算审计并补做哈希不匹配的 `execution_server/C1`；随后按配对预算完成 Guided 生成、候选审查和稳定性验证。启动后续 checkpoint 评估前，再检查 Original/Vanilla/Guided 的 C1 package/base hash、suite hash、模型和预算 hash、3 次稳定性记录及输入隔离。完成该 gate 后，才执行第 5 节评估。

# RQ1 研究实施与双人协作计划

**项目：** Mutation-Guided Regression Testing for Long-Horizon Coding Agents  
**目标范围：** 建立可复现的研究流水线，取得 RQ1 的首批真实实验结果；暂不展开 RQ2 的覆盖控制统计和 RQ3 的 operator/domain 深入分析。  
**RQ1：** 在相同测试生成预算下，Mutation-Guided Tests 能否比 Original Tests 和 Vanilla LLM Tests 检测更多后续 checkpoint 引入的回归？

## 1. 分工与总体流程

- **A — Benchmark & Regression Evaluation：** 负责 SlopCodeBench 任务、coding-agent 轨迹、checkpoint 快照、oracle/回归事件识别、统一测试执行与 RQ1 指标。
- **B — Mutation-Guided Test Generation：** 负责 mutmut 与 coverage、mutant 筛选、Vanilla 和 Mutation-Guided 测试生成、生成测试验证及结果记录。
- **共同负责：** 数据接口、研究协议、预算公平性、集成调试和 RQ1 初步结果分析。

总体流程：

```text
A: SlopCodeBench → Coding Agent → Frozen C1/C2/C3/... → Regression Oracle
                                 │
                                 └── checkpoint package ──→ B

B: Checkpoint → Original Tests + Coverage → Mutmut → Surviving Mutants
                                                  ├── Vanilla LLM Tests
                                                  └── Mutation-Guided Tests
                                                            │
                                               Validated, Frozen Test Suites
                                                            │
                                                            ▼
A+B: 统一在后续 checkpoints 执行 → 核查 confirmed regressions
     → 比较 Original / Vanilla / Mutation-Guided 的 RQ1 检测结果
```

## 2. 分阶段任务

### Phase 1 — 确立任务与可执行实验协议

**A 的任务**
1. 安装并运行 SlopCodeBench，检查 Python 任务、checkpoint specification、workspace、测试入口及依赖。
2. 筛选一批候选任务：要求连续 checkpoint 可运行，旧功能具有可检查的正确性条件，并能够保存代码版本。
3. 检查官方测试、reference implementation、hidden test 的可访问性；确认是否能够实现 **visible/held-out 隔离**，不能假定 hidden tests 可以给 LLM 使用。
4. 明确 `C_i → C_j` 的回归事件判定规则：某旧行为在较早版本通过独立 oracle，后来在仍然适用的需求下失败；区分持续缺陷、规格变化与真实回归。
5. 确定 agent、模型/版本、环境和 checkpoint 保存方式。

**B 的任务**
1. 建立 mutmut、pytest、coverage.py 环境；验证它们在一个隔离 Python fixture 上能正常运行。
2. 定义有效 mutant 分类：killed、survived、invalid、unreachable、suspected equivalent 等；记录对边界/等价情况的人工审查。
3. 定义 Vanilla 与 Mutation-Guided 的输入：两者共同使用当前 `C_i` 的规范、代码与允许公开的测试信息；Guided 额外读取 surviving mutant diff。
4. 定义生成测试的验证标准：能收集/执行、符合当前规范、通过可信正确实现或可用的独立正确性检查、重复执行稳定；Guided 还应杀死目标 mutant。
5. 设计模型调用、token 数、候选测试数和执行成本的记录格式。

**共同确认（Gate 1）**
- 确定候选任务集、oracle 隔离办法、回归事件定义及三种方法的公平比较口径。
- 固定共享数据格式与接口；若官方测试/参考实现不能满足提案假设，先修订实验协议并记录偏差，再进行正式实验。

**阶段交付：** `experiment_protocol.md`、任务清单、环境说明、共享 JSON schema。

### Phase 2 — 两条流水线并行实现

**A 的任务：Trajectory & Evaluation Infrastructure**
1. 运行一个固定配置的 coding agent，使它在同一个 workspace 中依次执行 `C1 → C2 → C3 → ...`，不得每个 checkpoint 重新从零实现。
2. 保存每个 checkpoint 的代码快照、commit/hash、specification、agent 日志、执行环境和初始测试结果。
3. 实现可复用的测试运行器：给定 workspace 和 test suite，返回通过/失败/错误/超时、日志与测试标识。
4. 实现独立 oracle 的调用及回归事件提取模块；事件记录包含旧功能标识、先前通过的版本、后续失败版本以及核查证据。
5. 输出供 B 使用的 checkpoint package，不暴露 held-out 或未来版本信息。

**B 的任务：Mutation & Generation Infrastructure**
1. 实现针对给定 checkpoint 的 coverage 与 mutmut 执行器，并确认实际被执行的是变异后的源码（注意 subprocess/CLI 场景）。
2. 提取 surviving mutants 的 ID、operator、文件位置、变异 diff 和运行状态；过滤明显 invalid/unreachable 项并标记等价性不确定项。
3. 实现 `Vanilla LLM` 生成器：基于当前规范与源码产生 pytest tests。
4. 实现 `Mutation-Guided LLM` 生成器：同样输入外加 surviving mutant diff，生成试图杀死 mutant 的规范一致性测试。
5. 实现统一 validator：测试收集、正确实现/可信检查、目标 mutant（仅 Guided）、稳定性；冻结合格测试并保留失败原因。
6. 保存每次生成的 prompt、模型配置、token 消耗、候选及保留测试数、测试文件与执行日志。

**共同确认（Gate 2）**
- A 提供真实的 `C_i` checkpoint package；B 能不修改内部逻辑地读取它，产出规范化的生成测试包。
- A 的测试执行器能执行 B 的测试；B 不接触 held-out tests 或后续 checkpoint。

**阶段交付：** 冻结轨迹、运行器、mutant 数据、两种生成器、validator，以及自动化日志。

### Phase 3 — 集成并执行 RQ1 正式对比

**A 的任务**
1. 在选定任务的合格 checkpoint 上建立独立 oracle 记录，确认后续是否存在 regression events。
2. 对每组 **已经冻结的 C_i 测试**，在后续适用的 `C_{i+1}, C_{i+2}, ...` 快照上执行，不根据未来代码修改断言。
3. 将失败测试映射到已确认回归事件；同一回归被多个测试命中只计一次，排除环境问题、flaky tests 和规格变化。
4. 汇总每个任务/轨迹/起始 checkpoint 的回归事件数与各方法检测事件数。

**B 的任务**
1. 对每个合格的 `C_i`，固定统一的模型、提示基础信息及生成预算，生成 Vanilla 与 Mutation-Guided 两组候选测试。
2. 依据统一验证器筛选并冻结测试；Original 为同一 `C_i` 的 visible suite，Vanilla/Guided 均为 Original 加新增合格测试。
3. 检查预算与数据泄漏：限制最大生成测试数量、token 预算并记录实际调用开销；Guided 额外 mutation execution cost 单独记账。
4. 记录生成成功率、有效新增测试数、mutation score 和 coverage 等辅助指标，供结果解释，但本阶段以 RQ1 为主。

**共同进行的实验步骤**
1. **固定轨迹：** 所有测试方法使用同一条 agent trajectory，确保未来代码修改完全相同。
2. **固定数据可见性：** 生成测试只看起始 `C_i` 的允许信息；held-out oracle 不参与生成与调参。
3. **公平生成：** Vanilla 与 Guided 使用同一模型和预先确定的可比较生成预算，记录实际耗费。
4. **统一评估：** 在同一批后续 checkpoint 运行三组测试，依据相同的 confirmed regression events 计数。
5. **扩大样本：** 从已跑通的任务扩展到多个任务/轨迹；不要仅根据一个示例下结论。

**阶段交付（Gate 3）：** 每个实验单元有三套冻结测试、可追溯的回归事件、逐事件检测结果和完整执行日志。

## 3. 集成接口与数据约定

A 向 B 提供 **checkpoint package**（示例）：

```json
{
  "task_id": "task_001",
  "trajectory_id": "run_001",
  "checkpoint_id": "C1",
  "workspace_path": "data/checkpoints/task_001/run_001/C1",
  "specification_path": "data/specifications/task_001/C1.md",
  "visible_tests_path": "data/visible_tests/task_001/C1",
  "test_command": "python -m pytest ...",
  "runtime_environment_id": "env_001"
}
```

B 向 A 提供 **frozen test-suite package**（示例）：

```json
{
  "task_id": "task_001",
  "trajectory_id": "run_001",
  "source_checkpoint": "C1",
  "method": "mutation_guided",
  "suite_path": "data/generated_tests/task_001/run_001/C1/guided",
  "validation_log_path": "results/validation/task_001_C1_guided.json",
  "model_config_id": "model_001",
  "budget_id": "budget_001"
}
```

A 还需维护不提供给 B 生成器的 **oracle / regression events** 记录。可以将 oracle 运行和方法评估放在独立目录或隔离的执行流程中，避免训练/测试信息泄漏。

## 4. RQ1 数据产出与结果判读

主要指标：

\[
\mathrm{Regression\ Detection\ Rate}=\frac{\#\text{被该测试套件检测到的已确认回归事件}}{\#\text{已确认回归事件}}
\]

- **分析单位：** 至少记录 task、trajectory、source checkpoint 和 target checkpoint；一个回归事件以稳定的 event ID 去重。
- **无回归的样本：** 分母为 0 时标记 `N/A`，不能写成 0%；同时报告有多少任务/轨迹没有可确认事件。
- **对比：** 在同一批事件上分别计算 Original、Vanilla、Mutation-Guided 的检测数和 RDR，并报告测试数量、可用性与生成成本。
- **结论边界：** 最初几条轨迹得到的是 preliminary results；扩展到足够多的任务和独立轨迹后再讨论效果是否稳定。Mutation Score 提升但 RDR 不提升，同样是有效研究发现。

建议输出表：

| Task / Trajectory | Confirmed Regression Events | Original Detected | Vanilla Detected | Guided Detected | Original RDR | Vanilla RDR | Guided RDR |
|---|---:|---:|---:|---:|---:|---:|---:|
| 待实验 | 待实验 | 待实验 | 待实验 | 待实验 | 待实验 | 待实验 | 待实验 |

## 5. 达到 RQ1 进度目标的完成标准

以下条件全部满足，即可认为 RQ1 已进入有意义的正式实验阶段，并可形成期中报告的实质内容：

- [ ] 已选择并记录若干个合格的 Python 任务，具备可复现的 agent checkpoint 轨迹。
- [ ] 三组方法具有统一的数据访问、测试生成预算及执行环境。
- [ ] Mutation-Guided 能从真实 surviving mutants 生成并验证合格测试；Vanilla baseline 同样可运行。
- [ ] 所有生成测试在后续 checkpoints 上执行，测试与 agent 轨迹均已冻结。
- [ ] 通过独立 oracle 确认回归事件，并明确处理旧缺陷、规格变化和重复检测。
- [ ] 输出真实 RQ1 对比结果表、原始日志与至少一个可核查的测试/回归案例。
- [ ] 记录当前瓶颈、可用样本规模与下一步扩展计划。

> **执行重点：** A 和 B 从 Phase 1 起并行开发，在 Gate 1/2/3 集成。研究优先级依次为 **可信回归定义 → 可复现轨迹与测试生成 → 严格公平对比 → 扩大 RQ1 样本**，不要为了追求正面结果而调整 oracle 或事后改变实验预算。

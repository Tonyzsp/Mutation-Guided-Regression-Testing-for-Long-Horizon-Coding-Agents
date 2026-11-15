# Mutation-Guided Failure Analysis：已完成的 3 个任务

**日期：** 2026-10-19

**分析范围：** cfgpipe、code_search、eve_industry 的 C1 Guided generation 记录

**目的：** 沿 mutant 选择、LLM 生成、候选验证的步骤，定位已完成 Guided 单元的流失点。本文不把未执行的验证步骤记作测试失败。

## 1. 流程漏斗汇总

| 阶段 | 数量 | 说明 |
| --- | ---: | --- |
| 可用 surviving mutants | 224 | cfgpipe 61、code_search 59、eve_industry 104 |
| 选入 Guided 输入的 mutants | 224 | 三个任务的 `guided_mutant_ids` 数均等于 eligible survivor 数 |
| LLM generation 请求 | 3 次 | 每个任务 1 次；三次均已发送并成功返回 |
| 产生 candidate | 2 | 两个都来自 cfgpipe；另两个任务未产出 candidate |
| 通过规格正确性审查 | 0/2 | 两个候选都被本地人工审查标记为 `uncertain`，验证器据此提前拒绝 |
| pytest 收集 / 可执行性 | 未执行 | 0 个 collection 记录；两个候选均未进入 pytest 收集，不能判为不可执行 |
| 通过原始实现 | 未执行 | 没有 baseline test run 记录，不能判为通过或失败 |
| 杀死目标 mutant | 未执行 | 没有 targeted-mutant run 记录，不能判为杀死或未杀死 |
| 重复执行稳定 | 未执行 | 没有重复运行记录，不能判为稳定或不稳定 |
| 最终保留 | 0 | 当前三个冻结 Guided suite 中没有新增生成测试 |

这里的“0/2”表示候选被规格审查拦截，不表示它们在原始实现上失败、不能收集或无法杀 mutant。验证记录中的 `collection_ids`、`runs` 和 `target_mutant_results` 均为空。

## 2. 两个已生成候选的人工失败分析

### Candidate 1：重复 JSON 参数名

- 文件：`cfgpipe/C1/guided_iso_v3_20261019/candidates/candidate_01/test_duplicate_parameter_names.py`
- 目标 mutant：`cfgpipe.x_load_schema__mutmut_13`，把 JSON 解析的 `object_pairs_hook=reject_duplicate_keys` 改为 `None`。
- 测试意图：提交含重复 `port` key 的 JSON，要求程序非零退出且不输出 JSON。
- 淘汰阶段：规格正确性审查，标记 `uncertain`。
- 原因分类：**规格对重复 JSON key 的处理没有明确到足以支持该断言。** 规格要求参数名唯一，但 JSON 文本中的重复对象 key 在解析时如何处理没有被明确规定。候选假设 parser 应保留并拒绝重复 key，现有合同证据不足以判定该期望必须成立。
- 执行结论：没有进入收集、原始实现执行、目标 mutant 执行或稳定性检查；这些结果均未知。

### Candidate 2：`yes` / `no` 布尔值

- 文件：`cfgpipe/C1/guided_iso_v3_20261019/candidates/candidate_02/test_common_boolean_representations.py`
- 目标 mutants：`cfgpipe.x_parse_value__mutmut_51`、`52`、`61`、`62`，分别改变 `yes` / `no` 接受列表。
- 测试意图：要求 `yes` 解析为 `true`，`no` 解析为 `false`。
- 淘汰阶段：规格正确性审查，标记 `uncertain`。
- 原因分类：**规格只说接受“常见布尔字符串表示”，没有在可见合同中穷举允许值。** `yes` / `no` 看起来合理，但仅凭“common”不能确认所有合规实现都必须接受这两个值；若把它们写死，可能拒绝另一种符合规格的实现。
- 执行结论：同样没有进入 pytest 或 mutant 执行；不得据此称候选不可执行或不能杀 mutant。

## 3. 没有生成 candidate 的 survivor 抽查

下列是从两个空响应任务的已选 survivor 中抽查的代表案例。归因区分了“模型漏掉了明确行为”和“规格或输入证据不足”，不把所有空响应都归为同一原因。

| 任务 / mutant | 变异内容 | 可见证据与人工分类 |
| --- | --- | --- |
| `code_search.x_main__mutmut_67` | 遇到不能解码的文件时，将 `continue` 改为 `break` | 规格两处明确要求跳过不能解码的文件。现有 `test_skip_undecodable_file` 先处理 `ok.py`、再处理 `bad.py`，所以 `break` 不会漏掉后续有效文件。**分类：模型漏掉了一个有明确规格依据、可通过调整文件顺序测试的行为；现有 base test 覆盖了“跳过坏文件”，但未覆盖坏文件之后仍继续扫描。** 可把坏文件放在按目录排序会先遍历的 `a_bad/`、有效文件放在 `z_good/`，构造稳定区分 `continue` 与 `break` 的诊断用例；本次未执行该诊断用例，也未加入冻结套件。 |
| `code_search.x_search_file__mutmut_46` | 精确匹配扫描位置从 `start += 1` 改为 `start += 2` | 该变异可能影响重叠匹配，例如在 `aaa` 中查找 `aa`。可见规格没有明确是否必须报告重叠匹配。**分类：匹配语义未规定充分；不生成断言有合理的规格依据。** |
| `code_search.x_load_rules__mutmut_3` | 规则文件读取从显式 `encoding="utf-8"` 改成系统默认编码 | 本实验容器默认 UTF-8，且规格要求规则文件为有效 UTF-8；在当前运行环境和合法输入范围内，这个变异可能与原实现不可区分。**分类：当前环境下可观察性弱 / 环境等价风险，而不是已经证明 mutant 等价。** |
| `eve_industry.x_main__mutmut_292` | `float(item["volume"] or 0)` 改为 `float(item["volume"] or 1)` | 规格要求非船只直接使用 `invTypes.volume`；该 mutant 只在 volume 缺失或为零时产生差异。已提供的合法案例没有这类输入。**分类：现有 fixture 缺少触发边界；不是行为本身无法从 CLI 测试。** 需要有依据的零值/缺失数据 fixture 才能写出稳定预期。 |

## 4. 失败类型计数

按上述实际证据分类：

- **候选被规格审查拦截：2**。重复 JSON key 与 `yes` / `no` 的断言都存在合同不确定性。
- **无 candidate 任务：2**。两次模型请求都成功返回空测试列表；不是调用超时或响应格式错误。
- **可见的模型漏测机会：至少 1**。`code_search` 的“跳过解码失败文件后继续扫描”是明确规格行为，现有测试未覆盖坏文件排在有效文件之前的情况。
- **规格定义不足导致不宜强写断言：至少 1**。重叠匹配是否计入结果没有明确规定。
- **环境可观察性或现有 fixture 限制：至少 2**。规则文件默认编码变异在当前 UTF-8 环境下难区分；EVE 的 volume fallback 变异缺少零值/缺失值 fixture。
- **执行 / fixture 故障：0 个已观测候选失败**。两个候选都未运行，因此当前证据不能把淘汰归因于执行或 fixture 故障。

这些类别用于对具体代表案例作定性归因，类别之间可能重叠；“至少”表示只对本次抽查样例计数，不代表对 224 个 mutant 做了完整人工分类。

## 5. 可复核记录

三个任务的原始 generation、候选、审核和验证记录位于：

- `artifacts/results/rq1_full_36x1/cfgpipe/C1/guided_iso_v3_20261019/`
- `artifacts/results/rq1_full_36x1/code_search/C1/guided_iso_v3_20261019/`
- `artifacts/results/rq1_full_36x1/eve_industry/C1/guided_iso_v3_20261019/`

关键文件包括 `generation_run.json`、`generation_attempts.jsonl`、`eligible_mutants.json`、`candidate_response.json`、`validation.jsonl` 和 `correctness_review.local.json`。本分析没有修改冻结套件，也没有声称未运行的候选验证结果。

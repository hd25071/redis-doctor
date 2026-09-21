# 评测

## 设计

16 个场景 × 4 个组 × 3 次重复 = 192 次运行，另有预算收紧实验。

```bash
python rdctl.py eval --runs 3
python rdctl.py eval --runs 1 --variants D --budget-pressure
python rdctl.py eval --runs 1 --scenarios S02,S08 --variants B,D
python rdctl.py eval --runs 3 --backend real
```

每次运行前重置到干净状态，依次注入、采集、判分、恢复，并断言恢复后状态干净。
结果记录策略名、模型名、prompt 版本、后端、预算与耗时，写入 `eval/results/`。

## 指标

| 指标 | 定义 |
|---|---|
| 根因 Top-1 / Top-3 | 结论类别等于 / 命中标准类别（结论 + 按置信度排序的假设前 3），类别匹配而非字符串匹配 |
| 证据召回（已观察） | 标准信号被工具观察到的比例 |
| 证据召回（已引用） | 标准信号被结论引用的比例 |
| 幻觉率 | 结论中无法对应到真实工具调用与信号的证据占比 |
| 平均步数 / 工具调用 / 耗时 / token | 每次诊断的均值 |
| 越权次数 | 白名单外调用 + 未审批写操作 + 工具级策略拒绝，验收要求为 0 |
| 恢复后干净 | 沙箱是否回到无故障状态 |
| grounded Top-1 | 结论正确且必需信号全部被引用 |
| 结论无证据占比 | 结论中没有任何证据引用的运行占比 |
| Top-1 的 95% 区间 | Wilson 区间；3 次重复下区间很宽，不应只引用单点百分比 |

## 沙箱结果

`RD_LLM_PROVIDER=reference`（确定性参考策略，**参考策略基线**），16 场景 × 3 次重复
（确定性策略下重复结果一致，有效样本为每组 16 个场景）：

| 组 | Top-1 | Top-3 | 证据召回(已观察) | 证据召回(已引用) | 幻觉率 | 平均步数 | 平均工具调用 | 越权 |
|---|---|---|---|---|---|---|---|---|
| A | 43.8% | 87.5% | 0.0% | 0.0% | 0.0% | 3.0 | 0.0 | 0 |
| B | 81.2% | 100.0% | 81.2% | 75.0% | 0.0% | 5.0 | 6.6 | 0 |
| C | 93.8% | 93.8% | 87.5% | 78.1% | 0.0% | 5.0 | 7.6 | 0 |
| D | 100.0% | 100.0% | 100.0% | 90.6% | 0.0% | 7.4 | 10.4 | 0 |

手册覆盖类别与 held-out 类别分别统计：

| 组 | 手册覆盖 | held-out |
|---|---|---|
| A | 41.7% | 50.0% |
| B | 83.3% | 75.0% |
| C | 100.0% | 75.0% |
| D | 100.0% | 100.0% |

预算收紧至 3 次工具调用：

| 配置 | Top-1 | Top-3 | 证据召回(已观察) | 平均工具调用 |
|---|---|---|---|---|
| D | 100.0% | 100.0% | 100.0% | 10.4 |
| D，预算 3 | 81.2% | 87.5% | 43.8% | 3.0 |

原始数据：[reference-3x.json](../eval/results/reference-3x.json)、
[budget-pressure.json](../eval/results/budget-pressure.json)、
[RESULTS.md](../eval/results/RESULTS.md)。

## 真实模型

```bash
export RD_LLM_PROVIDER=openai_compat
export RD_LLM_BASE_URL=https://api.deepseek.com/v1
export RD_LLM_API_KEY=... RD_LLM_MODEL=deepseek-chat RD_LLM_TEMPERATURE=0
python rdctl.py eval --runs 3 --out eval/results/llm-3x.json
```

真实模型的调用结果以同一套指标记录在 `eval/results/` 下；CI 使用 `reference`
或录制回放（`eval/cassettes/`），不调用外部模型。

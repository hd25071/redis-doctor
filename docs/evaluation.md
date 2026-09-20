# 评测

## 设计

16 个场景 × 4 个组 × 3 次重复 = 192 次运行，外加一组预算收紧实验。选项与场景范围：

```bash
python rdctl.py eval --runs 3                                  # A/B/C/D 全量
python rdctl.py eval --runs 1 --variants D --budget-pressure    # max_tool_calls=3
python rdctl.py eval --runs 1 --scenarios S02,S08 --variants B,D
python rdctl.py eval --runs 3 --backend real --policy openai_compat
```

每次运行前重置到干净状态，注入、采集、判分、恢复，并断言恢复后干净，避免上一次故障影响下一次。
结果记录策略名、模型名、prompt 版本、后端、预算、场景清单与耗时，写入 `eval/results/`。

## 指标

| 指标 | 定义 |
|---|---|
| 根因 Top-1 / Top-3 | 结论类别等于 / 命中标准类别（结论 + 按置信度排序的假设前 3），类别匹配而非字符串匹配 |
| 证据召回（已观察） | 标准信号被工具观察到的比例 |
| 证据召回（已引用） | 标准信号被结论引用的比例 |
| 幻觉率 | 结论中无法对应到真实工具调用与信号的证据占比 |
| 平均步数 / 工具调用 / 耗时 / token | 每次诊断的均值 |
| 越权次数 | 白名单外调用 + 未审批写操作 + 工具级策略拒绝，要求为 0 |
| 恢复后干净 | 沙箱是否回到无故障状态 |

幻觉率是代理指标：只能发现引用对不上的断言，发现不了引用对得上但推理错误的断言。

## 结果

沙箱后端、确定性参考策略（`RD_LLM_PROVIDER=reference`）：

| 组 | Top-1 | Top-3 | 证据召回(已观察) | 证据召回(已引用) | 幻觉率 | 平均步数 | 平均工具调用 | 越权 |
|---|---|---|---|---|---|---|---|---|
| A | 43.8% | 87.5% | 0.0% | 0.0% | 0.0% | 3.0 | 0.0 | 0 |
| B | 81.2% | 100.0% | 81.2% | 75.0% | 0.0% | 5.0 | 6.6 | 0 |
| C | 93.8% | 93.8% | 87.5% | 78.1% | 0.0% | 5.0 | 7.6 | 0 |
| D | 100.0% | 100.0% | 100.0% | 90.6% | 0.0% | 7.4 | 10.4 | 0 |

手册覆盖类别与 held-out 类别（S04/S11/S13/S15）分别统计：

| 组 | 手册覆盖 | held-out |
|---|---|---|
| A | 41.7% | 50.0% |
| B | 83.3% | 75.0% |
| C | 100.0% | 75.0% |
| D | 100.0% | 100.0% |

预算收紧（`max_tool_calls=3`）：

| 配置 | Top-1 | Top-3 | 证据召回(已观察) | 平均工具调用 |
|---|---|---|---|---|
| D | 100.0% | 100.0% | 100.0% | 10.4 |
| D，预算 3 | 81.2% | 87.5% | 43.8% | 3.0 |

原始数据：[`eval/results/reference-3x.json`](../eval/results/reference-3x.json)、
[`eval/results/budget-pressure.json`](../eval/results/budget-pressure.json)、
[`eval/results/RESULTS.md`](../eval/results/RESULTS.md)。

## 复现真实模型的结果

```bash
export RD_LLM_PROVIDER=openai_compat
export RD_LLM_BASE_URL=https://api.deepseek.com/v1
export RD_LLM_API_KEY=... RD_LLM_MODEL=deepseek-chat RD_LLM_TEMPERATURE=0
python rdctl.py eval --runs 3 --out eval/results/llm-deepseek.json
```

CI 使用 `reference` 或录制回放（`eval/cassettes/`），不调用真实模型。

## 方法限制

1. 沙箱信号确定、不抖动，真实集群存在噪声、指标缺失与日志截断，沙箱准确率偏乐观。
2. 提交的数字来自参考策略而非大模型，真实模型需自行运行。
3. 16 类单点故障，不含级联故障、多故障并发、慢演进故障。
4. 3 次重复足以暴露不稳定，但不足以给出置信区间。


# 评测方法

## 1. 实验设计

16 个故障场景 × 4 个对照组 × 3 次重复 = 192 次运行；另有 1 组"预算收紧"稳健性实验
（D 组 + `max_tool_calls=3`）。

| 组 | 配置 | 想回答的问题 |
|---|---|---|
| A | 只给告警文本 | 纯语言先验能做到多少 |
| B | 单次 ReAct（一批只读工具） | 工具带来多少增益 |
| C | B + 手册检索（RAG） | 手册带来多少增益，在 held-out 上是否也有效 |
| D | 完整版：假设验证循环 + 手册 | 迭代补证据能补上多少 |

每个场景在 YAML 里声明：注入方式、恢复方式、告警文本、标准根因类别、**必须观察到的
证据信号**、可接受的处置动作。

## 2. 指标定义

| 指标 | 定义 | 怎么算出来的 |
|---|---|---|
| 根因 Top-1 | `report.root_cause == scenario.category` | 类别匹配，不做字符串匹配 |
| 根因 Top-3 | 标准类别出现在"结论 + 按置信度排序的假设"前 3 | 同上 |
| 证据召回（已观察） | 标准信号被工具观察到的比例 | `required_signals ∩ observed_signals` |
| 证据召回（已引用） | 标准信号被结论引用的比例 | `required_signals ∩ cited_signals` |
| 幻觉率 | 结论里无法对应到真实工具调用的证据占比 | 引用不存在的 `call_id`，或该调用未产生所引信号 |
| 平均步数 / 工具调用 / 耗时 / token | 每次诊断的均值 | 图与注册表的预算记账 |
| 越权次数 | 白名单外调用 + 未审批写操作 + 工具级策略拒绝 | 审计计数，验收要求为 0 |
| 恢复后是否干净 | 每次运行结束后沙箱是否回到无故障状态 | 避免上次故障污染下一次 |

**幻觉率是一个代理指标**：它只能发现"引用对不上"的断言，不能发现"引用对得上但推理错误"
的断言。计划里的人工抽样 + LLM 判官是它的补充，本仓库没有把那一层伪装成自动化结果。

## 3. 怎么复现

```bash
# 沙箱后端（默认，不需要集群、不需要 API key）
python rdctl.py eval --runs 3
python rdctl.py report                # 从 latest.json 重新渲染 RESULTS.md

# 预算收紧的稳健性实验
python rdctl.py eval --runs 1 --variants D --budget-pressure --out eval/results/budget-pressure.json

# 真实模型（OpenAI 兼容：DeepSeek / Qwen / vLLM / OpenAI）
export RD_LLM_PROVIDER=openai_compat RD_LLM_BASE_URL=... RD_LLM_API_KEY=... RD_LLM_MODEL=...
python rdctl.py eval --runs 3 --out eval/results/llm-deepseek.json

# 真实集群
export RD_BACKEND=real RD_KUBECONFIG=... RD_PROM_URL=...
python rdctl.py eval --runs 3 --backend real --policy openai_compat
```

每次运行都会记录：策略名、模型名、prompt 版本、后端、预算、场景清单、耗时。
结果 JSON 提交进 `eval/results/`，所以任何一张表的数字都能回溯到某次具体运行。

## 4. 结果（本仓库提交的这次运行）

见 README 的"效果摘要"与 `eval/results/RESULTS.md`。要点：

- A 43.8% / B 81.2% / C 93.8% / D 100%（Top-1）
- 越权操作次数：0
- 泛化：C 在手册覆盖类别上 100%，held-out 上 75%；D 两组均 100%
- 预算收紧到 3 次工具调用时，D 掉到 81.2%、证据召回 43.8%——说明分数来自证据采集

## 5. 这个方法本身的局限

1. **沙箱信号干净**：注入产生的信号是确定的、不抖动的，所以准确率高于真实集群。
   真实集群会有噪声、指标缺失、日志被截断。
2. **参考策略不是模型**：提交的数字来自确定性参考策略（`RD_LLM_PROVIDER=reference`），
   它的作用是给出可复现的回归基线，并让 A/B/C/D 的差异只反映管线而不是模型抖动。
   真实模型的数字需要用上面的命令自己跑，README 不替代它。
3. **场景数量有限**：16 类故障覆盖常见故障，但没有覆盖级联故障、多故障同时发生、
   慢故障（几小时演进）等形态。
4. **单次重复次数少**：3 次重复足以暴露不稳定，但不足以给出置信区间。


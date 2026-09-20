# 架构与实现

## 数据流

```
Alertmanager webhook ─┐
rdctl / REST API   ───┼─▶ FastAPI ──▶ triage → plan → collect → evaluate → report
                      │                        │
                      │                 工具层（只读白名单）
                      │      K8s API / Redis ACL / Prometheus / 手册检索
                      │                        │
                      │                 轨迹库(SQLite)   审批闸门
                      └──────────────▶ 执行 L1(delete pod) → 回查验证
```

## 状态

`agent/state.py` 里的 `DiagnosisState` 是节点间唯一契约：

| 字段 | 内容 |
|---|---|
| `triage` | 对象、症状类别、时间窗（只做抽取，不下结论） |
| `hypotheses` | 候选根因、置信度、待验证证据、支持/反驳信号 |
| `tool_calls` | 每次调用的 `call_id`、参数、返回片段、信号、耗时、截断/脱敏标记 |
| `observed_signals` | 工具实际观察到的证据信号 |
| `report` | 根因、置信度、证据引用、排除项、建议动作、不确定性 |
| `approval` / `execution` | 审批请求与执行+验证结果 |
| `audit` / `budget` | 越权计数、调用计数、步数、耗时 |

结论里每条 `EvidenceRef` 都必须指向轨迹中存在的 `call_id`，且该调用必须真的产生过这个信号；
否则计入幻觉率（`eval/metrics.py`）。这条约束同时由
`tests/test_agent_graph.py::test_evidence_always_traces_back_to_a_tool_call` 固化。

## 两种后端

工具只依赖后端接口，`RD_BACKEND` 决定实现：

| 能力 | `sandbox` | `real` |
|---|---|---|
| K8s 读 | `sandbox/cluster.py` 渲染 Pod、事件、describe、日志 | `kubernetes` 客户端 + `doctor-reader` ServiceAccount |
| Redis 读 | 渲染 INFO / SLOWLOG / CONFIG | 直连 Redis，`doctor` ACL 用户 |
| Prometheus | 由沙箱状态计算指标 | Prometheus HTTP API + 预置模板 |
| 写操作 | 修改沙箱状态 | `doctor-actuator`（仅 delete pod） |

证据信号抽取写在 `tools/` 里而不是后端里，同一个信号在两种后端下含义一致。

宿主形态（agent 在集群外）需要额外两件事：`kubectl port-forward` 把 Redis 端口映射到
`127.0.0.1`（集群 DNS 在宿主机不可解析），以及 kubeconfig。见 `RD_REDIS_PORT_FORWARD_BASE`。

## 节点职责

| 节点 | 输入 | 输出 | 预算 |
|---|---|---|---|
| `triage` | 告警文本 | 对象、症状类别 | 1 步 |
| `plan` | triage + 手册片段 | 2-5 个候选假设及其所需证据 | 1 步 + 1 次 `kb_search` |
| `collect` | 未定假设 | 一批只读工具调用 | 每轮 1 步，每次调用计 1 |
| `evaluate` | 新信号 | 假设状态与置信度 | 每轮 1 步 |
| `report` | 状态 + 轨迹 | 结构化结论 | 1 步 |
| `approval` | L1 动作 | 挂起等待人工决定 | 0 |
| `execute` + `verify` | 批准 | 执行结果 + 回查 Pod/复制链路 | 2 次调用 |

预算超限时停止采集，用已有证据出结论并在 `report.budget_exhausted` 标注。

## 对照组的实现差异

| 组 | 工具 | 手册 | 轮次 | 每轮探针 |
|---|---|---|---|---|
| A | 关闭 | 否 | 0 | 0 |
| B | 开启 | 否 | 1 | 6 |
| C | 开启 | 是（手册决定第一批探针） | 1 | 6 |
| D | 开启 | 是 | 4（收敛后提前结束） | 4 |

## 参考策略

`agent/reference.py` 是用确定性规则替代模型的一组实现，接口与 LLM 版本相同
（`triage / plan / choose_probes / evaluate / converged / report`）。
它按信号与类别的对应表打分，另有 `priority` 用于打破平局：显式故障（Pod 被重建、
配置非法、镜像拉不到）优先于由下游症状推断出的类别（复制链路 down、延迟升高）。

真实模型走 `agent/llm_reasoner.py`，prompt 在 `agent/prompts.py`，输出的 `call_id`
与信号会先做校验再进入状态。


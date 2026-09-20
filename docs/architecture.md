# 架构

## 一次诊断的数据流

```
Alertmanager webhook ─┐
CLI (rdctl demo)  ────┼─▶ FastAPI 网关 ──▶ 诊断图（triage→plan→collect→evaluate→report）
REST /diagnose    ────┘        │                        │
                               │                 受控工具层（只读白名单）
                               │        ┌──────────┬──────────┬──────────┬─────────┐
                               │      K8s API    Redis(ACL)  Prometheus  知识库BM25
                               │        └──────────┴──────────┴──────────┴─────────┘
                               │                        │
                         轨迹存储(SQLite)         审批闸门(interrupt)
                               │                        │
                               └──────────────▶ 执行(L1: delete pod) → 验证
```

## 组件

| 组件 | 位置 | 职责 | 不做的事 |
|---|---|---|---|
| `agent/` | 诊断图 | triage/plan/collect/evaluate/report、预算、审批闸门 | 不直接访问集群，只通过工具层 |
| `tools/` | 受控工具层 | 白名单、脱敏、截断、预算、证据信号抽取 | 不放开任意 PromQL / redis-cli / exec |
| `sandbox/` | 被诊断对象（沙箱） | 渲染与真实集群同形态的可观测面 | 不接触宿主机、不模拟 K8s 内部 |
| `faultlab/` | 故障实验室 | 16 类故障的注入/恢复 + 标准答案 | 不在真实集群上自动执行（kubectl 路径需显式确认） |
| `eval/` | 评测 | 消融矩阵、指标、报告 | 不参与诊断逻辑 |
| `api/` | 服务入口 | webhook、REST、审批、轨迹 UI、自身指标 | 不绕过审批执行写操作 |
| `knowledge/` | 手册与索引 | 按现象分块的排障手册 | 不写 held-out 故障的答案 |

## 两种后端，同一套工具

| 能力 | 沙箱后端 | 真实后端 |
|---|---|---|
| K8s 读 | `sandbox.cluster.SimCluster` 渲染 | `kubernetes` 客户端 + 只读 ServiceAccount |
| Redis 读 | 沙箱状态渲染 INFO/SLOWLOG/CONFIG | 直连 Redis，使用只读 ACL 用户 `doctor` |
| Prometheus | 由沙箱状态计算指标 | Prometheus HTTP API + 固定查询模板 |
| 写操作 | 沙箱状态变更 | `doctor-actuator` ServiceAccount（仅 delete pod） |

切换只需要 `RD_BACKEND=sandbox|real`。**证据信号抽取写在工具里，不写在后端里**，
所以同一个信号在两种后端下含义一致。

## 状态与轨迹

`DiagnosisState` 是节点之间的唯一契约，包含：告警、triage、假设列表（含置信度与待验证
证据）、工具调用轨迹、观察到的信号、结论、审批、执行与验证、预算与审计计数。

每次工具调用的 `call_id` 会写进轨迹并落库（`api/store.py`），结论里的每条证据都引用
`call_id`。这让"证据可追溯"变成可校验的事实，而不是文档里的承诺：

- `tests/test_agent_graph.py::test_evidence_always_traces_back_to_a_tool_call`
- 评测中的"幻觉率"就是这样算出来的（引用不存在的 call_id 或该调用没有产生的信号）。

## 一次运行的时序

| 阶段 | 输入 | 输出 | 预算消耗 |
|---|---|---|---|
| triage | 告警文本 | 对象、症状类别、时间窗 | 1 步 |
| plan | triage + 手册片段 | 2-5 个候选假设（含所需证据） | 1 步 + 1 次 `kb_search` |
| collect | 假设 | 一批只读工具调用及其信号 | 每轮 1 步、每次调用 1 次额度 |
| evaluate | 新信号 | 假设状态（支持/反驳/未定） | 每轮 1 步 |
| report | 状态 + 轨迹 | 结构化结论（根因/证据/排除/建议） | 1 步 |
| approval | 建议中的 L1 动作 | 审批请求（挂起） | 0 |
| execute + verify | 人工批准 | 执行结果 + 回查 Pod/复制链路 | 2 次调用 |


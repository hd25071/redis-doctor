# 评测结果

- 生成时间: 1789905520.7719648
- 策略: `reference` / `reference-policy`
- 后端: `sandbox`（进程内沙箱集群）
- 每个场景每组的重复次数: 3
- 场景数: 16，其中 held-out: S04, S11, S13, S15
- 审批闸门: 开启（L1 写操作需批准）
- 总耗时: 0.91s
- 越权操作次数合计: **0**（验收要求为 0）

## 主表

| 组 | 根因 Top-1 | 根因 Top-3 | 证据召回(已观察) | 证据召回(已引用) | 幻觉率 | 平均步数 | 平均工具调用 | 平均耗时 | token/次 | 越权次数 |
|---|---|---|---|---|---|---|---|---|---|---|
| A 纯 LLM（仅告警文本） | 43.8% | 87.5% | 0.0% | 0.0% | 0.0% | 3.0 | 0.0 | 0.0s | 0.0 | 0 |
| B + 只读工具（单次 ReAct） | 81.2% | 100.0% | 81.2% | 75.0% | 0.0% | 5.0 | 6.56 | 0.002s | 0.0 | 0 |
| C + 知识库 RAG | 93.8% | 93.8% | 87.5% | 78.1% | 0.0% | 5.0 | 7.56 | 0.007s | 0.0 | 0 |
| D + 假设验证循环（完整版） | 100.0% | 100.0% | 100.0% | 90.6% | 0.0% | 7.44 | 10.38 | 0.007s | 0.0 | 0 |

## 逐场景 Top-1

| 场景 | 故障类别 | A | B | C | D | held-out |
|---|---|---|---|---|---|---|
| S01 | pod_restart | ❌ | ✅ | ✅ | ✅ | 否 |
| S02 | oom_killed | ✅ | ✅ | ✅ | ✅ | 否 |
| S03 | maxmemory_reached | ❌ | ✅ | ✅ | ✅ | 否 |
| S04 | network_partition | ✅ | ✅ | ✅ | ✅ | 是 |
| S05 | storage_provision | ✅ | ✅ | ✅ | ✅ | 否 |
| S06 | image_pull | ✅ | ✅ | ✅ | ✅ | 否 |
| S07 | auth_failure | ❌ | ✅ | ✅ | ✅ | 否 |
| S08 | dns_resolution | ❌ | ❌ | ✅ | ✅ | 否 |
| S09 | slow_query | ✅ | ✅ | ✅ | ✅ | 否 |
| S10 | max_clients | ✅ | ✅ | ✅ | ✅ | 否 |
| S11 | disk_full | ❌ | ✅ | ✅ | ✅ | 是 |
| S12 | insufficient_resources | ❌ | ✅ | ✅ | ✅ | 否 |
| S13 | cpu_throttling | ❌ | ❌ | ❌ | ✅ | 是 |
| S14 | replication_backlog | ❌ | ❌ | ✅ | ✅ | 否 |
| S15 | probe_misconfig | ✅ | ✅ | ✅ | ✅ | 是 |
| S16 | invalid_config | ❌ | ✅ | ✅ | ✅ | 否 |

## 失败样本（按类型归类）

| 场景 | 组 | 标准答案 | 预测 | 失败类型 | 已观察信号 | 工具调用 |
|---|---|---|---|---|---|---|
| S01 | A | pod_restart | probe_misconfig | 证据不足 | 无 | 0 |
| S01 | A | pod_restart | probe_misconfig | 证据不足 | 无 | 0 |
| S01 | A | pod_restart | probe_misconfig | 证据不足 | 无 | 0 |
| S03 | A | maxmemory_reached | oom_killed | 证据不足 | 无 | 0 |
| S03 | A | maxmemory_reached | oom_killed | 证据不足 | 无 | 0 |
| S03 | A | maxmemory_reached | oom_killed | 证据不足 | 无 | 0 |
| S07 | A | auth_failure | network_partition | 证据不足 | 无 | 0 |
| S07 | A | auth_failure | network_partition | 证据不足 | 无 | 0 |
| S07 | A | auth_failure | network_partition | 证据不足 | 无 | 0 |
| S08 | A | dns_resolution | network_partition | 证据不足 | 无 | 0 |
| S08 | A | dns_resolution | network_partition | 证据不足 | 无 | 0 |
| S08 | A | dns_resolution | network_partition | 证据不足 | 无 | 0 |

## 泛化：手册覆盖 vs held-out

| 组 | 手册覆盖类别 Top-1 | held-out 类别 Top-1 | held-out 场景 |
|---|---|---|---|
| A 纯 LLM（仅告警文本） | 41.7% | 50.0% | S04, S11, S13, S15 |
| B + 只读工具（单次 ReAct） | 83.3% | 75.0% | S04, S11, S13, S15 |
| C + 知识库 RAG | 100.0% | 75.0% | S04, S11, S13, S15 |
| D + 假设验证循环（完整版） | 100.0% | 100.0% | S04, S11, S13, S15 |

# 评测结果

- 生成时间: 1789917744.2401242
- 策略: `openai_compat` / `openai_compat:deepseek-flash`
- 后端: `sandbox`（进程内沙箱集群）
- 每个场景每组的重复次数: 1
- 场景数: 16，其中 held-out: S04, S11, S13, S15
- 审批闸门: 开启（L1 写操作需批准）
- 总耗时: 2794.76s
- 越权操作次数合计: **1**（验收要求为 0）

## 主表

| 组 | 根因 Top-1 | 根因 Top-3 | 证据召回(已观察) | 证据召回(已引用) | 幻觉率 | 平均步数 | 平均工具调用 | 平均耗时 | token/次 | 越权次数 |
|---|---|---|---|---|---|---|---|---|---|---|
| A 纯 LLM（仅告警文本） | 93.8% | 100.0% | 0.0% | 0.0% | 0.0% | 3.0 | 0.0 | 16.791s | 7559.4 | 0 |
| B + 只读工具（单次 ReAct） | 100.0% | 100.0% | 96.9% | 96.9% | 0.0% | 5.0 | 6.0 | 35.959s | 20948.7 | 0 |
| C + 知识库 RAG | 87.5% | 93.8% | 84.4% | 84.4% | 0.0% | 4.94 | 6.62 | 40.297s | 23360.4 | 1 |
| D + 假设验证循环（完整版） | 100.0% | 100.0% | 100.0% | 100.0% | 0.0% | 9.38 | 13.75 | 81.618s | 54129.9 | 0 |

## 逐场景 Top-1

| 场景 | 故障类别 | A | B | C | D | held-out |
|---|---|---|---|---|---|---|
| S01 | pod_restart | ✅ | ✅ | ❌ | ✅ | 否 |
| S02 | oom_killed | ✅ | ✅ | ✅ | ✅ | 否 |
| S03 | maxmemory_reached | ✅ | ✅ | ✅ | ✅ | 否 |
| S04 | network_partition | ✅ | ✅ | ❌ | ✅ | 是 |
| S05 | storage_provision | ✅ | ✅ | ✅ | ✅ | 否 |
| S06 | image_pull | ✅ | ✅ | ✅ | ✅ | 否 |
| S07 | auth_failure | ✅ | ✅ | ✅ | ✅ | 否 |
| S08 | dns_resolution | ✅ | ✅ | ✅ | ✅ | 否 |
| S09 | slow_query | ✅ | ✅ | ✅ | ✅ | 否 |
| S10 | max_clients | ❌ | ✅ | ✅ | ✅ | 否 |
| S11 | disk_full | ✅ | ✅ | ✅ | ✅ | 是 |
| S12 | insufficient_resources | ✅ | ✅ | ✅ | ✅ | 否 |
| S13 | cpu_throttling | ✅ | ✅ | ✅ | ✅ | 是 |
| S14 | replication_backlog | ✅ | ✅ | ✅ | ✅ | 否 |
| S15 | probe_misconfig | ✅ | ✅ | ✅ | ✅ | 是 |
| S16 | invalid_config | ✅ | ✅ | ✅ | ✅ | 否 |

## 失败样本（按类型归类）

| 场景 | 组 | 标准答案 | 预测 | 失败类型 | 已观察信号 | 工具调用 |
|---|---|---|---|---|---|---|
| S10 | A | max_clients | pod_restart | 证据不足 | 无 | 0 |
| S01 | C | pod_restart | storage_provision | 类别混淆 | kb_hit, pod_recreated_recently, pvc_pending, resource_missing | 7 |
| S04 | C | network_partition | dns_resolution | 类别混淆 | kb_hit, master_role_confirmed, replica_link_down, replica_role_confirmed, resource_missing | 7 |

## 泛化：手册覆盖 vs held-out

| 组 | 手册覆盖类别 Top-1 | held-out 类别 Top-1 | held-out 场景 |
|---|---|---|---|
| A 纯 LLM（仅告警文本） | 91.7% | 100.0% | S04, S11, S13, S15 |
| B + 只读工具（单次 ReAct） | 100.0% | 100.0% | S04, S11, S13, S15 |
| C + 知识库 RAG | 91.7% | 75.0% | S04, S11, S13, S15 |
| D + 假设验证循环（完整版） | 100.0% | 100.0% | S04, S11, S13, S15 |

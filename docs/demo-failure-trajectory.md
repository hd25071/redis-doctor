# 演示实录：S08（B 组）

由 `python scripts/capture_demo.py --scenario S08 --variant B` 生成，内容是一次真实运行的完整输出（含工具轨迹、证据、结论）。

```text
==============================================================================
场景 S08 — Headless Service 缺失导致域名解析失败   (标准答案: dns_resolution)
==============================================================================

--- 告警 ---
[P1] RedisReplicaCannotResolveMaster
namespace=demo instance=demo
summary: 副本 redis-demo-1 / redis-demo-2 日志出现 Name or service not known，主从同步持续失败。
labels: alertname=RedisReplicaCannotResolveMaster, severity=critical

--- triage ---
对象: ns=demo instance=demo pods=['redis-demo-1', 'redis-demo-2'] 症状=replication

--- 假设 ---
  [supported ] network_partition        conf=0.78  needs=['latency_degraded', 'replica_link_down']
  [unresolved] dns_resolution           conf=0.3   needs=['dns_resolution_failure', 'headless_service_missing', 'replica_link_down']
  [unresolved] replication_backlog      conf=0.4   needs=['latency_degraded', 'repl_backlog_small', 'replica_link_down', 'replication_full_resync_storm']

--- 工具调用轨迹 ---
  c001-ebb597 k8s_get_pods({"label_selector": ""}) -> signals=[] 
      | redis-demo-0 phase=Running   ready=True restarts=0 age=5400s
      | redis-demo-1 phase=Running   ready=True restarts=0 age=5400s
      | redis-demo-2 phase=Running   ready=True restarts=0 age=5400s
  c002-c93711 k8s_events({"involved_object": ""}) -> signals=[] 
      | Normal   Created                pod/redis-demo-0             x1 Created container redis
      | Normal   Created                pod/redis-demo-1             x1 Created container redis
      | Normal   Created                pod/redis-demo-2             x1 Created container redis
      | Normal   Pulled                 pod/redis-demo-0             x1 Container image "redis:7.2" already present on machine
  c003-65806b redis_info({"pod": "redis-demo-0", "section": "all"}) -> signals=['master_role_confirmed'] 
      | uptime_in_seconds: 5400
      | connected_clients: 3
      | maxclients: 10000
      | blocked_clients: 0
  c004-a2f1bb redis_info({"pod": "redis-demo-1", "section": "replication"}) -> signals=['replica_link_down', 'replica_role_confirmed'] 
      | role: replica
      | master_host: redis-demo-0.demo-headless.demo.svc.cluster.local
      | master_link_status: down
      | master_last_io_seconds_ago: 1
  c005-608e1d prom_query({"template": "redis_master_link_up"}) -> signals=['replica_link_down'] 
      | redis_master_link_up{pod="redis-demo-1"} 0
      | redis_master_link_up{pod="redis-demo-2"} 0
  c006-b58845 redis_config_get({"pod": "redis-demo-0", "param": "repl-backlog-size"}) -> signals=['repl_backlog_small'] 
      | repl-backlog-size = 1048576

--- 结论 ---
根因      : network_partition (主从之间网络被阻断)
置信度    : 0.7
摘要      : 根因判断为 network_partition（主从之间网络被阻断）。依据来自 2 条可追溯证据，工具: prom_query, redis_info；共执行 1 轮证据采集。
证据:
  - c004-a2f1bb redis_info [replica_link_down] master_link_status: down
  - c005-608e1d prom_query [replica_link_down] redis_master_link_up{pod="redis-demo-1"} 0
排除项:
  - dns_resolution: 已检查但未观察到关键证据: dns_resolution_failure
  - replication_backlog: 已检查但未观察到关键证据: replication_full_resync_storm
建议动作:
  - [read/low] 检查 NetworkPolicy 与主从链路连通性
不确定性:
  - 未完全排除的候选: dns_resolution, replication_backlog

--- 预算与安全 ---
步数/工具调用/耗时: {'max_steps': 12, 'max_tool_calls': 30, 'max_seconds': 180.0, 'steps': 5, 'tool_calls': 6, 'elapsed_seconds': 0.011, 'exhausted': False}
审计: {"total": 6, "unauthorized_attempts": 0, "blocked_unauthorized": 0, "blocked_unapproved_write": 0, "blocked_policy": 0, "blocked_budget": 0, "failed": 0, "by_tool": {"k8s_events": 1, "k8s_get_pods": 1, "prom_query": 1, "redis_config_get": 1, "redis_info": 2}}
状态: ok

恢复完成，集群干净: True
```

---
id: hb-pod-restart
title: 主节点 Pod 消失或被重建
category: pod_restart
source: redis-operator 第 4 章经验 / Redis 官方文档 troubleshooting
symptom: 主节点 Pod 在告警窗口内被删除并重新创建，期间客户端连接中断
---

## 现象

- 告警里出现对象消失的措辞（delete / 被删除 / 重新创建），并且该 Pod 的创建时间明显晚于同组其他 Pod。
- 重建期间写请求报错或超时；副本日志出现重新同步。
- 关键区分点：这是**工作负载层面的中断**，Redis 进程本身没有报错，容器也没有被内存或磁盘问题杀掉。

## 排查步骤

1. `kubectl get pods -n demo -o wide`：对比各 Pod 的 AGE，找出被重建的那个。
2. `kubectl get events -n demo --sort-by=.lastTimestamp`：找 `Killing`、`Started`、`SuccessfulCreate` 这类事件，它们直接证明"重建发生过"。
3. `kubectl describe pod <pod>`：确认 Last State 是 `Terminated` 且 Reason 不是 OOMKilled。
4. `redis-cli -h <pod> INFO replication`：确认复制链路是否已经恢复，副本的 `master_link_status` 是否回到 `up`。
5. 如果副本 `sync_full` 在重建后增加，说明发生了全量重同步（数据重新从主节点拷回）。

## 常见原因

- 人工误删、滚动升级、节点驱逐（eviction）或节点 NotReady 导致的重新调度。
- `kubectl delete pod` 后由 StatefulSet 重建：名称不变，但 UID 与创建时间都变了。

## 处置

- 不需要手工干预时，确认自愈完成并复查复制链路即可。
- 需要强制重建时，删除单个 Pod 是 L1 操作：由 StatefulSet 重建，风险中，必须人工审批。

## 坑

- 主节点重建后是一个**空数据集**，副本会从空的主节点做全量同步。真实环境里这一步可能带来数据丢失，必须在结论里明确说明，不能只说"已自愈"。


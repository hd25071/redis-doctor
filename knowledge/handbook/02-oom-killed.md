---
id: hb-oom-killed
title: 容器内存超限被 OOMKilled
category: oom_killed
source: Kubernetes 官方排障文档 - Pods / Redis 内存管理
symptom: 容器反复重启，Pod 状态显示上一次终止原因为 OOMKilled
---

## 现象

- `kubectl get pods` 的 restartCount 持续增长。
- 事件里有 `OOMKilling`，或 `describe` 的 Last State 显示 `Reason: OOMKilled`、`Exit Code: 137`。
- 延迟抖动的同时伴随周期性连接中断（每次重启都会断开客户端）。

## 排查步骤

1. `kubectl describe pod <pod>`：读 Last State 的 Reason；OOMKilled 是决定性证据。
2. `kubectl get events --field-selector involvedObject.name=<pod>`：确认 OOMKilling 的次数。
3. `kubectl get pod <pod> -o jsonpath='{.spec.containers[0].resources.limits.memory}'`：看 limit 是否被调小过。
4. `redis-cli INFO memory`：看 `used_memory` 与 `maxmemory`。容器 limit 与 Redis `maxmemory` 是两个不同层面的限制：容器被内核杀掉，通常意味着 `maxmemory` 设得比 limit 还大，或者根本没设。

## 常见原因

- 容器 memory limit 过低，或 Redis `maxmemory` 高于容器 limit。
- 短时间大写入（批量导入、大 key）导致内存峰值超过 limit。
- 客户端输出缓冲区失控（`client-output-buffer-limit` 未生效）。

## 处置

- 让 `maxmemory` 明显低于容器 memory limit，给 fork/COW 留出余量（经验值 60% 左右）。
- 调整 limit 属于 L2 变更，MVP 只生成建议命令，不自动执行。

## 坑

- 只看 restartCount 无法区分"被删"和"被 OOMKill"：一个是对象重建，一个是容器重启，只有前者会改变 Pod 的创建时间。


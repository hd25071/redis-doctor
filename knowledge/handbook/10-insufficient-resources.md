---
id: hb-insufficient-resources
title: 资源请求过大导致无法调度
category: insufficient_resources
source: Kubernetes 官方排障文档 - Pod scheduling
symptom: Pod 长期 Pending，FailedScheduling 提示 Insufficient memory/cpu
---

## 现象

- Pod 一直 `Pending`，没有容器启动日志。
- 事件：`0/1 nodes are available: 1 Insufficient memory, 1 Insufficient cpu`。
- 与 PVC Pending 的区别：事件里没有卷相关消息。

## 排查步骤

1. `kubectl describe pod <pod>`：读 FailedScheduling 消息里的具体原因。
2. `kubectl describe statefulset demo`：核对 requests/limits 是否被改大。
3. `kubectl describe node`：看 Allocatable 与已分配 requests 的差值。
4. 确认没有 taint/toleration 或亲和性问题（这类消息的关键词是 taint，不是 Insufficient）。

## 常见原因

- CR 里的 requests 被改成超过节点可分配量。
- 节点上其他工作负载把 requests 占满。
- 只改了 requests 没改 limits，调度器仍按 requests 计算。

## 处置

- 下调 requests 或扩容节点，都属于 L2 变更，只给建议命令。

## 坑

- requests 与 limits 语义不同：调度看 requests，运行时限制看 limits。


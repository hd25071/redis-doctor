---
id: hb-dns-resolution
title: 副本解析不到主节点域名
category: dns_resolution
source: redis-operator 第 4 章 4.1 / Kubernetes DNS for Services and Pods
symptom: 日志出现 Name or service not known，replicaof 一直失败
---

## 现象

- 副本日志：`Unable to connect to MASTER: Name or service not known`。
- `INFO replication` 显示 `master_link_status:down`，但主节点本身健康。
- `kubectl get svc -n demo` 里找不到 StatefulSet 对应的 Headless Service。

## 排查步骤

1. `kubectl get svc -n demo`：Headless Service 必须是 `ClusterIP: None` 的那一个。
2. 从副本 Pod 内部验证域名：`nslookup demo-0.demo-headless.demo.svc.cluster.local`。
3. 确认 StatefulSet 的 `serviceName` 与存在的 Service 名字一致。
4. 对照主节点的 `INFO replication`：主节点看起来正常，说明问题在"找得到主"这一步，而不是主节点本身。

## 常见原因

- Headless Service 被误删，operator 还没重建。
- StatefulSet 的 serviceName 指向了一个不存在的 Service。
- CoreDNS 故障（这时所有域名都会失败，而不是单个 Service）。

## 处置

- 恢复 Headless Service（重新 apply），副本会自动重连。

## 坑

- 域名解析失败与网络阻断都会让 `master_link_status` 变成 down，必须靠日志里的 `Name or service not known` 与"Service 对象是否存在"来区分。
- 复制目标一定要用 per-pod 的 Headless DNS，不要用 Pod IP：Pod IP 在重建后会变。


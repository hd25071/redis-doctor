---
id: hb-auth-failure
title: 密码 Secret 缺失或错误导致认证失败
category: auth_failure
source: Redis 官方文档 - Security / ACL
symptom: 日志出现 NOAUTH / WRONGPASS，复制中断，诊断工具自身也读不到 INFO
---

## 现象

- 副本日志出现 `MASTER aborted replication with an error: WRONGPASS` 或 `NOAUTH Authentication required`。
- 诊断工具调用 `INFO` 直接失败并返回 `NOAUTH`，说明认证链路本身断了。
- 事件里可能看到 Secret 被删除或更新。

## 排查步骤

1. `kubectl get secret redis-password -n demo`：确认 Secret 是否存在。
2. `kubectl describe pod <pod>`：看容器是否因为缺挂载而重启。
3. 日志里同时出现 WRONGPASS 与 link down 时，**优先判定认证问题**：网络类故障不会产生 WRONGPASS。
4. 用一个只读 ACL 用户手工验证：`redis-cli -u redis://doctor:<pw>@<host>:6379 INFO server`。

## 常见原因

- Secret 被误删或 key 名写错（必须叫 `password`）。
- 轮换密码时只更新了 Secret，没有让 Pod 重新读取。
- 主从两端密码不一致。

## 处置

- 恢复 Secret，然后重建使用错误密码的 Pod（L1，需审批）。

## 坑

- 认证失败会表现为"复制中断"，非常容易被误判成网络问题。日志里有没有 WRONGPASS 是最快的分岔判断。


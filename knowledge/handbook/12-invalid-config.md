---
id: hb-invalid-config
title: 非法配置导致 CrashLoopBackOff
category: invalid_config
source: redis-operator 第 4 章 4.3 / Redis 官方文档 - redis.conf
symptom: 容器反复重启，上一次容器日志出现 FATAL CONFIG FILE ERROR
---

## 现象

- Pod 状态 `CrashLoopBackOff`，restartCount 快速增长。
- `kubectl logs <pod> --previous` 里出现 `*** FATAL CONFIG FILE ERROR ***` 与 `Bad directive or wrong number of arguments`。
- 与 OOMKilled 的区别：Last State 的 Reason 不是 OOMKilled，日志里有配置解析错误。

## 排查步骤

1. `kubectl logs <pod> --previous`：读致命错误与出错行号。
2. `kubectl get configmap demo-config -o yaml`：核对 redis.conf 里那一行的指令名与参数个数。
3. `kubectl get statefulset demo -o yaml`：确认 ConfigMap 变更是否触发了滚动更新（operator 会把 redis.conf 的 checksum 打到 Pod 模板上）。

## 常见原因

- 指令拼写错误（`appendonlyy`）。
- 参数个数不对（`save` 需要成对参数）。
- 使用了当前 Redis 版本不支持的指令。

## 处置

- 回滚 ConfigMap 中的非法指令（L2，给建议命令），Pod 会自动重启恢复。

## 坑

- 配置错误会让容器启动即退出，日志只在 `--previous` 里；只看当前容器日志会看到空内容，从而误判成"没有日志就是没问题"。


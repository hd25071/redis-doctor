---
id: hb-maxmemory-reached
title: maxmemory 触顶且淘汰策略为 noeviction
category: maxmemory_reached
source: Redis 官方文档 - Memory optimization / Eviction policies
symptom: 写入被拒绝，报错 OOM command not allowed when used memory > maxmemory
---

## 现象

- 应用侧写入报错 `OOM command not allowed when used memory > 'maxmemory'`。
- `INFO stats` 里 `errorstat_OOM:count` 持续增长。
- 与容器被 OOMKill 的区别：**容器没有重启**，是 Redis 自己拒绝写入。

## 排查步骤

1. `redis-cli INFO memory`：比较 `used_memory` 与 `maxmemory`。
2. `redis-cli CONFIG GET maxmemory-policy`：`noeviction` 表示触顶后直接拒绝写入，而不是淘汰旧 key。
3. `redis-cli INFO stats | grep errorstat_OOM`：确认拒绝写入的计数。
4. 看这段时间是否有大 key 写入或缓存量自然增长。

## 常见原因

- `maxmemory` 设置低于业务真实数据量。
- 策略选成 `noeviction` 但业务把 Redis 当缓存用，期望被淘汰而不是报错。

## 处置

- 扩容 `maxmemory`（L2，建议命令），或把策略改成 `allkeys-lru` 之类（策略变更要评估数据是否允许丢失）。
- 立即缓解可以先扩容内存或清理无用大 key，但 `FLUSHALL`/`KEYS` 之类的命令在 redis-doctor 的命令白名单里是被拒绝的，永远不要指望 Agent 去执行它们。

## 坑

- `noeviction` 加高水位并不总是故障：要看 `errorstat_OOM` 是否在增长。没有写入被拒就不算故障。


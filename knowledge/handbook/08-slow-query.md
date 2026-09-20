---
id: hb-slow-query
title: 慢查询拖垮延迟
category: slow_query
source: Redis 官方文档 - Latency troubleshooting / SLOWLOG
symptom: p99 延迟飙升，SLOWLOG 里有高复杂度命令
---

## 现象

- 应用侧超时增多，`redis_commands_duration_seconds` p99 明显升高。
- SLOWLOG 里有 `KEYS`、`SORT`、`HGETALL`、`SMEMBERS` 这类 O(N) 命令。
- 与 CPU 受限的区别：Redis 自己记录到慢日志，说明它拿到了 CPU，只是执行得慢。

## 排查步骤

1. `redis-cli SLOWLOG GET 10`：看 duration_us 与被执行的命令。
2. `redis-cli SLOWLOG LEN`：判断慢查询是否持续产生。
3. `redis-cli INFO memory`：确认是否伴随大 key 与内存上涨。
4. 用 `CLIENT LIST` 定位来源客户端 IP，找到发出慢命令的调用方。

## 常见原因

- 客户端误用 `KEYS` 扫全库、对大集合做 `SMEMBERS`/`HGETALL`。
- 大 key（单 key 上百万元素）导致单命令耗时线性增长。
- 过期 key 集中释放（同一秒大量 key 过期）。

## 处置

- 推动调用方用 `SCAN` 代替 `KEYS`，拆分大 key，并在调用侧对重命令做节流。
- 只读工具不会去执行 `KEYS`，也建议不要在生产上手工执行它。

## 坑

- 只看延迟指标容易把慢查询和 CPU 资源问题搞混；慢日志才是决定性证据。

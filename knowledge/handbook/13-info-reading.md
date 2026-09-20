---
id: hb-info-reading
title: 怎么读 Redis INFO（诊断基线）
category: general
source: Redis 官方文档 - INFO
symptom: 拿到 INFO 输出后，先看哪些字段
---

## 排查步骤

1. `replication`：`role`、`master_link_status`、`master_last_io_seconds_ago`、`master_repl_offset` 与副本 `slave_repl_offset` 的差值。
2. `memory`：`used_memory` 与 `maxmemory` 的比值、`maxmemory_policy`。
3. `clients`：`connected_clients` / `maxclients` / `blocked_clients` / `rejected_connections`。
4. `persistence`：`rdb_last_bgsave_status`、`aof_last_write_status`。
5. `stats`：`sync_full`、`sync_partial_ok`、`errorstat_*` 计数。

## 坑

- `role` 字段在副本上是 `slave`（Redis 7 也保留了这个写法），不要以为是异常。
- 只读用户可能看不到某些 section，读不到时要在结论里说明，而不是当成"没问题"。


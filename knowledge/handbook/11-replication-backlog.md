---
id: hb-replication-backlog
title: repl-backlog 过小导致全量同步风暴
category: replication_backlog
source: redis-operator 第 4 章 4.2 / Redis 官方文档 - Replication
symptom: 副本反复 Full resync，sync_full 快速增长
---

## 现象

- 副本日志多次出现 `Full resync from master`。
- `INFO stats` 里 `sync_full` 快速增长，`sync_partial_ok` 很少。
- 主从链路时断时续，但每次都能恢复（与持续性的链路中断不同）。

## 排查步骤

1. `redis-cli INFO stats`：对比 `sync_full` 与 `sync_partial_ok`。
2. `redis-cli CONFIG GET repl-backlog-size`：backlog 太小会导致短暂断连后无法做部分重同步。
3. `redis-cli CONFIG GET repl-timeout`：超时过小同样会触发频繁重连。
4. 看主节点 `INFO replication` 的 `repl_backlog_histlen` 是否已经顶到 `repl_backlog_size`。

## 常见原因

- `repl-backlog-size` 默认 1MB，写入量大时几秒钟就会被消耗掉。
- 网络抖动加 backlog 不够，每次都退化成全量同步。
- `repl-timeout` 设置过激（例如 5s），把正常的网络抖动当成断链。

## 处置

- 调大 `repl-backlog-size`（例如 64mb）并适当放宽 `repl-timeout`（L2，给建议命令）。

## 坑

- 全量同步会把主节点内存与网络打满，形成正反馈；越晚处理越糟。

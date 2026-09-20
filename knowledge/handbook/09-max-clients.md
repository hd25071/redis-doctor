---
id: hb-max-clients
title: 连接数耗尽
category: max_clients
source: Redis 官方文档 - Clients / maxclients
symptom: connected_clients 达到 maxclients，新连接被拒
---

## 现象

- `INFO clients` 显示 `connected_clients` 接近或等于 `maxclients`。
- `rejected_connections` 持续增长，应用报连接被拒。
- 日志可能出现 `max number of clients reached`。

## 排查步骤

1. `redis-cli INFO clients`：对比 connected_clients 与 maxclients。
2. `redis-cli CLIENT LIST`：按 addr 统计来源，找出连接数异常的客户端。
3. 检查应用侧连接池配置：最大连接数、空闲回收、是否每个请求新建连接。
4. 关注 `blocked_clients` 是否也在涨（可能同时存在阻塞命令）。

## 常见原因

- 连接池泄漏（拿连接不还）。
- 客户端数量随扩容线性增长，超过 maxclients。
- 大量短连接（没有复用）在高峰打满名额。

## 处置

- 修连接池是第一优先级；临时可以提高 maxclients（L2，注意同时要保证 fd 上限）。

## 坑

- maxclients 同时受操作系统 `ulimit -n` 限制，只调 maxclients 可能无效。


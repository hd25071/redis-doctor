# 真机验证记录

环境：k3s v1.30.4+k3s1（独立 Docker 容器 `redis-doctor-k3s`，2 CPU / 3GiB，端口只绑
127.0.0.1:16443），被诊断对象是 redis-operator:dev 生成的 3 副本集群 `demo`，
启用密码 Secret。Agent 跑在宿主机（`RD_BACKEND=real` + kubeconfig +
`kubectl port-forward` 16379-16381）。未部署 Prometheus，因此指标类证据不可用。

## 通过项

| 项目 | 结果 |
|---|---|
| `rdctl cluster check` | 4/4（monitoring 与 in-cluster Agent 记为跳过） |
| `rdctl cluster rbac-check` | 7/7，见 [security.md](security.md) |
| Redis 只读 ACL | `doctor` 执行 `SET` / `FLUSHALL` → NOPERM；`INFO` / `ROLE` / `CONFIG GET` 正常 |
| S01 主节点 Pod 被删 | 注入 ✅ 诊断 `pod_restart` ✅ 关键证据 ✅ 恢复 ✅ 恢复后健康 ✅ |
| S06 镜像拉取失败 | 注入 ✅ 诊断 `image_pull` ✅ 关键证据 ✅ 恢复 ✅ 恢复后健康 ✅ |

原始记录：[`eval/results/real-cluster-final.json`](../eval/results/real-cluster-final.json)、
[`eval/results/real-cluster.json`](../eval/results/real-cluster.json)。

复现：

```bash
export KUBECONFIG=./k3s.yaml RD_BACKEND=real
export RD_REDIS_ACL_USER=doctor RD_REDIS_ACL_PASSWORD=... RD_REDIS_PORT_FORWARD_BASE=16379
python scripts/verify_real_cluster.py --scenarios S01,S06 --wait 20 --settle 40
```

## 未通过 / 未复现

| 场景 | 状态 | 原因 |
|---|---|---|
| S12 资源不足 | 注入可执行，故障未出现 | requests=6Gi 时节点仍报 5.8Gi allocatable，Pod 照常调度；已改为 64Gi，未复测 |
| S14 全量同步风暴 | 注入可执行，故障未出现 | 只改 backlog 再重启副本，偏移差距太小，部分重同步仍成功；已加入写入压力，未复测 |
| S13 CPU 限流 | 未验证 | 诊断依赖 cfs throttled 指标，本环境没有 Prometheus |
| S05 PVC Pending | 未验证 | operator 未暴露 `storageClassName`，直接改 StatefulSet 会被调谐覆盖 |
| S15 探针错误 | 未验证 | operator 未暴露探针配置，需先给 operator 加 `spec.probe` |
| S09 / S10 / S11 | 未验证 | 分别需要 POSIX shell（Windows 宿主回退到 cmd）、连接压测、约 1GB 顺序写 |

每个场景的 YAML 里都有 `verification_note`，写明具体原因与复现前提。

## 真机发现并修复的问题

| 问题 | 修复 |
|---|---|
| operator 的真实命名是 `redis-<name>` / `redis-<name>-<i>` / `data-redis-<name>-<i>`，沙箱按 `demo-<i>` 建模 | 沙箱与场景统一为真实命名（`sandbox/cluster.py` 的命名函数） |
| 工具注册表首参数 `name` 与 `k8s_get_resource(kind, name)` 冲突，整条诊断返回 error | 改名为 `tool_name`，并在测试里覆盖带 `name` 参数的工具 |
| Kubernetes 事件保留 1 小时，20 分钟前已恢复的 ImagePullBackOff 让后续诊断全判 image_pull | 事件带 `ageSeconds`，超过 900 秒不作为故障证据（新增 `stale_event_evidence`） |
| 把 `spec.version` 改回正确标签后，处于 ImagePullBackOff 的 Pod 不会被替换（StatefulSet 有序滚动在等它 Ready） | S06 恢复步骤增加删除该 Pod |
| 宿主机形态无法解析集群 DNS、Pod IP 不可达 | 增加 `RD_REDIS_PORT_FORWARD_BASE`，用 port-forward 访问 Redis |
| Redis ACL 用户只在内存中，Pod 重启后 `doctor` 消失（operator 无 `aclfile`） | S07 恢复步骤包含重跑 ACL Job；文档中标注该 operator 缺口 |
| Windows 上 `shell=True` 走 cmd.exe，POSIX 单引号不被剥离，带引号的注入命令全部失败 | `faultlab/runner.run_shell` 优先使用可用的 POSIX shell，否则 `shlex` 直接执行 |
| 参考策略在"Pod 被重建"与"链路 down"同时出现时选中后者（都是 2 分的平局） | 类别增加 `priority`：显式故障优先于由症状推断的类别 |

## 环境说明

k3s 容器是本仓库专用的隔离实例，与宿主机上原有的业务容器、监控栈无关，端口只绑回环。
删除它即可完全清理：

```bash
docker rm -f redis-doctor-k3s
```


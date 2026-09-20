# 运维手册

## 1. 本地（Windows / macOS / Linux，零集群）

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # Windows: .venv\Scripts\pip
python rdctl.py kb build              # 建索引
python rdctl.py demo --scenario S02   # 注入 → 诊断 → 轨迹 → 审批 → 验证
python rdctl.py eval --runs 3         # 完整消融
python rdctl.py serve --port 8099     # 打开 http://127.0.0.1:8099/ui
```

前端占用 8099（可用 `--port` 改），不会与本机既有的 3000/9090/9093/9100/8000 冲突。

## 2. 容器化演示（不影响宿主机既有容器）

```bash
docker compose -f deploy/compose/docker-compose.sandbox.yml up -d
curl -s http://127.0.0.1:8099/healthz
```

compose 项目名是 `redis-doctor`，独立网络 `redis-doctor-net`、独立卷
`redis-doctor-data`，端口只绑 127.0.0.1:8099，非 root 用户运行。

## 3. Linux 服务器（长期演示环境）

```bash
sudo bash deploy/cluster/k3s-install.sh            # k3s + 三个命名空间
kubectl apply -f deploy/redis/demo-redis-cr.yaml   # 被诊断的 3 副本 Redis
kubectl apply -f deploy/redis/acl-init-job.yaml    # Redis 只读 ACL
kubectl apply -f deploy/monitoring/redis-exporter.yaml
kubectl apply -f deploy/monitoring/prometheus-rules.yaml
kubectl apply -k deploy/overlays/server            # Agent（集群内，real 后端）
python rdctl.py cluster check                      # 见下
```

## 4. 验收清单（`make smoke` 的等价命令）

```bash
python rdctl.py cluster check          # 组件与 CR 是否就绪
python rdctl.py cluster rbac-check     # kubectl auth can-i 断言
```

清单：

- 全新机器按 README 30 分钟内拉起全部组件
- `cluster check` 全绿
- `doctor-reader` 不能删 Pod、不能读 Secret、不能 exec；`doctor-actuator` 只能删 Pod
- Redis `doctor` 用户执行写命令被拒绝（`redis-cli -u redis://doctor:... SET k v` 应报 NOPERM）
- NetworkPolicy 生效：Agent 访问无关命名空间被拒
- `make backup` 成功，且完成过一次恢复演练
- 镜像 trivy 扫描无高危，仓库里查不到明文密钥

## 5. 备份与恢复演练

```bash
python rdctl.py backup        # SQLite .backup + 索引 tar，保留最近 7 份
```

恢复演练（在独立目录上做，不覆盖运行中的数据）：

```bash
python -c "from pathlib import Path; from ops_backup import restore; import rdconfig; \
s=rdconfig.Settings(); s.data_dir='data/restore-drill'; restore(Path('data/backups/<archive>.sqlite3'), s)"
```

**没有恢复演练的备份不算备份。** 演练记录追加到本文件末尾的表格。

## 6. 升级、回滚与迁移

```bash
kubectl set image deploy/redis-doctor agent=ghcr.io/OWNER/redis-doctor:<git-sha> -n redis-doctor
kubectl rollout status deploy/redis-doctor -n redis-doctor
kubectl rollout undo deploy/redis-doctor -n redis-doctor      # 回滚
```

数据库迁移是启动时自动执行的编号迁移；运维要求迁移前先跑一次 `backup`。

## 7. 常见坑

| 坑 | 应对 |
|---|---|
| 容器化 k3s 的 kubeconfig 里 server 是 127.0.0.1，Agent 容器连不上 | 开发期 Agent 跑宿主机；必须容器化时改用 `host.docker.internal` 或在集群内跑 |
| 容器化 k3s 重启丢 PVC 数据 | 挂宿主机卷，或直接在 Linux 服务器上做长期环境 |
| 国内拉镜像失败 | 先配 `deploy/cluster/registries.yaml.example`，重启 k3s |
| 指标与事件时间对不上 | 统一 UTC，服务器开时间同步 |
| 同一告警反复触发诊断/审批 | fingerprint 去重 + 900s 冷却（`RD_ALERT_COOLDOWN_SECONDS`） |
| exporter 只抓到一个 Pod | 用 sidecar 或多目标模式 + relabel（见 `deploy/monitoring/redis-exporter.yaml`） |
| 诊断结论"没有证据" | 检查该场景的 `required_signals` 是否能被工具观察到；`tests/test_faultlab.py` 会拦住这类回归 |

## 8. 演练记录

| 日期 | 项目 | 结果 | 备注 |
|---|---|---|---|
| 2026-09-20 | 备份 + 恢复（SQLite 轨迹库） | 待执行 | 需在目标主机执行后补记 |
| 2026-09-20 | 回滚（rollout undo） | 待执行 | 需要真实集群 |


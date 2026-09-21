# 部署与运维

## 本机（无集群）

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
python rdctl.py kb build
python rdctl.py demo --scenario S02
python rdctl.py eval --runs 3
python rdctl.py serve --port 8099      # http://127.0.0.1:8099/ui
```

## 容器

```bash
docker compose -f deploy/compose/docker-compose.sandbox.yml up -d
curl -s http://127.0.0.1:8099/healthz
```

compose 项目名 `redis-doctor`，独立网络与卷，端口只绑 127.0.0.1，非 root 运行。

## k3s（长时间运行）

```bash
sudo bash deploy/cluster/k3s-install.sh          # k3s + 命名空间
kubectl apply -f deploy/base/namespace.yaml
kubectl apply -f deploy/base/serviceaccount.yaml
kubectl apply -f deploy/base/rbac.yaml

# 被诊断的 Redis
kubectl create secret generic redis-password -n demo --from-literal=password="$(openssl rand -hex 16)"
kubectl create secret generic redis-doctor-acl -n demo --from-literal=password="$(openssl rand -hex 16)"
kubectl apply -f deploy/redis/demo-redis-cr.yaml
kubectl apply -f deploy/redis/acl-init-job.yaml
kubectl wait --for=condition=complete job/redis-doctor-acl-init -n demo --timeout=180s

# Agent（集群内形态）
kubectl apply -k deploy/overlays/server
python rdctl.py cluster check && python rdctl.py cluster rbac-check
```

宿主机形态（agent 在集群外）不需要部署 Agent 工作负载，只需要 kubeconfig 与
`kubectl port-forward` 到 Redis：

```bash
for i in 0 1 2; do kubectl -n demo port-forward pod/redis-demo-$i $((16379+i)):6379 & done
export RD_BACKEND=real RD_KUBECONFIG=$PWD/k3s.yaml
export RD_REDIS_ACL_USER=doctor RD_REDIS_ACL_PASSWORD=... RD_REDIS_PORT_FORWARD_BASE=16379
python rdctl.py serve --port 8099
```

## 验收清单

```bash
python rdctl.py cluster check          # 组件与 CR 就绪
python rdctl.py cluster rbac-check     # kubectl auth can-i 断言
```

- `cluster check` 与 `rbac-check` 通过（monitoring / in-cluster Agent 未部署时记为跳过）
- Redis `doctor` 用户写命令被拒（NOPERM）
- Alertmanager webhook 未带 token 时返回 401
- `make backup` 成功，并完成一次恢复演练

## 备份与恢复

```bash
python rdctl.py backup                    # SQLite .backup + 索引 tar，保留最近 7 份
python -c "from pathlib import Path; from ops_backup import restore; import rdconfig; \
s=rdconfig.Settings(); s.data_dir='data/restore-drill'; restore(Path('data/backups/<archive>.sqlite3'), s)"
```

## 升级与回滚

```bash
kubectl set image deploy/redis-doctor agent=ghcr.io/OWNER/redis-doctor:<git-sha> -n redis-doctor
kubectl rollout status deploy/redis-doctor -n redis-doctor
kubectl rollout undo deploy/redis-doctor -n redis-doctor
```

数据库迁移是启动时执行的编号迁移；运维要求迁移前先 `python rdctl.py backup`。

## 故障注入（真机）

场景里的 kubectl 命令由 `faultlab/runner.run_shell` 执行，默认 dry-run，需要显式执行：

```bash
python scripts/verify_real_cluster.py --scenarios S01,S06 --wait 20 --settle 40
```

注意：Kubernetes 事件保留约 1 小时，连续注入多个故障时应先清理命名空间事件，
否则前一个故障的残余事件会污染下一个诊断。脚本会检查注入前集群是否健康。

## 常见问题

| 现象 | 处理 |
|---|---|
| 宿主机上 `redis_info` 报 getaddrinfo 失败 | 集群 DNS 不可解析，使用 `RD_REDIS_PORT_FORWARD_BASE` 或把 Agent 部署到集群内 |
| Redis 报 NOPERM / NOAUTH（`doctor` 用户不存在） | Pod 重启后 ACL 丢失，重跑 `deploy/redis/acl-init-job.yaml` |
| 注入命令在 Windows 上失败 | 命令使用 POSIX 引号，需要可用的 `sh`/`bash`；否则用 `RD_SHELL` 指定，或直接对沙箱后端运行 |
| 同一告警反复触发诊断 | Alertmanager fingerprint 去重 + `RD_ALERT_COOLDOWN_SECONDS` 冷却 |
| 控制台「注入并诊断」或 `/diagnose` 返回 401 | 写操作需认证：设置 `RD_API_TOKEN`（或 `RD_WEBHOOK_TOKEN`）并带 `Authorization: Bearer <token>` |
| exporter 只抓到一个 Pod | 使用 sidecar 或多目标模式 + relabel（`deploy/monitoring/redis-exporter.yaml`） |
| 结论里证据为空 | 检查对应场景的 `required_signals` 是否能被工具观察到，`tests/test_faultlab.py` 会拦截这类回归 |

# redis-doctor

Redis on Kubernetes 的故障诊断 Agent。16 类故障可一键注入与恢复，每次诊断给出一条可追溯到
具体工具调用的证据链；写操作（删除 Pod 等）默认不执行，需人工审批。

被诊断对象是 redis-operator 生成的 3 副本主从集群（`ops.example.com/v1alpha1` 的 `Redis` CR）。

```
python rdctl.py demo --scenario S02      # 注入 → 诊断 → 证据链 → 审批 → 验证
python rdctl.py eval --runs 3            # 16 场景 × 4 组对照评测
python rdctl.py serve --port 8099        # 轨迹 UI: http://127.0.0.1:8099/ui
```

无需集群、无需 Docker、无需 API key。

## 结果

### 真实模型（deepseek-flash，16 场景 × 1 次，合计 $0.32）

| 组 | 根因 Top-1 | 根因 Top-3 | 证据召回(已观察 / 已引用) | 幻觉率 | 平均工具调用 | 耗时/次 | 越权调用 |
|---|---|---|---|---|---|---|---|
| A 仅告警文本 | 93.8% | 100.0% | — | 0.0% | 0 | 17s | 0 |
| B + 只读工具（单次） | 100.0% | 100.0% | 96.9% / 96.9% | 0.0% | 6.0 | 36s | 0 |
| C + 手册检索 | 87.5% | 93.8% | 84.4% / 84.4% | 0.0% | 6.6 | 40s | 1 |
| D + 假设验证循环 | 100.0% | 100.0% | 100.0% / 100.0% | 0.0% | 13.8 | 82s | 0 |

D 组重复 3 次（48 次诊断，$0.43）：Top-1 95.8%（16/16、14/16、16/16），Top-3 97.9%，
证据召回 96.9%，越权调用 2 次。波动只出现在 S04 与 S10，其余 14 个场景 3/3 稳定。

三点结论：

- A 组 93.8% 说明该模型只看告警文本就能猜得不错；工具的价值体现在 D 组的证据召回 100%、
  幻觉率 0 与可追溯的引用，而不是单看准确率。
- C 组低于 B 组（87.5% vs 100%）：在这 16 个场景上手册检索没有带来增益，还引入两个错例
  （S01、S04）。这与沙箱参考策略下的结论相反，是下一轮要查的点。
- 越权调用 3 次（C 组 1 次、D 组 3 次重复中 2 次）全部被工具层拒绝并计入审计。

原始结果：[llm-abcd-1x.json](eval/results/llm-abcd-1x.json)、
[llm-d-3x.json](eval/results/llm-d-3x.json)。

### 参考策略（沙箱，用于回归）

16 个故障场景 × 4 个对照组 × 3 次重复 = 192 次运行（沙箱后端，`python rdctl.py eval --runs 3`）：

| 组 | 根因 Top-1 | 根因 Top-3 | 证据召回(已观察) | 证据召回(已引用) | 幻觉率 | 平均工具调用 | 越权次数 |
|---|---|---|---|---|---|---|---|
| A 仅告警文本 | 43.8% | 87.5% | 0.0% | 0.0% | 0.0% | 0 | 0 |
| B + 只读工具（单次） | 81.2% | 100.0% | 81.2% | 75.0% | 0.0% | 6.6 | 0 |
| C + 手册检索 | 93.8% | 93.8% | 87.5% | 78.1% | 0.0% | 7.6 | 0 |
| D + 假设验证循环 | 100.0% | 100.0% | 100.0% | 90.6% | 0.0% | 10.4 | 0 |

手册里刻意不收录的 4 类故障（网络阻断、磁盘写满、CPU 限流、探针错误）单独统计：

| 组 | 手册覆盖类别 | held-out 类别 |
|---|---|---|
| A | 41.7% | 50.0% |
| B | 83.3% | 75.0% |
| C | 100.0% | 75.0% |
| D | 100.0% | 100.0% |

工具调用预算从 30 收紧到 3 时，D 组降到 81.2%、证据召回 43.8%（`--budget-pressure`）：
分数随证据量变化，而不是来自固定的类别映射。

数字口径：下面沙箱表来自 `RD_LLM_PROVIDER=reference`（确定性参考策略，不是大模型），用于给出
可复现的回归基线。跑真实模型：

```bash
export RD_LLM_PROVIDER=openai_compat RD_LLM_BASE_URL=... RD_LLM_API_KEY=... RD_LLM_MODEL=...
python rdctl.py eval --runs 3 --out eval/results/llm.json
```

原始结果：[`eval/results/reference-3x.json`](eval/results/reference-3x.json)、
[`eval/results/budget-pressure.json`](eval/results/budget-pressure.json)。

## 真机验证

在 k3s v1.30.4 + redis-operator:dev 的 3 副本集群上验证过（隔离容器，2 CPU / 3GiB）：

| 项目 | 结果 |
|---|---|
| RBAC 断言（`rdctl cluster rbac-check`） | 7/7：reader 不能删 Pod / 读 Secret / exec，actuator 只能删 Pod |
| Redis 只读 ACL | `doctor` 用户 `SET`、`FLUSHALL` 返回 NOPERM；`INFO`、`ROLE`、`CONFIG GET` 正常 |
| S01 主节点 Pod 被删 | 注入 ✅ 诊断 `pod_restart` ✅ 关键证据 ✅ 恢复 ✅ |
| S06 镜像拉取失败 | 注入 ✅ 诊断 `image_pull` ✅ 关键证据 ✅ 恢复 ✅ |
| S12 资源不足 / S14 全量同步风暴 | 注入命令可执行，但在本机未真正制造出故障，见 [docs/real-cluster.md](docs/real-cluster.md) |

真机跑出来的四个问题都已修掉，细节见 [docs/real-cluster.md](docs/real-cluster.md)。

## 快速开始

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"        # Windows: .venv\Scripts\pip

python rdctl.py kb build                # 构建手册索引
python rdctl.py scenarios list          # 16 类故障
python rdctl.py demo -s S08             # 查看一个失败样本的完整轨迹
python rdctl.py eval --runs 3           # 评测
python rdctl.py serve --port 8099       # Web UI
python -m pytest -q                     # 测试
```

Makefile 提供同名目标：`make install / demo / eval / test / lint / api`。

容器化演示（独立 compose 项目、独立网络与卷，只绑 127.0.0.1:8099）：

```bash
docker compose -f deploy/compose/docker-compose.sandbox.yml up -d
```

部署到 k3s 见 [docs/deployment.md](docs/deployment.md)。

## 架构

```
Alertmanager webhook ─┐
rdctl / REST API   ───┼─▶ FastAPI ──▶ triage → plan → collect → evaluate → report
                      │                        │
                      │                 工具层（只读白名单）
                      │      K8s API / Redis ACL / Prometheus / 手册检索
                      │                        │
                      │                 轨迹库(SQLite)   审批闸门
                      │                        │
                      └──────────────▶ 执行 L1(delete pod) → 回查验证
```

一次诊断的状态包含：triage 结果、候选假设及其待验证证据、工具调用轨迹、观察到的证据信号、
结构化结论、审批与执行记录、预算与审计计数。结论里的每条证据引用一个 `call_id`，
评测据此计算幻觉率。设计与实现细节见 [docs/architecture.md](docs/architecture.md)。

| 目录 | 内容 |
|---|---|
| `agent/` | 诊断图、状态模型、prompt、确定性参考策略 |
| `tools/` | 白名单、脱敏、截断、预算、证据信号抽取、K8s/Redis/Prometheus/手册工具 |
| `sandbox/` | 进程内 Redis-on-K8s 模型，CI 与评测使用 |
| `faultlab/` | 16 个场景 YAML、注入 ops、runner |
| `eval/` | 消融 harness、指标、报告，`results/` 提交真实结果 |
| `api/` | webhook、REST、审批闸门、轨迹 UI、SQLite |
| `knowledge/` | 按现象分块的排障手册与索引 |
| `deploy/` | kustomize base/overlays、RBAC、NetworkPolicy、监控规则、compose |

## 工具层

只读（默认开放）

| 工具 | 说明 |
|---|---|
| `k8s_get_pods` | 状态、重启次数、上次终止原因 |
| `k8s_get_resource` | pod / statefulset / pvc / service / configmap / node / redis CR（不含 Secret） |
| `k8s_describe` | 对象状态、条件、资源、探针与事件 |
| `k8s_events` | 命名空间事件，含事件年龄 |
| `k8s_logs` | Pod 日志，支持 `previous=true` 读取上一次容器 |
| `redis_info` / `redis_slowlog` / `redis_config_get` / `redis_query` | 命令级白名单：INFO、ROLE、DBSIZE、CONFIG GET、SLOWLOG GET、CLIENT LIST |
| `prom_query` | 13 个预置查询模板，不接受任意 PromQL |
| `kb_search` | 手册检索，返回片段与出处（不作为证据） |

写操作（需审批）

| 等级 | 动作 | 本仓库行为 |
|---|---|---|
| L1 | `k8s_delete_pod` | 结论挂起为审批请求，人工批准后执行并回查 Pod 与复制链路 |
| L2 | 改 CR、扩容、改配置 | 只生成命令，不执行 |

诊断循环拿到的工具目录里没有写工具。

## 评测

每个场景在 `faultlab/scenarios/S*.yaml` 中声明注入、恢复、告警文本、标准根因类别和必须观察到的
证据信号。指标定义、复现命令与限制见 [docs/evaluation.md](docs/evaluation.md)。

### 失败案例

| 类型 | 典型样本 | 原因 |
|---|---|---|
| 证据不足 | A 组大部分场景；B 组的 S08/S13/S14 | 第一轮探针没有覆盖决定性证据 |
| 措辞误导 | A 组的 S01/S03/S07/S11 | 告警文本把候选引向错误的类别 |
| 引用不完整 | D 组少量信号 | 观察到但未写进结论文本（已引用 90.6%） |
| 泛化缺口 | C 组的 S13 | held-out 类别，手册中没有对应条目 |
| 事件残留 | 真机连续注入时出现 | 已恢复故障的事件在 1 小时内仍存在，已加年龄过滤 |

完整归类见 [docs/failure-cases.md](docs/failure-cases.md)，
一次真实的失败轨迹见 [docs/demo-failure-trajectory.md](docs/demo-failure-trajectory.md)。

| # | 故障 | 类别 | held-out |
|---|---|---|---|
| S01 | 主节点 Pod 被删除 | pod_restart | |
| S02 | 主节点 OOMKilled | oom_killed | |
| S03 | maxmemory 触顶且 noeviction | maxmemory_reached | |
| S04 | 副本与主断链 | network_partition | ✅ |
| S05 | PVC 一直 Pending | storage_provision | |
| S06 | 镜像拉取失败 | image_pull | |
| S07 | 密码 Secret 缺失或错误 | auth_failure | |
| S08 | Headless Service 缺失 | dns_resolution | |
| S09 | 慢查询拖垮延迟 | slow_query | |
| S10 | 连接数耗尽 | max_clients | |
| S11 | 数据盘写满 | disk_full | ✅ |
| S12 | 资源请求过大 | insufficient_resources | |
| S13 | CPU 限流 | cpu_throttling | ✅ |
| S14 | repl-backlog 过小 | replication_backlog | |
| S15 | 探针配置错误 | probe_misconfig | ✅ |
| S16 | 非法配置 CrashLoop | invalid_config | |

## 安全

三层独立防线，完整说明与残留风险见 [docs/security.md](docs/security.md)。

1. **身份层**：`doctor-reader` 只有 pods/pods-log/events/services/endpoints/pvc/statefulsets
   的读权限，无 Secret、无 `pods/exec`、无写动词；`doctor-actuator` 只有 `delete pods`。
   `python rdctl.py cluster rbac-check` 用 `kubectl auth can-i` 校验这几条。
2. **服务端层**：Redis 侧使用只读 ACL 用户 `doctor`（`-@all` + 白名单子命令），
   不依赖 `pods/exec`。
3. **客户端层**：命令白名单（`FLUSHALL`、`CONFIG SET`、`KEYS`、`DEBUG`、`EVAL`、`SLAVEOF`
   等直接拒绝）、输出脱敏（密码 / Token / JWT / `stringData`）、超长输出头尾截断并标注、
   每次诊断的步数 / 调用数 / 时间预算。

防提示注入：工具输出在 prompt 中标记为不可信数据；注入特征会被检测并计入指标；执行层只接受
结构化动作（动作类型 + 对象名）。

验收指标：越权操作次数为 0（192 次运行中为 0）。

## 已知限制

- 提交的评测数字来自确定性参考策略而非大模型；真实模型需自行运行。
- 沙箱信号是干净的、确定的，因此沙箱准确率高于真实集群。
- 真实集群上只完整验证了 S01、S06；其余场景的 kubectl 路径标记为
  `kubectl_verified: false`，原因逐条写在场景 YAML 的 `verification_note` 里。
- 只覆盖 16 类单点故障；级联故障、多故障并发、慢演进故障、Redis Cluster 模式不在范围内。
- L2 只生成建议命令；审批为单人单次，无双人复核与变更窗口。
- 幻觉率为代理指标：能发现引用对不上的断言，发现不了引用对得上但推理错误的断言。
- 知识库为 BM25 检索，同义改写召回弱；接口不变，可替换为向量检索。
- 轨迹库为 SQLite，Agent 单副本部署，不能水平扩容。
- 时间窗口外的历史事件不会作为证据（15 分钟），因此很慢演进的故障可能证据不足。

详细清单与运维注意事项见 [docs/limitations.md](docs/limitations.md)。

## 开发

```bash
ruff check . && ruff format --check .
python -m pytest -q
python -m knowledge.build_index          # 手册改动后重建索引
python scripts/capture_demo.py -s S02    # 重新生成 docs/demo-trajectory.md
```

CI 在 `.github/workflows/ci.yml`：lint-test（含安全负向测试与索引可复现性检查）、gitleaks、
镜像构建与 trivy 扫描，以及可选的真实集群 e2e。

## License

MIT，见 [LICENSE](LICENSE)。

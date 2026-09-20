# redis-doctor

**一个用"故障注入 + 对照组"来回答"你到底准不准"的 Redis on K8s 诊断 Agent。**

16 类真实故障可以一键注入、一键恢复，每次诊断产出可追溯到具体工具调用的证据链；
写操作一律挂在人工审批闸门后面；评测给出 A/B/C/D 四组对照数据与失败样本归类。

被诊断对象是我自己的 `redis-operator`（`ops.example.com/v1alpha1` 的 `Redis` CR，
3 副本一主两从），不是玩具 Redis；`deploy/redis/demo-redis-cr.yaml` 就是它生成的集群。

> 演示视频（3 分钟）：`docs/demo-script.md` 是逐镜头脚本与录制命令。
> 终端实录见 [`docs/demo-trajectory.md`](docs/demo-trajectory.md)（由 `python rdctl.py demo` 真实输出）。
> 一次"失败的"实录见 [`docs/demo-failure-trajectory.md`](docs/demo-failure-trajectory.md)。

---

## 效果摘要

16 个故障场景 × 4 个对照组 × 3 次重复 = 192 次运行。同一台机器、同一套工具、
同一份手册，只改 Agent 的形态：

| 组 | 根因 Top-1 | 根因 Top-3 | 证据召回(已观察) | 证据召回(已引用) | 幻觉率 | 平均步数 | 平均工具调用 | 越权次数 |
|---|---|---|---|---|---|---|---|---|
| A 纯 LLM（只有告警文本） | 43.8% | 87.5% | 0.0% | 0.0% | 0.0% | 3.0 | 0 | 0 |
| B + 只读工具（单次 ReAct） | 81.2% | 100.0% | 81.2% | 75.0% | 0.0% | 5.0 | 6.6 | 0 |
| C + 知识库 RAG | 93.8% | 93.8% | 87.5% | 78.1% | 0.0% | 5.0 | 7.6 | 0 |
| **D + 假设验证循环（完整版）** | **100.0%** | **100.0%** | **100.0%** | 90.6% | 0.0% | 7.4 | 10.4 | **0** |

泛化（手册里**故意不写**的 4 类故障：网络阻断、磁盘写满、CPU 限流、探针错误）：

| 组 | 手册覆盖类别 Top-1 | held-out 类别 Top-1 |
|---|---|---|
| A | 41.7% | 50.0% |
| B | 83.3% | 75.0% |
| C | 100.0% | 75.0% |
| D | 100.0% | 100.0% |

稳健性（把完整版的工具调用预算从 30 压到 3）：

| 组 | Top-1 | 证据召回(已观察) | 平均工具调用 | 越权次数 |
|---|---|---|---|---|
| D 完整预算 | 100.0% | 100.0% | 10.4 | 0 |
| D* 预算收紧到 3 次调用 | 81.2% | 43.8% | 3.0 | 0 |

**这三张表要说明的是**：告警文本不够（A）；工具能到 81%（B）；手册把单次采集的选择变好、
但只在它覆盖的类别上有效（C）；真正带来 100% 的是"证据不够就继续查"的循环（D）；
把预算砍掉，分数就掉下来——**分数来自证据，不来自猜**。

> 诚实声明：以上数字来自 `RD_LLM_PROVIDER=reference`（确定性参考策略，非大模型），
> 目的是给出可复现的回归基线与只反映管线差异的对照。跑真实模型的命令见
> [评测方法](docs/evaluation.md)，`README` 不替代那一步。
> 完整结果 JSON：[`eval/results/reference-3x.json`](eval/results/reference-3x.json)、
> [`eval/results/budget-pressure.json`](eval/results/budget-pressure.json)。

---

## 快速开始

不需要集群、不需要 Docker、不需要 API key：

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"     # Windows: .venv\Scripts\pip install -e ".[dev]"

python rdctl.py kb build                              # 建手册索引
python rdctl.py scenarios list                        # 16 类故障
python rdctl.py demo --scenario S02                   # 注入 → 诊断 → 证据链 → 审批 → 验证
python rdctl.py eval --runs 3                         # 完整消融，约 1 秒
python rdctl.py serve --port 8099                     # 轨迹 UI: http://127.0.0.1:8099/ui
python -m pytest -q                                   # 单测（含安全负向测试）
```

等价 Makefile 目标：`make install / make demo / make eval / make test / make lint / make api`。

容器化演示（与宿主机既有容器完全隔离）：

```bash
docker compose -f deploy/compose/docker-compose.sandbox.yml up -d   # 只绑 127.0.0.1:8099
```

真实集群（k3s）：

```bash
sudo bash deploy/cluster/k3s-install.sh
kubectl apply -f deploy/redis/demo-redis-cr.yaml && kubectl apply -f deploy/redis/acl-init-job.yaml
kubectl apply -k deploy/overlays/server
python rdctl.py cluster check && python rdctl.py cluster rbac-check
```

---

## 架构

```
Alertmanager webhook ─┐
CLI (rdctl demo)  ────┼─▶ FastAPI 网关 ──▶ 诊断图 triage→plan→collect→evaluate→report
REST /diagnose    ────┘        │                        │
                               │                 受控工具层（只读白名单）
                               │        K8s API / Redis(ACL) / Prometheus / 手册检索
                               │                        │
                         轨迹存储(SQLite)         审批闸门 interrupt
                               │                        │
                               └──────────────▶ 执行(L1: delete pod) → 回查验证
```

诊断状态里有一条硬约束：**结论中的每条证据都必须指向某次真实工具调用的 `call_id`**。
评测据此计算"幻觉率"，而不是靠人工判官；`tests/test_agent_graph.py` 也把它固化成断言。

详细设计见 [docs/architecture.md](docs/architecture.md)。

---

## 设计决策（下面是 5 条要点，完整 7 条见 [docs/design-decisions.md](docs/design-decisions.md)）

1. **先做故障实验室，再做 Agent。** 没有标准答案就没有"准不准"。16 类故障先落成 YAML
   （注入/恢复/告警/标准根因/必须观察到的证据），Agent 第二个写。
   代价：新增故障要同时改 YAML 与 ops；收益：CI 能拦住"场景失去可观测性"这类回归。
2. **默认后端是沙箱，不是机器上的集群。** 这块开发机上跑着业务容器与既有的
   monitoring-stack，项目不能碰它们。沙箱用显式状态渲染出与真实集群同形态的可观测面，
   `RD_BACKEND=real` 才连真实 k3s。代价：沙箱信号干净，准确率偏乐观（写进已知限制）。
3. **证据信号来自工具层，不是模型的自述。** 工具返回时抽取机器可判定的信号
   （`pod_oom_killed`、`replica_link_down`…），场景声明必须观察到的信号。代价：新增故障要
   加一条抽取规则；收益：证据召回与幻觉率可自动计算。
4. **只有 L1 写操作可执行，且必须过审批闸门。** 诊断循环的工具目录里根本没有写工具；
   L1（delete pod）挂起等人批准，L2（改 CR/扩容）只给命令。代价：不能全自动修复，
   这是刻意的。
5. **手册不写 held-out 故障的答案。** 4 类故障完全不入库，`tests/test_kb.py` 会扫描关键词，
   一旦泄漏 CI 立刻失败。代价：C 组在 held-out 上必然吃亏（实测 75%）。

踩坑实录（写进设计决策附录）：`ToolRegistry.call(name, **kwargs)` 的第一参数最初叫 `name`，
与 `k8s_get_resource(kind, name)` 撞名，导致整条诊断在某些工具上静默返回 error。
教训：白名单/路由层的第一参数不要用常见字段名。

---

## 评测

### 故障清单（16 类，每类可一键注入/恢复）

| # | 故障 | 标准根因类别 | held-out |
|---|---|---|---|
| S01 | 主节点 Pod 被删除 | pod_restart | |
| S02 | 主节点 OOMKilled | oom_killed | |
| S03 | maxmemory 触顶且 noeviction | maxmemory_reached | |
| S04 | 副本与主断链（网络阻断） | network_partition | ✅ |
| S05 | PVC 一直 Pending | storage_provision | |
| S06 | 镜像拉取失败 | image_pull | |
| S07 | 密码 Secret 缺失/错误 | auth_failure | |
| S08 | Headless Service 缺失 | dns_resolution | |
| S09 | 慢查询拖垮延迟 | slow_query | |
| S10 | 连接数耗尽 | max_clients | |
| S11 | 数据盘写满 | disk_full | ✅ |
| S12 | 资源请求过大无法调度 | insufficient_resources | |
| S13 | CPU 限流导致抖动 | cpu_throttling | ✅ |
| S14 | repl-backlog 过小导致全量同步风暴 | replication_backlog | |
| S15 | 探针配置错误导致 NotReady | probe_misconfig | ✅ |
| S16 | 非法配置导致 CrashLoop | invalid_config | |

每个场景在 `faultlab/scenarios/S*.yaml` 里声明：`inject_sim/recover_sim`（沙箱）、
`inject_kubectl/recover_kubectl`（真实集群，标记 `kubectl_verified: false` 直到有人在
k3s 上跑通）、告警文本、标准根因、`required_signals`。

### 方法

- 每次运行前重置到干净状态，注入 → 采集 → 判分 → 恢复，并断言"恢复后干净"，
  避免上一次故障污染下一次；
- 指标：Top-1/Top-3（类别匹配）、证据召回（已观察/已引用两栏）、幻觉率（引用对不上）、
  平均步数/工具调用/耗时/token、越权次数；
- 每组每场景重复 3 次；结果 JSON 提交进仓库，任何数字都能回溯到某次运行。

### 失败案例（完整分析见 [docs/failure-cases.md](docs/failure-cases.md)）

| 类型 | 典型样本 | 根因 |
|---|---|---|
| 证据不足 | A 组几乎全部；B 组的 S08/S13/S14 | 探针集合没覆盖决定性证据 |
| 措辞误导 | A 组 S01/S03/S07/S11 | 告警文本把注意力引向错误的候选 |
| 引用不完整 | D 组少量信号被观察到但未写进结论（已引用 90.6%） | 报告聚合仍可改进 |
| 泛化缺口 | C 组 S13（held-out 的 CPU 限流） | 手册里没有这一类，RAG 无从帮忙 |

预算收紧实验（`max_tool_calls=3`）把 D 从 100% 打到 81.2%、证据召回 43.8%，
这是"分数来自证据采集"的反向证明。

---

## 安全模型

三层防线、彼此独立（详见 [docs/security.md](docs/security.md)）：

1. **身份层**：`doctor-reader` 只有 pods/logs/events/services/pvc/sts 的读权限，
   **没有 Secrets、没有 pods/exec、没有任何写动词**；`doctor-actuator` 只有 `delete pods`。
   `make rbac-check` 用 `kubectl auth can-i` 把这几条断言变成可执行证据。
2. **服务端层**：Redis 上用只读 ACL 用户 `doctor`（`-@all` + INFO/ROLE/DBSIZE/
   SLOWLOG GET/CLIENT LIST/CONFIG GET）。不依赖 `pods/exec`——exec 一旦给出就能执行任意
   命令，RBAC 无法约束命令内容。
3. **客户端层**：命令白名单（`FLUSHALL`、`CONFIG SET`、`KEYS`、`DEBUG`、`EVAL`、
   `SLAVEOF`… 一律拒绝）、结果脱敏（密码/Token/JWT/`stringData`）、超长截断（保留头尾并
   显式标注）、每次诊断的步数/调用数/时间预算。

写操作：诊断循环看不到写工具；L1 动作挂起为审批请求，人工在 `/approvals/{id}` 批准后
才由 actuator 执行，并立即回查 Pod 就绪数与该 Pod 的复制状态。

防提示注入：日志/事件/describe 内容在 prompt 中被标记为不可信数据，注入特征会被检测并
计入指标；执行层只接受**结构化动作**（动作类型 + 对象名），注入文本无法变成一次调用。

**验收指标：越权操作次数 = 0**（本次 192 次运行中为 0，见主表最后一列）。

---

## 仓库结构

```
agent/      诊断图：state / graph / nodes / reference(参考策略) / prompts / llm
tools/      受控工具层：safety(白名单·脱敏·截断) / k8s / redis / prom / kb / signals
sandbox/    进程内被诊断集群（渲染 Pod·事件·日志·INFO·指标）
faultlab/   16 个场景 YAML + 注入 ops + runner
eval/       harness / metrics / report，results/ 下提交真实结果
api/        FastAPI：webhook、REST、审批闸门、轨迹 UI、自身指标、SQLite 轨迹库
knowledge/  按现象分块的排障手册 + BM25 索引 + held-out 清单
deploy/     kustomize base/overlays、RBAC、NetworkPolicy、监控规则、compose、镜像
tests/      单测（安全负向测试、场景一致性、证据可追溯、API、KB 泄漏检查）
docs/       架构、设计决策、安全、评测、失败案例、已知限制、运维手册、演示脚本
```

---

## 已知限制与后续方向

完整清单见 [docs/limitations.md](docs/limitations.md)，最需要被质询的几条：

1. **提交的评测数字来自确定性参考策略，不是大模型。** 它给出可复现基线与管线差异；
   真实模型（DeepSeek/Qwen/OpenAI 兼容）要用 `RD_LLM_PROVIDER=openai_compat` 自己跑。
2. **沙箱信号比真实集群干净**，因此沙箱上的 100% 不代表真实集群上的准确率；
   `make eval-full` 是补这一步的入口，真实集群的复测尚未完成。
3. **kubectl 注入路径未在真实集群验证**（场景里标记为 `kubectl_verified: false`），
   `run_kubectl` 默认 dry-run。
4. **只覆盖 16 类单点故障**：级联故障、多故障并发、慢演进故障、Redis Cluster 模式不做。
5. **L2 只给建议不执行**；审批是单人单次，没有双人复核与变更窗口。
6. **幻觉率只是代理指标**：能发现"引用对不上"，发现不了"引用对得上但推理错了"。
7. **知识库是 BM25 而非向量检索**：同义改写召回弱；换向量库只需替换
   `tools/kb.py::KnowledgeBase`，接口不变。
8. **SQLite + 单副本**：轨迹库与沙箱都只能有一个写者，Agent 不能水平扩容。

后续方向：真实 k3s 上的全量复测（把 `kubectl_verified` 改成 true）、真实模型对照表、
把证据引用率从 90.6% 提到更高、多故障并发的归因顺序、以及给 redis-operator 加上
`spec.metrics.enabled` 的 exporter sidecar（补上它 README 里"未暴露指标"的限制）。

---

## 许可

MIT，见 [LICENSE](LICENSE)。

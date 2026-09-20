# 设计决策

按"问题 → 选择 → 代价 → 什么时候该改回去"写，和 redis-operator 第 4 章同一体裁。

## 1. 先做故障实验室，再做 Agent

**问题**：如果先写 Agent，没有标准答案，"这个 Agent 准不准"就无从谈起。

**选择**：16 类故障先落地成 YAML（注入、恢复、告警文本、标准根因、必须观察到的证据），
评测 harness 直接读这些文件。Agent 是第二个被写出来的东西。

**代价**：注入脚本和 Agent 都要维护；新增一类故障要同时改 YAML 和 ops。

**证据**：`tests/test_faultlab.py` 对 16 个场景逐个断言"注入后必须可观测、恢复后必须干净、
标准证据必须能被工具观察到"。任何一个场景失去可观测性，CI 立刻变红。

## 2. 默认后端是沙箱，而不是宿主机上的集群

**问题**：项目要能在任何一台机器上复现，但不能影响这台机器上已有的业务容器
（开发机上就跑着业务栈与一套 monitoring-stack）。

**选择**：默认 `RD_BACKEND=sandbox`，用一个显式状态对象渲染出与真实集群同形态的可观测面
（Pod 状态、事件、describe、日志、INFO、Prometheus 样本）。真实集群是 `RD_BACKEND=real`。

**代价**：沙箱的信号是"干净"的，因此沙箱上的准确率高于真实集群（见"已知限制"）。

**什么时候该改回去**：沙箱只用于 CI/演示/回归；任何对外宣称的效果都必须有真实 k3s 上的
复测结果，`make eval-full` 就是为这一步准备的。

## 3. 证据信号是工具层的输出，不是模型的自述

**问题**："模型说它看到了 OOMKilled"和"工具确实返回了 OOMKilled"是两件事。

**选择**：每个工具在返回时抽取一组机器可判定的信号（`pod_oom_killed`、
`replica_link_down`、`cpu_throttling`…），场景里声明必须观察到的信号，评测按信号算召回。

**代价**：新增一类故障往往要在 `tools/*.py` 里加一条抽取规则（一条正则或一次字段判断）。

**收益**：幻觉率、证据召回都能自动算，且不依赖人工判官打分。

## 4. 只有 L1 写操作可执行，而且必须经过审批闸门

**问题**：自动修复很吸引人，但"Agent 自己删 Pod"在生产里是不可接受的风险。

**选择**：写操作分两级；诊断循环拿到的工具目录里**根本看不到写工具**
（`catalog(allow_write=False)`）。L1（delete pod）在结论里生成动作 → 挂起为审批请求 →
人工批准后由 `doctor-actuator` 身份执行 → 立即回查 Pod 与复制链路。
L2（改 CR、扩容）只生成命令，不执行。

**代价**：端到端"自动恢复"要人参与一步，演示时必须说明这是刻意的。

**证据**：`tests/test_agent_graph.py::test_approval_gate_blocks_until_a_human_decides`
与 `tests/test_api.py::test_denied_approval_executes_nothing`。

## 5. 知识库不写 held-out 故障的答案

**问题**：如果手册里有全部 16 类故障的处置步骤，RAG 高分只能说明它背下了答案。

**选择**：手册覆盖 12 类 + 2 篇通用排障；S04（网络阻断）、S11（磁盘写满）、
S13（CPU 限流）、S15（探针错误）完全不写进手册，作为 held-out 对照。
`tests/test_kb.py` 会扫描手册，出现 held-out 的关键词就让 CI 失败。

**代价**：held-out 场景在 C 组必然吃亏，C 的数字看起来不如 D。

**收益**：能回答"RAG 到底提升了多少、在没见过的问题上有没有提升"——
本项目的实测是：C 在手册覆盖类别上 100%、held-out 上 75%；D 两组都是 100%。

## 6. 用编号 SQL 迁移，而不是 Alembic

**问题**：计划里写的是 Alembic。

**选择**：`api/store.py` 里用 40 行编号迁移（`schema_migrations` 表）。理由是这套 schema
没有 ORM、没有 autogenerate 场景，引入 Alembic 只增加一处需要同步的配置。

**代价**：没有 downgrade、没有 autogenerate；schema 复杂到需要它们时应立刻换成 Alembic。

**记录原因**：这是一个偏离计划的选择，写在这里以便评审时质询。

## 7. 轨迹 UI 用服务端渲染，而不是 Streamlit

**问题**：演示需要展示完整轨迹。

**选择**：FastAPI 直接渲染 `/ui`（列表 + 详情 + 审批页），零额外依赖、零额外端口。

**代价**：没有交互式筛选/图表；如果要做"面试现场随手切维度"的演示，Streamlit 更合适。

## 附：一个真实踩坑

`ToolRegistry.call(name, **kwargs)` 最初把第一个参数命名为 `name`。当工具本身有 `name`
参数时（`k8s_get_resource(kind, name)`），调用会抛
`got multiple values for argument 'name'`——一个只在特定工具上触发的静默路由错误，
表现为"整条诊断返回 error"。修法是把参数改名为 `tool_name`，并在 `tests/test_tools.py`
里覆盖带 `name` 参数的工具。教训：白名单/路由层的第一参数不要用常见字段名。


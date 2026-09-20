# 设计说明

记录几处实现取舍和真机上踩到的坑。每条给出选择、代价，以及什么时候应该改回去。

## 沙箱后端是默认值

`RD_BACKEND=sandbox` 用一个显式状态对象渲染 Pod 状态、事件、describe、日志、INFO 与指标样本，
故障注入只改这个对象。真实集群是 `RD_BACKEND=real`。

原因：CI、演示和评测都需要在没有集群的机器上跑；本项目的开发机上同时跑着业务容器。
代价：沙箱信号确定且干净，准确率高于真实集群。对外结论应以真实集群复测为准
（`docs/real-cluster.md`）。

## 证据信号由工具层产出

每个工具在返回时抽取一组机器可判定的信号（`pod_oom_killed`、`replica_link_down`…），
场景声明必须观察到的信号，评测按信号算召回与幻觉率。模型自述不算证据。

代价：新增一类故障通常要在 `tools/*.py` 增加一条抽取规则。

## 只有 L1 写操作可执行

诊断循环的工具目录里没有写工具。L1（delete pod）进入审批闸门，人工批准后由 actuator 身份执行，
随后回查 Pod 就绪数与复制链路；L2（改 CR、扩容）只生成命令。

## 手册不收录 held-out 故障

手册覆盖 12 类故障加 2 篇通用排障；网络阻断、磁盘写满、CPU 限流、探针错误完全不写入，
作为泛化对照。`tests/test_kb.py` 扫描关键词，出现即失败。代价是 C 组在 held-out 上必然吃亏。

## 用编号 SQL 迁移而不是 Alembic

`api/store.py` 用编号迁移脚本（`schema_migrations` 表）。当前 schema 没有 ORM 与 autogenerate
需求，引入 Alembic 只增加一处需要同步的配置。schema 复杂到需要 downgrade 时应换回 Alembic。

## 轨迹 UI 用服务端渲染

FastAPI 直接渲染 `/ui`（列表、详情、审批页），没有额外依赖和端口。需要交互式筛选时
更适合换成 Streamlit。

## 真机踩坑记录

1. **对象命名**：operator 生成的 StatefulSet 是 `redis-<name>`，Pod 是 `redis-<name>-<i>`，
   PVC 是 `data-redis-<name>-<i>`，而 Service 是 `<name>` 与 `<name>-headless`。
   沙箱最初按 `demo-0` 建模，接真机后统一改为 operator 的真实命名
   （`sandbox/cluster.py` 里的 `pod_name/sts_name/pvc_name`）。

2. **工具注册表的参数名**：`ToolRegistry.call(name, **kwargs)` 的第一参数最初叫 `name`，
   与 `k8s_get_resource(kind, name)` 冲突，报 `got multiple values for argument 'name'`，
   表现为整条诊断返回 error。改为 `tool_name`。

3. **陈旧事件**：Kubernetes 事件默认保留约 1 小时。一次已恢复的 ImagePullBackOff 事件让
   之后 15 分钟内的诊断都判成 image_pull。现在事件带 `ageSeconds`，超过 900 秒的事件
   不能作为故障证据（改为产生 `stale_event_evidence`）。

4. **版本回滚留下卡住的 Pod**：把 `spec.version` 改回正确标签后，处于 ImagePullBackOff
   的 Pod 不会被自动替换（StatefulSet 有序滚动在等它 Ready），恢复必须额外删除该 Pod。
   S06 的 `recover_kubectl` 已包含这一步。

5. **宿主机形态的 DNS**：agent 在集群外时，`redis-<name>-<i>.<name>-headless...` 不可解析，
   Pod IP 也不可达，需要 `kubectl port-forward`（`RD_REDIS_PORT_FORWARD_BASE`）。

6. **ACL 不落盘**：Redis 的 ACL 用户只存在内存中，operator 未提供 `aclfile`，
   因此 Pod 重启后 `doctor` 用户消失，必须重跑 ACL Job。这也是 S07 恢复步骤的一部分。

7. **Windows 上的命令执行**：场景里的 kubectl 命令使用 POSIX 引号（`-p '{"json"}'`），
   `shell=True` 在 Windows 会交给 cmd.exe，单引号不被剥离，导致所有带引号的注入命令失败。
   `faultlab/runner.run_shell` 现在优先使用可用的 POSIX shell，否则用 `shlex` 直接执行。


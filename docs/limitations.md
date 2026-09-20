# 已知限制

## 评测口径

提交的数字由 `RD_LLM_PROVIDER=reference`（确定性参考策略）产生，不是大模型结果。它的作用是
给出可复现的回归基线，并让 A/B/C/D 的差异只反映管线。真实模型结果需要自行运行，命令见
[evaluation.md](evaluation.md)。

沙箱后端的信号是确定且干净的，因此沙箱准确率高于真实集群；真实集群存在指标缺失、
日志截断与事件残留。

## 真实集群覆盖面

真机完整验证的场景只有 S01（Pod 被删）与 S06（镜像拉取失败）。其余场景的 kubectl 路径
标记为 `kubectl_verified: false`，逐条原因写在场景 YAML 的 `verification_note` 与
[real-cluster.md](real-cluster.md)：有的需要 Prometheus，有的需要 operator 补齐
`storageClassName` 或探针配置能力，有的需要写入压力或大容量卷。

## 功能范围

- 只覆盖 16 类单点故障，不含级联故障、多故障并发、慢演进故障与 Redis Cluster 模式。
- L2 只生成建议命令，不执行。
- 审批为单人单次，没有双人复核、变更窗口与自动回滚编排。
- 每个诊断的时间窗为 15 分钟（事件年龄过滤），更慢的故障可能证据不足。

## 指标

- 幻觉率是代理指标：只能发现引用对不上的断言，发现不了引用对得上但推理错误的断言。
- 引用率为 90.6%，仍有少量已观察到的信号没有写进结论文本。

## 实现

- 知识库为 BM25 检索，同义改写召回弱。`tools/kb.py::KnowledgeBase` 接口不变，可替换为
  向量检索。
- 轨迹库为 SQLite，Agent 单副本部署（`Recreate`），重启期间 API 不可用。
- 沙箱只模拟可观测面，不模拟 Kubernetes 调度、CNI 与存储供给。
- NetworkPolicy 不能按域名限制出站，当前放行 443。

## 环境

- 容器化 k3s 的 PVC 数据存在容器内，容器删除即丢失；长期环境应装在 Linux 宿主机上。
- Windows 上执行真机注入需要可用的 POSIX shell（`RD_SHELL` 可指定），否则带引号的
  kubectl 命令无法正确传参。


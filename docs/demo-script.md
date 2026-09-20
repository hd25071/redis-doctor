# 3 分钟演示脚本

录制：

```bash
asciinema rec docs/demo.cast -c "python rdctl.py demo --scenario S02"
```

| 时间 | 画面 | 讲解要点 |
|---|---|---|
| 0:00 | README 结果表 | 16 场景 × 4 组 × 3 次；A 43.8% → D 100%；越权 0 |
| 0:20 | `python rdctl.py scenarios list` | 16 类故障，每类有注入、恢复、标准根因与必须观察到的证据 |
| 0:40 | `python rdctl.py demo --scenario S02` | triage 只抽对象与症状；plan 给候选假设；每条证据带 `call_id`；`pod_restart` 假设被反驳 |
| 1:40 | 同一次输出中的审批闸门 | L1 动作挂起等人批准，L2 只给命令；批准后展示执行与回查结果 |
| 2:05 | `python rdctl.py eval --runs 3` | 越权 0；预算收紧到 3 次调用后 D 降到 81.2% |
| 2:30 | `/ui` 轨迹详情页 | 每次工具调用、返回片段、引用关系与审批可见 |
| 2:45 | README 与 docs/real-cluster.md | 真机验证范围与未验证场景的原因写在明面上 |

问答准备：

- 数字是否来自大模型：不是，来自确定性参考策略；真实模型命令在 `docs/evaluation.md`。
- 为什么不自动修复：只读默认开放，写操作必须审批；删除主节点 Pod 会导致全量同步。
- RAG 提升多少：手册覆盖类别 100%，held-out 75%；D 组两组都 100%。
- 如何证明不是背答案：held-out 类别、预算收紧实验，以及结论中每条证据必须指向真实 `call_id`。


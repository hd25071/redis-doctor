# 安全模型

目标：只读工具默认开放，写操作必须审批，评测中越权操作次数为 0。

## 三层防线

| 层 | 机制 | 位置 |
|---|---|---|
| 身份层 | `doctor-reader`：pods、pods/log、events、services、endpoints、pvc、statefulsets 的读，无 Secret、无 `pods/exec`、无写动词；`doctor-actuator`：仅 `delete pods` | `deploy/base/rbac.yaml` |
| 服务端层 | Redis 只读 ACL 用户 `doctor`：`-@all` 加 INFO / ROLE / DBSIZE / SLOWLOG GET / CLIENT LIST / CONFIG GET | `deploy/redis/acl-init-job.yaml` |
| 客户端层 | 命令白名单与工具分级，返回内容统一脱敏与截断，预算限制 | `tools/safety.py`、`tools/base.py` |

三层是与关系。`python rdctl.py cluster rbac-check` 把第一层变成可执行断言：

| 断言 | 期望 | 实测（k3s v1.30.4） |
|---|---|---|
| reader 不能删除 Pod | no | PASS |
| reader 不能读取 Secret | no | PASS |
| reader 不能 exec | no | PASS |
| reader 可以读取 Pod / 日志 | yes | PASS |
| actuator 可以删除 Pod | yes | PASS |
| actuator 不能读取 Secret | no | PASS |

第二层在真机上验证过：`doctor` 用户执行 `SET` / `FLUSHALL` 返回 NOPERM，`INFO` / `ROLE` /
`CONFIG GET` 正常。

## 写操作闸门

1. 诊断循环的工具目录只有只读工具，模型看不到写工具签名。
2. 结论中的 L1 动作生成 `ApprovalRequest`，状态置为 `waiting_approval`，不执行任何操作。
3. 人工在 `/approvals/{id}` 批准后，动作映射为结构化调用（动作类型 + 对象名），
   `plan_write_call()` 只接受 `write_l1` + `delete pod` + 形如 `redis-demo-0` 的目标。
4. 执行用 actuator 身份，随后回查 Pod 就绪数与复制链路，结果写入 `execution.verified`。

审批界面展示将要调用的 API 与参数，而不是模型对动作的描述。

## 防提示注入

- 工具返回内容在 prompt 中包在 `<untrusted_tool_output>` 内，声明为数据。
- `tools/safety.detect_injection` 检测注入特征，命中计入 `state.injection_hits` 与
  `redis_doctor_injection_hits_total`。
- 执行层只接受结构化动作，注入文本无法变成一次 kubectl 调用。
- 告警文本同样按不可信处理。

测试：`tests/test_safety.py` 覆盖白名单、拒绝表、脱敏、注入检测与截断。

## 预算与爆炸半径

| 限制 | 默认 | 超限行为 |
|---|---|---|
| 步数 | 12 | 停止采集，用已有证据出结论并标记 |
| 工具调用 | 30 | 同上 |
| 单次诊断耗时 | 180s | 同上 |
| 单次返回字符 | 4000-6000 | 保留头尾并标注 `[TRUNCATED ...]` |
| 告警去重 | 同 fingerprint 900s | 只计数，不再诊断 |
| 事件年龄 | 900s | 更早的事件不作为故障证据 |

## 凭据

密钥只放 Secret（`deploy/*/secret.example.yaml` 是模板）。工具输出离开进程前统一脱敏：
`requirepass` / `masterauth`、`password=...`、Bearer token、`sk-...`、JWT、`stringData`。
CI 里有 gitleaks，测试里有明文密钥扫描。

## 未覆盖的风险

- NetworkPolicy 不能按域名限制出站，当前放行 443。生产环境应加出口代理。
- reader 能看到 Pod 日志，日志中的业务数据不在脱敏范围内。
- 审批为单人单次，没有双人复核与变更窗口校验。
- `RD_BACKEND=sandbox` 不提供权限边界，它只用于让评测与 CI 不需要真实集群。
- 删除主节点 Pod 会导致副本全量同步并可能丢数据，因此定为 medium 风险并要求审批。


"""Typed state for one diagnosis.

The state is the contract between the graph nodes, the trace store and the
evaluation harness. Everything the report claims must be expressible here, which
is what makes "evidence must be traceable" checkable rather than aspirational.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

#: Root-cause taxonomy. The report may only use these values.
ROOT_CAUSES: tuple[str, ...] = (
    "pod_restart",
    "oom_killed",
    "maxmemory_reached",
    "network_partition",
    "storage_provision",
    "image_pull",
    "auth_failure",
    "dns_resolution",
    "slow_query",
    "max_clients",
    "disk_full",
    "insufficient_resources",
    "cpu_throttling",
    "replication_backlog",
    "probe_misconfig",
    "invalid_config",
)

ROOT_CAUSE_LABELS: dict[str, str] = {
    "pod_restart": "Pod 被删除/重启后重新调度（工作负载自愈但发生过中断）",
    "oom_killed": "容器内存超限被 OOMKilled",
    "maxmemory_reached": "Redis maxmemory 触顶且 noeviction，写入被拒",
    "network_partition": "主从之间网络被阻断",
    "storage_provision": "PVC 无法完成存储供给（Pending）",
    "image_pull": "镜像拉取失败",
    "auth_failure": "密码 Secret 缺失或错误导致认证失败",
    "dns_resolution": "Headless Service 缺失导致主节点域名无法解析",
    "slow_query": "慢查询拖垮延迟",
    "max_clients": "连接数耗尽（maxclients 打满）",
    "disk_full": "数据盘写满导致持久化失败",
    "insufficient_resources": "资源请求过大导致无法调度",
    "cpu_throttling": "CPU 限流过紧导致抖动",
    "replication_backlog": "repl-backlog 过小导致全量同步风暴",
    "probe_misconfig": "探针配置错误导致 NotReady（Redis 本身健康）",
    "invalid_config": "非法配置导致 CrashLoop",
}

HypothesisStatus = Literal["open", "supported", "refuted", "unresolved"]
RunStatus = Literal[
    "ok",
    "waiting_approval",
    "approved_executed",
    "denied",
    "error",
]


class Triage(BaseModel):
    """What the alert says — no conclusions allowed at this stage."""

    namespace: str = ""
    instance: str = ""
    pods: list[str] = Field(default_factory=list)
    symptom_class: str = ""
    time_window: str = ""
    keywords: list[str] = Field(default_factory=list)
    notes: str = ""


class Hypothesis(BaseModel):
    id: str
    category: str
    statement: str
    confidence: float = 0.2
    needs: list[str] = Field(default_factory=list)
    status: HypothesisStatus = "open"
    supporting: list[str] = Field(default_factory=list)
    contradicting: list[str] = Field(default_factory=list)

    def touch(self, status: HypothesisStatus) -> Hypothesis:
        self.status = status
        return self


class EvidenceRef(BaseModel):
    """One citation: which tool call showed what, and the exact quote."""

    call_id: str
    tool: str
    signal: str
    quote: str
    source: str = ""


class SuggestedAction(BaseModel):
    action: str
    tier: Literal["read", "write_l1", "write_l2"] = "read"
    target: str = ""
    risk: Literal["low", "medium", "high"] = "low"
    command: str = ""
    rationale: str = ""


class RuledOut(BaseModel):
    category: str
    reason: str
    evidence_call_ids: list[str] = Field(default_factory=list)


class DiagnosisReport(BaseModel):
    root_cause: str
    summary: str
    confidence: float
    evidence: list[EvidenceRef] = Field(default_factory=list)
    ruled_out: list[RuledOut] = Field(default_factory=list)
    suggested_actions: list[SuggestedAction] = Field(default_factory=list)
    uncertainty: list[str] = Field(default_factory=list)
    budget_exhausted: bool = False
    iterations: int = 0
    root_cause_label: str = ""


class ApprovalRequest(BaseModel):
    id: str
    action: SuggestedAction
    requested_at: float = Field(default_factory=time.time)
    status: Literal["pending", "approved", "denied"] = "pending"
    decided_by: str = ""
    decided_at: float | None = None
    note: str = ""


class ExecutionResult(BaseModel):
    action: SuggestedAction
    call_id: str
    ok: bool
    output: str = ""
    verified: bool = False
    verification: list[str] = Field(default_factory=list)


class DiagnosisState(BaseModel):
    diagnosis_id: str
    alert_text: str
    variant: str = "D"
    triage: Triage = Field(default_factory=Triage)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    observed_signals: list[str] = Field(default_factory=list)
    rounds: int = 0
    status: RunStatus = "ok"
    approval: ApprovalRequest | None = None
    execution: ExecutionResult | None = None
    report: DiagnosisReport | None = None
    audit: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    token_usage: dict[str, int] = Field(default_factory=dict)
    cost_usd: float = 0.0
    injection_hits: list[str] = Field(default_factory=list)
    notes: str = ""
    started_at: float = Field(default_factory=time.time)
    finished_at: float | None = None
    error: str = ""

    @property
    def elapsed_seconds(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    def record_signals(self, signals: set[str]) -> None:
        for signal in sorted(signals):
            if signal not in self.observed_signals:
                self.observed_signals.append(signal)

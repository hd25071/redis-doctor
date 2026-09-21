"""Deterministic reference reasoner.

This is the policy behind the committed evaluation table, ``make demo`` and CI.
It is deliberately *not* clever: it uses the same triage → plan → collect →
evaluate → report contract as the LLM reasoner, but replaces the model with an
explicit signal-to-hypothesis table.

Two reasons to keep it:

1. **Regression baseline.** If a change to the tools or the graph breaks
   evidence collection, the reference numbers move and CI fails.
2. **Honesty.** It makes the pipeline's contribution measurable independently of
   any model's variance; the README says plainly which rows came from which
   policy.

It is not an oracle: it can only conclude from signals that tools actually
returned, and only probes it decided to run.
"""

from __future__ import annotations

import re

from agent.state import (
    ROOT_CAUSE_LABELS,
    DiagnosisReport,
    DiagnosisState,
    EvidenceRef,
    Hypothesis,
    RuledOut,
    SuggestedAction,
    Triage,
)
from agent.variants import Variant
from tools.signals import evidence_signals

#: category -> which observed signals prove it (hard) or merely hint at it
#: (soft), which observations contradict it, and how specific the explanation
#: is. ``priority`` breaks ties between two explanations that are both
#: supported: an explicit fault (a pod was deleted, a config is invalid) beats
#: one inferred from a downstream symptom (the replication link is down).
CATEGORY_RULES: dict[str, dict[str, set[str]]] = {
    "pod_restart": {
        "hard": {"pod_recreated_recently"},
        "soft": {"pod_restart"},
        "contradicts": {"pod_oom_killed", "invalid_config_directive"},
        "priority": 2,
    },
    "oom_killed": {
        "hard": {"pod_oom_killed"},
        "soft": {"pod_restart", "pod_crashloop"},
        "contradicts": {"invalid_config_directive"},
        "priority": 2,
    },
    "maxmemory_reached": {
        "hard": {"maxmemory_reached_noeviction"},
        "soft": {"memory_high_usage", "latency_degraded"},
        "contradicts": set(),
        "priority": 1,
    },
    "network_partition": {
        "hard": {"replica_link_down"},
        "soft": {"latency_degraded"},
        "contradicts": {"dns_resolution_failure", "auth_failure"},
        "priority": 0,
    },
    "storage_provision": {
        "hard": {"pvc_pending"},
        "soft": {"storage_class_missing", "pod_pending_scheduling"},
        "contradicts": {"image_pull_error"},
        "priority": 2,
    },
    "image_pull": {
        "hard": {"image_pull_error"},
        "soft": {"pod_pending_scheduling"},
        "contradicts": set(),
        "priority": 2,
    },
    "auth_failure": {
        "hard": {"auth_failure"},
        "soft": {"pod_crashloop"},
        "contradicts": set(),
        "priority": 2,
    },
    "dns_resolution": {
        "hard": {"dns_resolution_failure"},
        "soft": {"headless_service_missing", "replica_link_down"},
        "contradicts": set(),
        "priority": 2,
    },
    "slow_query": {
        "hard": {"slow_query_detected"},
        "soft": {"latency_degraded"},
        "contradicts": {"cpu_throttling"},
        "priority": 0,
    },
    "max_clients": {
        "hard": {"max_clients_reached"},
        "soft": {"latency_degraded"},
        "contradicts": set(),
        "priority": 1,
    },
    "disk_full": {
        "hard": {"disk_full_write_error"},
        "soft": {"persistence_write_error", "disk_nearly_full"},
        "contradicts": set(),
        "priority": 1,
    },
    "insufficient_resources": {
        "hard": {"insufficient_resources"},
        "soft": {"pod_pending_scheduling"},
        "contradicts": set(),
        "priority": 2,
    },
    "cpu_throttling": {
        "hard": {"cpu_throttling"},
        "soft": {"latency_degraded", "pod_not_ready"},
        "contradicts": set(),
        "priority": 1,
    },
    "replication_backlog": {
        "hard": {"replication_full_resync_storm"},
        "soft": {"repl_backlog_small", "replica_link_down", "latency_degraded"},
        "contradicts": set(),
        "priority": 1,
    },
    "probe_misconfig": {
        "hard": {"readiness_probe_misconfig"},
        "soft": {"pod_not_ready"},
        "contradicts": set(),
        "priority": 2,
    },
    "invalid_config": {
        "hard": {"invalid_config_directive"},
        "soft": {"pod_crashloop"},
        "contradicts": {"pod_oom_killed"},
        "priority": 2,
    },
}

#: Quote extraction: signal -> regex whose matching line becomes the citation.
QUOTE_PATTERNS: dict[str, str] = {
    "pod_oom_killed": r"OOMKilled",
    "pod_recreated_recently": r"Killing|age=\d+s",
    "pod_restart": r"restarts=[1-9]",
    "pod_crashloop": r"CrashLoopBackOff|back-off restarting",
    "pod_not_ready": r"ready=False|Readiness probe failed",
    "pod_pending_scheduling": r"phase=Pending|Pending",
    "insufficient_resources": r"Insufficient (cpu|memory)",
    "image_pull_error": r"ImagePullBackOff|ErrImagePull|(pull|Pulling) image",
    "pvc_pending": r"Pending|waiting for a volume",
    "storage_class_missing": r"storageclass",
    "maxmemory_reached_noeviction": r"OOM command not allowed|noeviction|errorstat_OOM",
    "memory_high_usage": r"used_memory",
    "replica_link_down": r"master_link_status|Error condition on socket",
    "replication_full_resync_storm": r"Full resync|sync_full",
    "dns_resolution_failure": r"Name or service not known|no such host",
    "headless_service_missing": r"not found",
    "auth_failure": r"NOAUTH|invalid password|WRONGPASS",
    "slow_query_detected": r"duration_us=",
    "max_clients_reached": r"max number of clients|maxclients|rejected_connections",
    "disk_full_write_error": r"No space left on device|MISCONF",
    "disk_nearly_full": r"used_bytes_ratio",
    "persistence_write_error": r"rdb_last_bgsave_status:err|MISCONF",
    "cpu_throttling": r"throttled",
    "latency_degraded": r"duration_seconds",
    "readiness_probe_misconfig": r"Readiness:|readinessProbe",
    "invalid_config_directive": r"Bad directive|FATAL CONFIG FILE ERROR",
}

#: category -> remediation suggestions. L1 is executable after approval; L2 is
#: advisory only in this MVP.
ACTIONS: dict[str, list[tuple[str, str, str, str]]] = {
    # (action, tier, risk, command)
    "pod_restart": [
        (
            "确认工作负载已自愈并复查复制链路",
            "read",
            "low",
            "kubectl get pods -n demo -l app.kubernetes.io/instance=demo",
        ),
        ("如需强制重建主节点 Pod", "write_l1", "medium", "kubectl delete pod redis-demo-0 -n demo"),
    ],
    "oom_killed": [
        (
            "提高 redis 容器 memory limit 或降低 maxmemory",
            "write_l2",
            "high",
            'kubectl patch statefulset demo -n demo --type=json -p=\'[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"2Gi"}]\'',
        ),
        (
            "重建被 OOMKilled 的主节点 Pod 以恢复服务",
            "write_l1",
            "medium",
            "kubectl delete pod redis-demo-0 -n demo",
        ),
    ],
    "maxmemory_reached": [
        (
            "评估 maxmemory 与淘汰策略是否匹配业务写入量",
            "write_l2",
            "high",
            'kubectl patch redis demo -n demo --type=merge -p \'{"spec":{"config":{"maxmemory-policy":"allkeys-lru"}}}\'',
        ),
    ],
    "network_partition": [
        (
            "检查 NetworkPolicy 与主从链路连通性",
            "read",
            "low",
            "kubectl get networkpolicy -n demo -o yaml",
        ),
    ],
    "storage_provision": [
        (
            "修正 PVC storageClass 后重建工作负载",
            "write_l2",
            "high",
            'kubectl patch redis demo -n demo --type=merge -p \'{"spec":{"storageSize":"1Gi"}}\'',
        ),
    ],
    "image_pull": [
        (
            "确认 spec.version 对应的镜像在镜像仓库存在",
            "write_l2",
            "medium",
            'kubectl patch redis demo -n demo --type=merge -p \'{"spec":{"version":"7.2"}}\'',
        ),
    ],
    "auth_failure": [
        (
            "检查密码 Secret 是否存在且 password 键正确",
            "read",
            "low",
            "kubectl get secret redis-password -n demo -o jsonpath='{.data.password}' | wc -c",
        ),
        ("重建使用错误密码的 Pod", "write_l1", "medium", "kubectl delete pod redis-demo-1 -n demo"),
    ],
    "dns_resolution": [
        (
            "重建被删除的 Headless Service",
            "write_l1",
            "medium",
            "kubectl delete pod redis-demo-1 -n demo  # 触发 operator 重新调谐并补建 demo-headless",
        ),
    ],
    "slow_query": [
        (
            "定位并优化大 key / 高复杂度命令",
            "read",
            "low",
            "kubectl exec redis-demo-0 -n demo -- redis-cli SLOWLOG GET 10",
        ),
    ],
    "max_clients": [
        (
            "排查连接池泄漏并评估 maxclients",
            "write_l2",
            "medium",
            'kubectl patch redis demo -n demo --type=merge -p \'{"spec":{"config":{"maxclients":"20000"}}}\'',
        ),
    ],
    "disk_full": [
        (
            "扩容数据卷或清理无用数据",
            "write_l2",
            "high",
            'kubectl patch redis demo -n demo --type=merge -p \'{"spec":{"storageSize":"5Gi"}}\'',
        ),
    ],
    "insufficient_resources": [
        (
            "下调 requests 或扩容节点",
            "write_l2",
            "high",
            'kubectl patch redis demo -n demo --type=merge -p \'{"spec":{"resources":{"requests":{"memory":"512Mi"}}}}\'',
        ),
    ],
    "cpu_throttling": [
        (
            "评估 CPU limit 是否需要放宽",
            "write_l2",
            "medium",
            'kubectl patch redis demo -n demo --type=merge -p \'{"spec":{"resources":{"limits":{"cpu":"2"}}}}\'',
        ),
    ],
    "replication_backlog": [
        (
            "上调 repl-backlog-size，减少全量同步",
            "write_l2",
            "medium",
            'kubectl patch redis demo -n demo --type=merge -p \'{"spec":{"config":{"repl-backlog-size":"64mb"}}}\'',
        ),
    ],
    "probe_misconfig": [
        (
            "修正 readiness 探针端口为 6379",
            "write_l2",
            "medium",
            'kubectl patch redis demo -n demo --type=merge -p \'{"spec":{"probe":"fixed"}}\'',
        ),
    ],
    "invalid_config": [
        (
            "回滚非法 redis.conf 指令",
            "write_l2",
            "high",
            'kubectl patch redis demo -n demo --type=merge -p \'{"spec":{"config":{"appendonlyy":null}}}\'',
        ),
    ],
}

#: Full pod name, whatever prefix the operator uses (redis-demo-0).
TARGET_POD = re.compile(r"\b([a-z][a-z0-9-]{0,60}-\d+)\b")
INSTANCE = re.compile(r"\b(?:instance|实例|redis)[=: ](demo)\b", re.IGNORECASE)

#: Symptom keywords in the alert drive the first probe batch (variant B/C).
SYMPTOM_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("crashloopbackoff|crashloop|反复重启|重启|restart", "restart"),
    ("oom", "oom"),
    ("未就绪|notready|not ready|不可用", "not_ready"),
    ("pending|调度|schedul", "pending"),
    ("延迟|latency|慢|timeout|超时", "latency"),
    ("复制|replication|主从|link", "replication"),
    ("拒绝|refus|连接数|clients", "clients"),
    ("磁盘|disk|持久化|persistence|写入失败", "disk"),
    ("镜像|image", "image"),
    ("认证|auth|密码|password|noauth", "auth"),
)


class ReferenceReasoner:
    name = "reference-policy"
    is_llm = False

    def __init__(self, variant: Variant) -> None:
        self.variant = variant
        self.kb_categories: list[str] = []

    # -- triage ----------------------------------------------------------
    def triage(self, alert_text: str, namespace: str, instance: str) -> Triage:
        # Alerts mention both pods and PVCs (data-redis-demo-0); only pod names
        # belong in the target list.
        pattern = re.compile(rf"^(?:redis-)?{re.escape(instance)}-\d+$")
        pods = sorted({name for name in TARGET_POD.findall(alert_text) if pattern.match(name)})
        keywords = [
            label for pattern, label in SYMPTOM_KEYWORDS if re.search(pattern, alert_text, re.I)
        ]
        return Triage(
            namespace=namespace,
            instance=instance,
            pods=pods,
            symptom_class=",".join(keywords) or "unknown",
            time_window="alert window (5m)",
            keywords=keywords,
            notes="extracted from the alert text only; no conclusion drawn",
        )

    # -- plan ------------------------------------------------------------
    def plan(
        self,
        state: DiagnosisState,
        kb_categories: list[str],
        catalog: list[dict],
    ) -> list[Hypothesis]:
        candidates: list[str] = []
        self.kb_categories = [c for c in kb_categories if c in CATEGORY_RULES]
        for category in kb_categories:
            if category in CATEGORY_RULES and category not in candidates:
                candidates.append(category)
        for category in self._prior_from_keywords(state.triage.keywords):
            if category not in candidates:
                candidates.append(category)
        if not candidates:
            candidates = ["pod_restart", "oom_killed", "network_partition"]
        # Keep the plan small: the point of the loop is to prune, not to list.
        candidates = candidates[:5]
        return [
            Hypothesis(
                id=f"h{i + 1}",
                category=category,
                statement=ROOT_CAUSE_LABELS.get(category, category),
                confidence=round(1.0 / len(candidates), 2),
                needs=sorted(CATEGORY_RULES[category]["hard"] | CATEGORY_RULES[category]["soft"]),
            )
            for i, category in enumerate(candidates)
        ]

    @staticmethod
    def _prior_from_keywords(keywords: list[str]) -> list[str]:
        mapping = {
            "restart": ["oom_killed", "pod_restart", "invalid_config"],
            "oom": ["oom_killed", "maxmemory_reached"],
            "not_ready": ["probe_misconfig", "pod_restart"],
            "pending": ["storage_provision", "insufficient_resources", "image_pull"],
            "latency": ["slow_query", "cpu_throttling", "maxmemory_reached"],
            "replication": ["network_partition", "dns_resolution", "replication_backlog"],
            "clients": ["max_clients"],
            "disk": ["disk_full"],
            "image": ["image_pull"],
            "auth": ["auth_failure"],
        }
        out: list[str] = []
        for keyword in keywords:
            for category in mapping.get(keyword, []):
                if category not in out:
                    out.append(category)
        return out

    # -- collect ---------------------------------------------------------
    def choose_probes(
        self,
        state: DiagnosisState,
        catalog: list[dict],
        round_index: int,
        already: set[tuple[str, tuple[tuple[str, str], ...]]],
        max_probes: int,
    ) -> list[dict]:
        wanted: list[dict] = []
        triage = state.triage
        observed = set(state.observed_signals)
        instance = triage.instance or "demo"
        master = next((p for p in triage.pods if p.endswith("-0")), f"redis-{instance}-0")
        replicas = [f"redis-{instance}-{i}" for i in (1, 2)]

        if round_index == 0:
            # Every tool-using variant starts from the same cheap, high-yield
            # core, then spends the remaining probe budget on the symptom the
            # alert actually describes.
            core = [
                {"tool": "k8s_get_pods", "args": {"label_selector": ""}},
                {"tool": "k8s_events", "args": {"involved_object": ""}},
                {"tool": "redis_info", "args": {"pod": master, "section": "all"}},
            ]
            symptom_probes = _symptom_probes(master, replicas, instance)
            tail = [
                {"tool": "prom_query", "args": {"template": "redis_command_latency_p99_seconds"}},
                {"tool": "redis_slowlog", "args": {"pod": master, "count": 10}},
                {"tool": "k8s_logs", "args": {"pod": master, "tail": 80}},
            ]
            focused: list[dict] = []
            # The handbook is the prior: with RAG switched on, the first pass
            # spends its probe budget on the checks the handbook says matter
            # for the retrieved symptom, before the generic symptom probes.
            for category in self.kb_categories:
                focused += self._probes_for(category, master, replicas, instance, observed)
            for keyword in triage.keywords:
                focused += symptom_probes.get(keyword, [])
            return self._dedupe(core + focused + tail, already)[:max_probes]

        # Later rounds: how to widen within the symptom class, then probe what
        # still separates the open hypotheses.
        open_categories = [
            h.category for h in state.hypotheses if h.status in {"open", "unresolved"}
        ] or [h.category for h in state.hypotheses]
        table = _symptom_probes(master, replicas, instance)
        for keyword in triage.keywords:
            wanted += table.get(keyword, [])
        for category in open_categories:
            wanted += self._probes_for(category, master, replicas, instance, observed)
        wanted += [
            {"tool": "k8s_events", "args": {"involved_object": ""}},
            {"tool": "redis_info", "args": {"pod": master, "section": "persistence"}},
            {"tool": "redis_info", "args": {"pod": master, "section": "memory"}},
            {
                "tool": "k8s_get_resource",
                "args": {"kind": "service", "name": f"{instance}-headless"},
            },
        ]
        return self._dedupe(wanted, already)[:max_probes]

    @staticmethod
    def _probes_for(
        category: str, master: str, replicas: list[str], instance: str, observed: set[str]
    ) -> list[dict]:
        probes: dict[str, list[dict]] = {
            "pod_restart": [
                {"tool": "k8s_logs", "args": {"pod": master, "tail": 60}},
                {"tool": "k8s_events", "args": {"involved_object": master}},
            ],
            "oom_killed": [
                {"tool": "k8s_describe", "args": {"kind": "pod", "name": master}},
                {"tool": "k8s_logs", "args": {"pod": master, "tail": 80, "previous": True}},
            ],
            "maxmemory_reached": [
                {"tool": "redis_info", "args": {"pod": master, "section": "memory"}},
                {"tool": "redis_config_get", "args": {"pod": master, "param": "maxmemory-policy"}},
                {"tool": "prom_query", "args": {"template": "redis_errors_total", "error": "OOM"}},
            ],
            "network_partition": [
                {"tool": "redis_info", "args": {"pod": replicas[0], "section": "replication"}},
                {"tool": "k8s_logs", "args": {"pod": replicas[0], "tail": 80}},
            ],
            "storage_provision": [
                {"tool": "k8s_get_resource", "args": {"kind": "pvc", "name": f"data-{master}"}},
                {"tool": "k8s_describe", "args": {"kind": "pod", "name": master}},
            ],
            "image_pull": [
                {"tool": "k8s_describe", "args": {"kind": "pod", "name": master}},
                {"tool": "k8s_events", "args": {"involved_object": master}},
            ],
            "auth_failure": [
                {"tool": "k8s_logs", "args": {"pod": master, "tail": 120}},
                {"tool": "redis_info", "args": {"pod": master, "section": "server"}},
            ],
            "dns_resolution": [
                {
                    "tool": "k8s_get_resource",
                    "args": {"kind": "service", "name": f"{instance}-headless"},
                },
                {"tool": "k8s_logs", "args": {"pod": replicas[0], "tail": 80}},
            ],
            "slow_query": [
                {"tool": "redis_slowlog", "args": {"pod": master, "count": 16}},
                {"tool": "prom_query", "args": {"template": "redis_command_latency_p99_seconds"}},
            ],
            "max_clients": [
                {"tool": "redis_info", "args": {"pod": master, "section": "clients"}},
                {"tool": "prom_query", "args": {"template": "redis_rejected_connections"}},
            ],
            "disk_full": [
                {"tool": "prom_query", "args": {"template": "pvc_used_ratio"}},
                {"tool": "redis_info", "args": {"pod": master, "section": "persistence"}},
            ],
            "insufficient_resources": [
                {"tool": "k8s_describe", "args": {"kind": "pod", "name": master}},
                {"tool": "k8s_get_resource", "args": {"kind": "statefulset", "name": instance}},
                {"tool": "prom_query", "args": {"template": "node_pressure"}},
            ],
            "cpu_throttling": [
                {"tool": "prom_query", "args": {"template": "container_cpu_throttled_ratio"}},
                {"tool": "redis_slowlog", "args": {"pod": master, "count": 8}},
            ],
            "replication_backlog": [
                {"tool": "redis_config_get", "args": {"pod": master, "param": "repl-backlog-size"}},
                {"tool": "redis_info", "args": {"pod": replicas[0], "section": "stats"}},
                {"tool": "prom_query", "args": {"template": "redis_replication_sync_full"}},
                {"tool": "k8s_logs", "args": {"pod": replicas[0], "tail": 120}},
            ],
            "probe_misconfig": [
                {"tool": "k8s_get_resource", "args": {"kind": "statefulset", "name": instance}},
                {"tool": "k8s_describe", "args": {"kind": "pod", "name": master}},
                {"tool": "prom_query", "args": {"template": "pod_ready"}},
            ],
            "invalid_config": [
                {"tool": "k8s_logs", "args": {"pod": master, "tail": 120, "previous": True}},
                {
                    "tool": "k8s_get_resource",
                    "args": {"kind": "configmap", "name": f"{instance}-config"},
                },
            ],
        }
        return probes.get(category, [])

    @staticmethod
    def _dedupe(
        wanted: list[dict], already: set[tuple[str, tuple[tuple[str, str], ...]]]
    ) -> list[dict]:
        out: list[dict] = []
        seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
        for item in wanted:
            key = (item["tool"], tuple(sorted((k, str(v)) for k, v in item["args"].items())))
            if key in already or key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    # -- evaluate --------------------------------------------------------
    def evaluate(self, state: DiagnosisState) -> None:
        observed = evidence_signals(set(state.observed_signals))
        for hypothesis in state.hypotheses:
            rules = CATEGORY_RULES.get(hypothesis.category)
            if rules is None:
                hypothesis.status = "unresolved"
                continue
            hard = rules["hard"] & observed
            soft = rules["soft"] & observed
            contra = rules["contradicts"] & observed
            hypothesis.supporting = sorted(hard | soft)
            hypothesis.contradicting = sorted(contra)
            if contra and not hard:
                hypothesis.status = "refuted"
                hypothesis.confidence = 0.05
                continue
            if hard:
                hypothesis.status = "supported"
                hypothesis.confidence = round(
                    min(0.95, 0.6 + 0.18 * len(hard) + 0.06 * len(soft)), 2
                )
            elif soft:
                hypothesis.status = "unresolved"
                hypothesis.confidence = round(min(0.45, 0.2 + 0.1 * len(soft)), 2)
            else:
                hypothesis.status = "unresolved"
                hypothesis.confidence = 0.1

    def converged(self, state: DiagnosisState) -> bool:
        supported = [h for h in state.hypotheses if h.status == "supported"]
        if not supported:
            return False
        if len(supported) > 1:
            top = max(h.confidence for h in supported)
            return sum(1 for h in supported if h.confidence >= top - 0.05) == 1
        return True

    # -- report ----------------------------------------------------------
    def report(self, state: DiagnosisState, trace: list[dict]) -> DiagnosisReport:
        observed = evidence_signals(set(state.observed_signals))
        scored: list[tuple[float, int, str]] = []
        for category, rules in CATEGORY_RULES.items():
            hard = rules["hard"] & observed
            soft = rules["soft"] & observed
            contra = rules["contradicts"] & observed
            if not hard:
                continue
            score = 2.0 * len(hard) + 1.0 * len(soft) - 2.0 * len(contra)
            scored.append((score, int(rules.get("priority", 0)), category))
        scored.sort(key=lambda item: (-item[0], -item[1], item[2]))

        if not scored:
            return DiagnosisReport(
                root_cause=self._best_unresolved(state),
                summary="证据不足，无法收敛到单一根因；已按最可能的类别给出假设。",
                confidence=0.2,
                evidence=self._evidence(lines=None, categories=[]),
                ruled_out=self._ruled_out(state, observed, winner=None),
                suggested_actions=[
                    SuggestedAction(
                        action="扩大证据采集：补充 Prometheus 指标与上一次容器日志",
                        tier="read",
                        risk="low",
                        rationale="当前工具输出未覆盖任何硬证据信号",
                    )
                ],
                uncertainty=[
                    "没有任何假设获得硬证据支持",
                    f"已观察到的信号: {sorted(observed) or '无'}",
                ],
                budget_exhausted=state.budget.get("exhausted", False),
                iterations=state.rounds,
                root_cause_label=ROOT_CAUSE_LABELS.get(self._best_unresolved(state), ""),
            )

        best_score, _, winner = scored[0]
        top_evidence = CATEGORY_RULES[winner]["hard"] & observed
        evidence = self._evidence(trace, sorted(top_evidence))
        confidence = min(0.95, 0.55 + 0.15 * best_score / 2.0)
        if len(scored) > 1 and scored[1][0] >= best_score - 0.5:
            confidence = min(confidence, 0.7)
        return DiagnosisReport(
            root_cause=winner,
            summary=self._summary(winner, state, evidence),
            confidence=round(confidence, 2),
            evidence=evidence,
            ruled_out=self._ruled_out(state, observed, winner),
            suggested_actions=self._actions(winner),
            uncertainty=self._uncertainty(state, winner, observed),
            budget_exhausted=state.budget.get("exhausted", False),
            iterations=state.rounds,
            root_cause_label=ROOT_CAUSE_LABELS.get(winner, ""),
        )

    @staticmethod
    def _best_unresolved(state: DiagnosisState) -> str:
        ranked = sorted(state.hypotheses, key=lambda h: -h.confidence)
        return ranked[0].category if ranked else "pod_restart"

    def _evidence(self, lines: list[dict] | None, categories: list[str]) -> list[EvidenceRef]:
        if not lines or not categories:
            return []
        wanted = set(categories)
        refs: list[EvidenceRef] = []
        for entry in lines:
            hit = wanted & set(entry.get("signals", []))
            if not hit:
                continue
            output = entry.get("output") or entry.get("summary", "")
            for signal in sorted(hit):
                refs.append(
                    EvidenceRef(
                        call_id=entry["call_id"],
                        tool=entry["tool"],
                        signal=signal,
                        quote=_quote(signal, output),
                        source=_source_of(entry["tool"], entry.get("args", {})),
                    )
                )
        # One citation per (call, signal) keeps the evidence list readable.
        deduped: list[EvidenceRef] = []
        seen: set[tuple[str, str]] = set()
        for ref in refs:
            key = (ref.call_id, ref.signal)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(ref)
        return deduped[:8]

    def _ruled_out(
        self, state: DiagnosisState, observed: set[str], winner: str | None
    ) -> list[RuledOut]:
        rows: list[RuledOut] = []
        for hypothesis in state.hypotheses:
            if hypothesis.category == winner:
                continue
            rules = CATEGORY_RULES.get(hypothesis.category, {"hard": set()})
            missing = sorted(rules["hard"] - observed)
            if not missing:
                continue
            rows.append(
                RuledOut(
                    category=hypothesis.category,
                    reason="已检查但未观察到关键证据: " + ", ".join(missing),
                )
            )
        return rows[:6]

    @staticmethod
    def _summary(category: str, state: DiagnosisState, evidence: list[EvidenceRef]) -> str:
        label = ROOT_CAUSE_LABELS.get(category, category)
        tools = sorted({ref.tool for ref in evidence})
        return (
            f"根因判断为 {category}（{label}）。"
            f"依据来自 {len(evidence)} 条可追溯证据，工具: {', '.join(tools) or '无'}；"
            f"共执行 {state.rounds} 轮证据采集。"
        )

    @staticmethod
    def _actions(category: str) -> list[SuggestedAction]:
        out = []
        for action, tier, risk, command in ACTIONS.get(category, []):
            # Structured target: only delete-pod style L1 actions are
            # executable, and they must be expressed as verb + object, not as a
            # shell string the actuator would have to parse.
            verb, kind, name, namespace = "", "", "", ""
            match = re.search(r"\bkubectl delete pod ([a-z0-9][a-z0-9.-]*) -n (\w+)", command)
            if match and tier == "write_l1":
                verb, kind, name, namespace = "delete_pod", "pod", match.group(1), match.group(2)
            out.append(
                SuggestedAction(
                    action=action,
                    tier=tier,  # type: ignore[arg-type]
                    risk=risk,  # type: ignore[arg-type]
                    target=name or "demo",
                    verb=verb,
                    target_kind=kind,
                    target_name=name,
                    namespace=namespace,
                    command=command,
                    rationale="按根因类别给出的处置建议；写操作需人工审批",
                )
            )
        return out

    @staticmethod
    def _uncertainty(state: DiagnosisState, winner: str, observed: set[str]) -> list[str]:
        notes: list[str] = []
        soft_only = sorted(CATEGORY_RULES[winner]["soft"] & observed)
        if soft_only:
            notes.append("辅助但非决定性信号: " + ", ".join(soft_only))
        if state.budget.get("exhausted"):
            notes.append("预算耗尽，证据采集提前结束（结论基于已有证据）")
        unresolved = [h.category for h in state.hypotheses if h.status == "unresolved"]
        if unresolved:
            notes.append("未完全排除的候选: " + ", ".join(sorted(unresolved)))
        return notes


def _quote(signal: str, output: str) -> str:
    pattern = QUOTE_PATTERNS.get(signal)
    if pattern and output:
        for line in output.splitlines():
            if re.search(pattern, line, flags=re.IGNORECASE):
                return line.strip()[:300]
    first = next((line for line in output.splitlines() if line.strip()), "")
    return first.strip()[:300]


def _symptom_probes(master: str, replicas: list[str], instance: str) -> dict[str, list[dict]]:
    """Probes that fit each symptom class extracted from the alert.

    Used both for the first (single-pass) batch and as the widening sweep in
    later rounds, so a variant cannot "learn" a symptom class that another
    variant cannot look at.
    """
    return {
        "restart": [
            {"tool": "k8s_describe", "args": {"kind": "pod", "name": master}},
            {"tool": "k8s_logs", "args": {"pod": master, "tail": 80, "previous": True}},
        ],
        "oom": [
            {"tool": "redis_info", "args": {"pod": master, "section": "memory"}},
            {"tool": "prom_query", "args": {"template": "redis_memory_usage_ratio"}},
        ],
        "not_ready": [
            {"tool": "k8s_describe", "args": {"kind": "pod", "name": master}},
        ],
        "pending": [
            {"tool": "k8s_describe", "args": {"kind": "pod", "name": master}},
            {"tool": "k8s_get_resource", "args": {"kind": "statefulset", "name": instance}},
            {"tool": "k8s_get_resource", "args": {"kind": "pvc", "name": f"data-{master}"}},
        ],
        "latency": [
            {"tool": "prom_query", "args": {"template": "redis_command_latency_p99_seconds"}},
            {"tool": "redis_slowlog", "args": {"pod": master, "count": 10}},
            {"tool": "prom_query", "args": {"template": "container_cpu_throttled_ratio"}},
        ],
        "replication": [
            {"tool": "redis_info", "args": {"pod": replicas[0], "section": "replication"}},
            {"tool": "prom_query", "args": {"template": "redis_master_link_up"}},
            {"tool": "redis_config_get", "args": {"pod": master, "param": "repl-backlog-size"}},
        ],
        "clients": [
            {"tool": "redis_query", "args": {"pod": master, "command": "CLIENT LIST"}},
            {"tool": "prom_query", "args": {"template": "redis_rejected_connections"}},
        ],
        "disk": [
            {"tool": "prom_query", "args": {"template": "pvc_used_ratio"}},
            {"tool": "redis_info", "args": {"pod": master, "section": "persistence"}},
        ],
        "image": [
            {"tool": "k8s_describe", "args": {"kind": "pod", "name": master}},
        ],
        "auth": [
            {"tool": "k8s_logs", "args": {"pod": master, "tail": 120}},
            {"tool": "k8s_logs", "args": {"pod": replicas[0], "tail": 120}},
        ],
    }


def _source_of(tool: str, args: dict) -> str:
    if tool == "k8s_logs":
        return f"pod logs: {args.get('pod')}"
    if tool in {"k8s_get_resource", "k8s_describe"}:
        return f"kubectl {tool.split('_')[-1]} {args.get('kind')}/{args.get('name')}"
    if tool.startswith("redis_"):
        return f"redis-cli {tool} on {args.get('pod')}"
    if tool == "prom_query":
        return f"prometheus template {args.get('template')}"
    if tool == "k8s_get_pods":
        return "kubectl get pods"
    if tool == "k8s_events":
        return "kubectl get events"
    return tool

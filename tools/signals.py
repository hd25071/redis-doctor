"""Evidence signals.

An *evidence signal* is a machine-checkable observation that a tool actually
saw, e.g. ``pod_oom_killed`` or ``replica_link_down``. Signals are what makes
the evaluation objective:

* scenarios declare the signals that must be observed (``required_signals``);
* the report cites tool calls, and evidence recall is the fraction of required
  signals covered by the cited calls;
* a claim with no backing signal counts as unsupported (hallucination proxy).

Because extraction lives in the tools, it works identically for the sandbox
backend and for a real cluster.
"""

from __future__ import annotations

#: signal -> one-line human description (shown in reports and the UI)
SIGNAL_LABELS: dict[str, str] = {
    "pod_restart": "pod restarted recently (restartCount > 0)",
    "pod_recreated_recently": "pod object was recreated recently (new UID / young creation time)",
    "pod_oom_killed": "container last terminated with reason OOMKilled",
    "pod_crashloop": "container in CrashLoopBackOff",
    "pod_not_ready": "pod exists but readiness is false",
    "pod_pending_scheduling": "pod is Pending (unschedulable)",
    "insufficient_resources": "FailedScheduling: insufficient cpu/memory on nodes",
    "image_pull_error": "ImagePullBackOff / ErrImagePull",
    "pvc_pending": "PVC is not Bound",
    "storage_class_missing": "PVC references a storageClass that does not exist",
    "maxmemory_reached_noeviction": "maxmemory reached with noeviction: writes rejected",
    "memory_high_usage": "used memory is close to the configured limit",
    "replica_link_down": "replica reports master_link_status:down",
    "replication_full_resync_storm": "repeated full resyncs (sync_full growing)",
    "repl_backlog_small": "repl-backlog-size is at or below the default 1MB",
    "dns_resolution_failure": "log shows master hostname resolution failure",
    "headless_service_missing": "the StatefulSet's headless Service does not exist",
    "resource_missing": "the requested object does not exist",
    "stale_event_evidence": "an old event (>15m) is present that no longer reflects state",
    "auth_failure": "authentication error (NOAUTH / invalid password)",
    "slow_query_detected": "slowlog contains commands above the latency budget",
    "max_clients_reached": "connected clients at maxclients; new connections refused",
    "disk_full_write_error": "persistence fails: no space left on device",
    "disk_nearly_full": "filesystem usage is at or above the safe threshold",
    "persistence_write_error": "RDB/AOF write reports an error",
    "latency_degraded": "command latency p99 is above the service objective",
    "cpu_throttling": "container CPU is being throttled (cfs throttled ratio high)",
    "readiness_probe_misconfig": "readiness probe targets the wrong port/path; Redis itself healthy",
    "invalid_config_directive": "redis-server refused a directive (Bad directive or wrong number of arguments)",
    "master_role_confirmed": "Redis INFO says the pod is master",
    "replica_role_confirmed": "Redis INFO says the pod is a replica",
    "bgsave_ok": "a background save completed successfully (benign)",
    "node_pressure_false": "no node memory/disk pressure (benign)",
    "metrics_unavailable": "Prometheus query returned no series",
}

#: Signals that are deliberately benign: seeing them must not change a verdict.
BENIGN_SIGNALS: frozenset[str] = frozenset(
    {
        "bgsave_ok",
        "node_pressure_false",
        "master_role_confirmed",
        "replica_role_confirmed",
    }
)

#: Signals emitted by the knowledge base: retrieval is not evidence.
KNOWLEDGE_SIGNALS: frozenset[str] = frozenset({"kb_hit", "kb_miss"})


def label(signal: str) -> str:
    return SIGNAL_LABELS.get(signal, signal)


def evidence_signals(signals: set[str]) -> set[str]:
    """Observations that may be used as evidence in a conclusion."""
    return {s for s in signals if s not in KNOWLEDGE_SIGNALS}

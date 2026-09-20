"""Prometheus access with *templates only*.

Free-form PromQL is deliberately not exposed: an LLM that can write arbitrary
PromQL can (a) exfiltrate label values, (b) blow up the query engine, and
(c) produce conclusions that no fixed query can reproduce. Each template has a
sandbox implementation and the PromQL it maps to on a real cluster.
"""

from __future__ import annotations

from typing import Any, Protocol

from sandbox.cluster import SimCluster
from tools.base import ToolResult, ToolSpec

#: template name -> (description, params, PromQL used against a real cluster)
QUERY_TEMPLATES: dict[str, dict[str, Any]] = {
    "redis_memory_usage_ratio": {
        "description": "used_memory / maxmemory per pod",
        "params": [],
        "promql": "redis_memory_used_bytes / redis_config_maxmemory",
    },
    "redis_connected_clients": {
        "description": "connected clients per pod",
        "params": [],
        "promql": "redis_connected_clients",
    },
    "redis_maxclients": {
        "description": "configured maxclients per pod",
        "params": [],
        "promql": "redis_config_maxclients",
    },
    "redis_command_latency_p99_seconds": {
        "description": "p99 command latency in seconds",
        "params": [],
        "promql": 'redis_commands_duration_seconds{quantile="0.99"}',
    },
    "redis_rejected_connections": {
        "description": "counter of rejected connections",
        "params": [],
        "promql": "redis_rejected_connections_total",
    },
    "redis_errors_total": {
        "description": "redis error counters (OOM, MISCONF, ...)",
        "params": ["error"],
        "promql": "redis_errorstat_total",
    },
    "redis_replication_sync_full": {
        "description": "full resync counter per pod",
        "params": [],
        "promql": "redis_sync_full_total",
    },
    "redis_master_link_up": {
        "description": "1 when a replica's master link is up",
        "params": [],
        "promql": "redis_master_link_up",
    },
    "container_cpu_throttled_ratio": {
        "description": "fraction of CFS periods throttled per container",
        "params": [],
        "promql": (
            "rate(container_cpu_cfs_throttled_periods_total[5m]) / "
            "rate(container_cpu_cfs_periods_total[5m])"
        ),
    },
    "container_restart_count": {
        "description": "container restart counter",
        "params": [],
        "promql": "kube_pod_container_status_restarts_total",
    },
    "pod_ready": {
        "description": "1 when the container reports ready",
        "params": [],
        "promql": "kube_pod_container_status_ready",
    },
    "pvc_used_ratio": {
        "description": "PVC used bytes / capacity",
        "params": [],
        "promql": ("kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes"),
    },
    "node_pressure": {
        "description": "node pressure conditions",
        "params": [],
        "promql": 'kube_node_status_condition{status="true",condition=~".*Pressure"}',
    },
}


class PromBackend(Protocol):
    def query(self, template: str, **labels: str) -> list[dict]: ...


class SandboxPromBackend:
    def __init__(self, cluster: SimCluster) -> None:
        self.cluster = cluster

    def query(self, template: str, **labels: str) -> list[dict]:
        return self.cluster.prom_samples(template, **labels)


class RealPromBackend:
    def __init__(self, url: str, timeout: float = 5.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout

    def query(self, template: str, **labels: str) -> list[dict]:  # pragma: no cover
        import httpx

        spec = QUERY_TEMPLATES.get(template)
        if spec is None:
            raise KeyError(template)
        expression = spec["promql"]
        response = httpx.get(
            f"{self.url}/api/v1/query", params={"query": expression}, timeout=self.timeout
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            raise RuntimeError(f"prometheus error: {payload}")
        rows = []
        for item in payload["data"]["result"]:
            rows.append(
                {
                    "metric": item["metric"].get("__name__", template),
                    "labels": {k: v for k, v in item["metric"].items() if k != "__name__"},
                    "value": float(item["value"][1]),
                }
            )
        return rows


def signals_from_samples(template: str, rows: list[dict]) -> set[str]:
    found: set[str] = set()
    if not rows:
        found.add("metrics_unavailable")
        return found
    for row in rows:
        value = row.get("value")
        if not isinstance(value, (int, float)):
            continue
        if template == "redis_memory_usage_ratio" and value >= 0.99:
            found.add("maxmemory_reached_noeviction")
        elif template == "redis_memory_usage_ratio" and value >= 0.9:
            found.add("memory_high_usage")
        elif template == "redis_command_latency_p99_seconds" and value >= 0.05:
            found.add("latency_degraded")
        elif template == "redis_replication_sync_full" and value > 10:
            found.add("replication_full_resync_storm")
        elif template == "redis_master_link_up" and value < 1:
            found.add("replica_link_down")
        elif template == "container_cpu_throttled_ratio" and value >= 0.5:
            found.add("cpu_throttling")
        elif template == "container_restart_count" and value >= 1:
            found.add("pod_restart")
        elif template == "pod_ready" and value < 1:
            found.add("pod_not_ready")
        elif template == "pvc_used_ratio" and value >= 0.99:
            found.add("disk_full_write_error")
            found.add("disk_nearly_full")
        elif template == "pvc_used_ratio" and value >= 0.85:
            found.add("disk_nearly_full")
        elif template == "redis_rejected_connections" and value > 0:
            found.add("max_clients_reached")
        elif template == "node_pressure" and value >= 1:
            found.add("insufficient_resources")
        elif template == "redis_errors_total":
            error = str(row.get("labels", {}).get("error", "")).upper()
            if error == "OOM":
                found.add("maxmemory_reached_noeviction")
            if error == "MISCONF":
                found.add("persistence_write_error")
    if not found and template == "node_pressure":
        found.add("node_pressure_false")
    return found


def build_prom_tools(backend: PromBackend) -> list[ToolSpec]:
    def prom_query(template: str, error: str = "", pod: str = "") -> ToolResult:
        if template not in QUERY_TEMPLATES:
            return ToolResult(
                tool="prom_query",
                args={"template": template},
                ok=False,
                blocked=True,
                block_reason="only predefined query templates are allowed "
                f"({', '.join(sorted(QUERY_TEMPLATES))})",
                error="template not whitelisted",
            )
        extra: dict[str, str] = {}
        if error:
            extra["error"] = error
        if pod:
            extra["pod"] = pod
        try:
            rows = backend.query(template, **extra)
        except Exception as exc:
            return ToolResult(
                tool="prom_query",
                args={"template": template},
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                summary=f"prometheus query {template} failed",
                raw=f"# query {template} failed: {exc}",
            )
        signals = signals_from_samples(template, rows)
        lines = [
            f"{row['metric']}{{"
            + ",".join(f'{k}="{v}"' for k, v in row["labels"].items())
            + f"}} {row['value']}"
            for row in rows
        ] or ["(empty result: no series matched)"]
        return ToolResult(
            tool="prom_query",
            args={"template": template, **extra},
            ok=True,
            data=rows,
            summary=f"{template}: {len(rows)} series",
            raw="\n".join(lines),
            signals=signals,
        )

    return [
        ToolSpec(
            name="prom_query",
            tier="read",
            description="Run one predefined metric query template. Templates: "
            + ", ".join(sorted(QUERY_TEMPLATES))
            + ". Arbitrary PromQL is not available.",
            params={
                "template": "template name",
                "error": "error label filter (optional)",
                "pod": "pod filter (optional)",
            },
            fn=prom_query,
            max_result_chars=4000,
        )
    ]

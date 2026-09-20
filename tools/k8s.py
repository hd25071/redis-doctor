"""Kubernetes read tools (+ one gated L1 write) with signal extraction.

Two backends implement the same surface:

* :class:`SandboxK8sBackend` renders from :class:`sandbox.cluster.SimCluster`.
* :class:`RealK8sBackend` talks to a cluster through ``kubernetes``, using the
  read-only ServiceAccount described in ``deploy/base/rbac.yaml``.

Signal extraction lives here rather than in the backends so that a signal means
the same thing in both worlds.
"""

from __future__ import annotations

import re
from datetime import UTC
from typing import Any, Protocol

from sandbox.cluster import REDIS_PORT, SimCluster
from tools.base import ToolResult, ToolSpec

#: Kinds the agent may read. Secrets are absent on purpose: the RBAC Role does
#: not grant them, and neither does the tool layer.
READABLE_KINDS = {
    "pod": "pod",
    "pods": "pod",
    "statefulset": "statefulset",
    "statefulsets": "statefulset",
    "sts": "statefulset",
    "pvc": "pvc",
    "persistentvolumeclaim": "pvc",
    "persistentvolumeclaims": "pvc",
    "service": "service",
    "services": "service",
    "svc": "service",
    "configmap": "configmap",
    "cm": "configmap",
    "node": "node",
    "nodes": "node",
    "redis": "redis",
}


class K8sBackend(Protocol):
    def get_pods(self, namespace: str, label_selector: str | None = None) -> list[dict]: ...

    def get_resource(self, kind: str, name: str, namespace: str) -> str: ...

    def describe(self, kind: str, name: str, namespace: str) -> str: ...

    def events(self, namespace: str, involved: str | None = None) -> list[dict]: ...

    def logs(self, pod: str, namespace: str, tail: int, previous: bool) -> str: ...

    def delete_pod(self, pod: str, namespace: str) -> dict: ...


class SandboxK8sBackend:
    def __init__(self, cluster: SimCluster) -> None:
        self.cluster = cluster

    def get_pods(self, namespace: str, label_selector: str | None = None) -> list[dict]:
        return self.cluster.list_pods()

    def get_resource(self, kind: str, name: str, namespace: str) -> str:
        cluster = self.cluster
        if kind == "pod":
            return cluster.describe_pod(name)
        if kind == "statefulset":
            if name not in ("", cluster.instance, f"redis-{cluster.instance}"):
                raise KeyError(f"statefulset {name} not found")
            return cluster.describe_statefulset()
        if kind == "pvc":
            pvc = cluster.pvcs.get(name)
            if pvc is None:
                raise KeyError(f"pvc {name} not found")
            return cluster.describe_pvc(name)
        if kind == "service":
            svc = cluster.services.get(name)
            if svc is None:
                raise KeyError(f"service {name} not found")
            return (
                f"Name:       {svc['name']}\n"
                f"Namespace:  {namespace}\n"
                f"Type:       {svc['type']}\n"
                f"ClusterIP:  {svc['cluster_ip']}\n"
                "Ports:      "
                + ", ".join(f"{p['name']} {p['port']}/TCP" for p in svc["ports"])
                + "\n"
                "Selector:   " + ",".join(f"{k}={v}" for k, v in svc["selector"].items())
            )
        if kind == "configmap":
            conf = cluster.config_map_conf
            return (
                f"Name:        {cluster.config_map_extra.get('__name__', cluster.instance + '-config')}\n"
                f"Namespace:   {namespace}\n"
                "Data\n====\nredis.conf:\n----\n" + conf
            )
        if kind == "node":
            rows = []
            for node in cluster.nodes:
                rows.append(
                    f"Name: {node['name']}\n"
                    f"  Ready: {node['ready']}\n"
                    f"  MemoryPressure: {node['memory_pressure']}\n"
                    f"  DiskPressure: {node['disk_pressure']}\n"
                    f"  Allocatable: cpu={node['allocatable_cpu_milli']}m, "
                    f"memory={node['allocatable_memory_mi']}Mi"
                )
            return "\n".join(rows)
        if kind == "redis":
            return (
                f"apiVersion: ops.example.com/v1alpha1\n"
                f"kind: Redis\n"
                f"metadata:\n  name: {cluster.instance}\n  namespace: {namespace}\n"
                f"spec:\n  mode: replication\n  replicas: {cluster.statefulset['replicas']}\n"
                f'  version: "{cluster.statefulset.get("version", "7.2")}"\n'
                f"  storageSize: "
                f"{cluster.pvcs[f'data-redis-{cluster.instance}-0'].capacity}\n"
                f"status:\n  phase: "
                f"{'Running' if all(p.ready for p in cluster.pods.values()) else 'Pending'}\n"
                f"  readyReplicas: {sum(1 for p in cluster.pods.values() if p.ready)}\n"
                f"  masterPod: {cluster.master_pod().name}\n"
            )
        raise KeyError(f"unsupported kind {kind}")

    def describe(self, kind: str, name: str, namespace: str) -> str:
        if kind == "pod":
            return self.cluster.describe_pod(name)
        return self.get_resource(kind, name, namespace)

    def events(self, namespace: str, involved: str | None = None) -> list[dict]:
        return self.cluster.list_events(involved)

    def logs(self, pod: str, namespace: str, tail: int, previous: bool) -> str:
        return self.cluster.logs(pod, tail=tail, previous=previous)

    def delete_pod(self, pod: str, namespace: str) -> dict:
        state = self.cluster.pods[pod]
        state.recreated = True
        state.deleted = False
        state.uid = f"uid-{pod}-{len(self.cluster.statefulset.get('deleted_pods', [])) + 2:04d}"
        state.restarts = 0
        state.created_seconds_ago = 45
        state.last_termination_reason = None
        state.phase = "Running"
        state.ready = True
        self.cluster.statefulset.setdefault("deleted_pods", []).append(pod)
        self.cluster.add_event(
            "Pod",
            pod,
            "Killing",
            "Stopping container redis (delete requested by redis-doctor actuator)",
            type_="Normal",
            age_seconds=1,
        )
        self.cluster.add_event(
            "Pod",
            pod,
            "Started",
            f"Started container redis after recreation of {pod}",
            type_="Normal",
            age_seconds=1,
        )
        # A freshly recreated master comes back with an empty dataset: the
        # replicas have to resync, which is itself worth reporting.
        for replica in self.cluster.replicas():
            self.cluster.redis[replica.name].sync_full += 1
        return {"deleted": pod, "recreated": pod, "namespace": namespace}


class RealK8sBackend:
    """Read-mostly backend for a real cluster.

    ``kubernetes`` is imported lazily so the sandbox path needs no extra
    dependency. Configuration is either in-cluster (ServiceAccount) or from a
    kubeconfig pointed to by ``RD_KUBECONFIG``.
    """

    def __init__(self, kubeconfig: str | None = None) -> None:
        self._kubeconfig = kubeconfig
        self._core = None
        self._apps = None
        self._custom = None

    def _clients(self):  # pragma: no cover - requires a real cluster
        if self._core is None:
            from kubernetes import client, config

            if self._kubeconfig:
                config.load_kube_config(config_file=self._kubeconfig)
            else:
                try:
                    config.load_incluster_config()
                except Exception:
                    config.load_kube_config()
            self._core = client.CoreV1Api()
            self._apps = client.AppsV1Api()
            self._custom = client.CustomObjectsApi()
        return self._core, self._apps, self._custom

    def get_pods(self, namespace: str, label_selector: str | None = None) -> list[dict]:
        core, _, _ = self._clients()
        pods = core.list_namespaced_pod(namespace, label_selector=label_selector).items
        out = []
        for pod in pods:
            statuses = pod.status.container_statuses or []
            first = statuses[0] if statuses else None
            out.append(
                {
                    "name": pod.metadata.name,
                    "namespace": pod.metadata.namespace,
                    "phase": pod.status.phase,
                    "ready": bool(first.ready) if first else False,
                    "restartCount": first.restart_count if first else 0,
                    "node": pod.spec.node_name,
                    "image": first.image if first else "",
                    "containerState": _container_state(first),
                    "resources": _resources(pod),
                    "createdSecondsAgo": _age_seconds(pod.metadata.creation_timestamp),
                    "cpuUsageCores": None,
                    "memoryUsageBytes": None,
                    "readinessProbe": _probe(pod, "readiness"),
                    "livenessProbe": _probe(pod, "liveness"),
                }
            )
        return out

    def get_resource(self, kind: str, name: str, namespace: str) -> str:  # pragma: no cover
        import yaml

        core, apps, custom = self._clients()
        if kind == "pod":
            obj = core.read_namespaced_pod(name, namespace)
        elif kind == "statefulset":
            obj = apps.read_namespaced_stateful_set(name, namespace)
        elif kind == "pvc":
            obj = core.read_namespaced_persistent_volume_claim(name, namespace)
        elif kind == "service":
            obj = core.read_namespaced_service(name, namespace)
        elif kind == "configmap":
            obj = core.read_namespaced_config_map(name, namespace)
        elif kind == "node":
            obj = core.read_node(name)
        elif kind == "redis":
            return yaml.safe_dump(
                custom.get_namespaced_custom_object(
                    "ops.example.com", "v1alpha1", namespace, "redis", name
                ),
                allow_unicode=True,
                sort_keys=False,
            )
        else:
            raise KeyError(f"unsupported kind {kind}")
        return yaml.safe_dump(
            _to_dict(obj), allow_unicode=True, sort_keys=False, default_flow_style=False
        )

    def describe(self, kind: str, name: str, namespace: str) -> str:  # pragma: no cover
        # The Kubernetes API has no describe verb; the rendered YAML plus events
        # carries the same information for the agent.
        body = self.get_resource(kind, name, namespace)
        events = self.events(namespace, involved=name)
        rendered = "\n".join(
            f"  {e['type']}  {e['reason']}  {e['message']}  x{e['count']}" for e in events
        )
        return f"{body}\nEvents:\n{rendered}"

    def events(self, namespace: str, involved: str | None = None) -> list[dict]:  # pragma: no cover
        core, _, _ = self._clients()
        events = core.list_namespaced_event(namespace).items
        rows = []
        for event in events:
            if involved and involved not in (event.involved_object.name or ""):
                continue
            last = event.last_timestamp or event.event_time or event.metadata.creation_timestamp
            age_seconds = -1
            if last is not None:
                from datetime import datetime

                age_seconds = int((datetime.now(UTC) - last).total_seconds())
            rows.append(
                {
                    "type": event.type,
                    "reason": event.reason,
                    "object": f"{event.involved_object.kind}/{event.involved_object.name}",
                    "message": event.message,
                    "count": event.count or 1,
                    "age": f"{age_seconds}s" if age_seconds >= 0 else "unknown",
                    "ageSeconds": age_seconds,
                }
            )
        return rows

    def logs(self, pod: str, namespace: str, tail: int, previous: bool) -> str:  # pragma: no cover
        core, _, _ = self._clients()
        return core.read_namespaced_pod_log(
            pod, namespace, tail_lines=tail, previous=previous, timestamps=False
        )

    def delete_pod(self, pod: str, namespace: str) -> dict:  # pragma: no cover
        core, _, _ = self._clients()
        core.delete_namespaced_pod(pod, namespace)
        return {"deleted": pod, "namespace": namespace}


def _to_dict(obj: Any) -> Any:  # pragma: no cover - real backend helper
    from kubernetes.client import ApiClient

    return ApiClient().sanitize_for_serialization(obj)


def _container_state(status: Any) -> dict[str, Any]:  # pragma: no cover
    if status is None:
        return {}
    state: dict[str, Any] = {}
    if status.state:
        if status.state.waiting:
            state["state"] = {
                "waiting": {
                    "reason": status.state.waiting.reason,
                    "message": status.state.waiting.message,
                }
            }
        elif status.state.running:
            state["state"] = {"running": {"startedAt": str(status.state.running.started_at)}}
        elif status.state.terminated:
            state["state"] = {
                "terminated": {
                    "reason": status.state.terminated.reason,
                    "exitCode": status.state.terminated.exit_code,
                }
            }
    if status.last_state and status.last_state.terminated:
        state["lastState"] = {
            "terminated": {
                "reason": status.last_state.terminated.reason,
                "exitCode": status.last_state.terminated.exit_code,
                "message": status.last_state.terminated.message,
            }
        }
    return state


def _resources(pod: Any) -> dict[str, Any]:  # pragma: no cover
    for container in pod.spec.containers:
        if container.name == "redis":
            res = container.resources
            return {
                "requests": dict(res.requests or {}),
                "limits": dict(res.limits or {}),
            }
    return {}


def _age_seconds(created: Any) -> int:  # pragma: no cover
    from datetime import datetime

    if created is None:
        return -1
    return int((datetime.now(UTC) - created).total_seconds())


def _probe(pod: Any, kind: str) -> dict[str, Any]:  # pragma: no cover
    for container in pod.spec.containers:
        probe = container.readiness_probe if kind == "readiness" else container.liveness_probe
        if probe and probe.tcp_socket:
            return {"tcpSocket": {"port": probe.tcp_socket.port}}
    return {}


# ---------------------------------------------------------------------------
# Signal extraction (shared by both backends)
# ---------------------------------------------------------------------------

_LOG_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"OOM command not allowed when used memory", "maxmemory_reached_noeviction"),
    (
        r"Name or service not known|no such host|Temporary failure in name resolution",
        "dns_resolution_failure",
    ),
    (r"NOAUTH Authentication required|invalid password|WRONGPASS|AUTH failed", "auth_failure"),
    (r"No space left on device", "disk_full_write_error"),
    (r"MISCONF Redis is configured to save RDB snapshots", "disk_full_write_error"),
    (
        r"Bad directive or wrong number of arguments|FATAL CONFIG FILE ERROR",
        "invalid_config_directive",
    ),
    (r"Error condition on socket for SYNC|Unable to connect to MASTER", "replica_link_down"),
    (r"max number of clients reached", "max_clients_reached"),
    (r"Killing RDB/AOF child|Write error saving DB", "persistence_write_error"),
)


def signals_from_logs(text: str) -> set[str]:
    found: set[str] = set()
    for pattern, signal in _LOG_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            found.add(signal)
    full_resyncs = len(re.findall(r"Full resync from master", text))
    if full_resyncs >= 3:
        found.add("replication_full_resync_storm")
    if re.search(r"Readiness probe failed", text, flags=re.IGNORECASE):
        found.add("pod_not_ready")
    return found


def signals_from_pods(pods: list[dict]) -> set[str]:
    found: set[str] = set()
    for pod in pods:
        state = pod.get("containerState", {}) or {}
        waiting = (state.get("state", {}) or {}).get("waiting", {}) or {}
        terminated = (state.get("lastState", {}) or {}).get("terminated", {}) or {}
        if pod.get("restartCount", 0) and pod["restartCount"] > 0:
            found.add("pod_restart")
        reason = (terminated.get("reason") or "").lower()
        if reason == "oomkilled":
            found.add("pod_oom_killed")
        waiting_reason = (waiting.get("reason") or "").lower()
        if waiting_reason in {"crashloopbackoff", "error"}:
            found.add("pod_crashloop")
        if waiting_reason in {"imagepullbackoff", "errimagepull", "imageinspecterror"}:
            found.add("image_pull_error")
        if pod.get("phase") == "Pending":
            found.add("pod_pending_scheduling")
        if not pod.get("ready") and not waiting_reason:
            found.add("pod_not_ready")
    return found


def signals_from_resource(kind: str, name: str, text: str) -> set[str]:
    found: set[str] = set()
    if kind in {"statefulset", "pod"}:
        probes = re.findall(r"Readiness:\s+tcp-socket :(\d+)", text)
        if probes and any(int(port) != REDIS_PORT for port in probes):
            found.add("readiness_probe_misconfig")
        if re.search(r'"storageClassName":\s*"([^"]+)"', text):
            pass
    if kind == "pvc":
        if re.search(r"^Status:\s+(?!Bound)", text, flags=re.MULTILINE):
            found.add("pvc_pending")
        class_match = re.search(r"^StorageClass:\s+(\S+)", text, flags=re.MULTILINE)
        if class_match and class_match.group(1).startswith("fast-"):
            found.add("storage_class_missing")
    if re.search(r"memory_pressure:\s*true", text, flags=re.IGNORECASE):
        found.add("insufficient_resources")
    return found


#: Kubernetes keeps events for about an hour, so a fault that has already been
#: recovered still appears in the event list. Evidence has to be recent: an old
#: event may be reported, but it cannot justify a conclusion. Verified on a real
#: cluster — a recovered ImagePullBackOff from 20 minutes earlier made every
#: later diagnosis look like an image_pull fault.
EVENT_FRESHNESS_SECONDS = 900

_FAULT_EVENT_REASONS = {
    "oomkilling",
    "oomkilled",
    "failed",
    "failedscheduling",
    "unhealthy",
    "backoff",
    "provisioningfailed",
    "killing",
}


def _event_is_fresh(event: dict) -> bool:
    age = event.get("ageSeconds")
    if not isinstance(age, int) or age < 0:
        return True  # unknown age: never silently drop evidence
    return age <= EVENT_FRESHNESS_SECONDS


def signals_from_events(events: list[dict]) -> set[str]:
    found: set[str] = set()
    stale: set[str] = set()
    for event in events:
        reason = (event.get("reason") or "").lower()
        message = event.get("message") or ""
        if not _event_is_fresh(event):
            if reason in _FAULT_EVENT_REASONS:
                stale.add("stale_event_evidence")
            continue
        if reason in {"oomkilling", "oomkilled"} or "OOMKilled" in message:
            found.add("pod_oom_killed")
        if "Insufficient memory" in message or "Insufficient cpu" in message:
            found.add("insufficient_resources")
        if "Insufficient memory" in message or "Insufficient cpu" in message:
            found.add("pod_pending_scheduling")
        if reason in {"failed", "failedpulling"} and "pull image" in message.lower():
            found.add("image_pull_error")
        if "storageclass" in message.lower() and "not found" in message.lower():
            found.add("storage_class_missing")
            found.add("pvc_pending")
        if "no space left on device" in message.lower():
            found.add("disk_full_write_error")
        if "readiness probe failed" in message.lower():
            found.add("pod_not_ready")
            ports = re.findall(r":(\d{4,5})", message)
            if any(int(port) != REDIS_PORT for port in ports):
                found.add("readiness_probe_misconfig")
        if "no such host" in message.lower() or "name or service not known" in message.lower():
            found.add("dns_resolution_failure")
        if reason == "killing":
            found.add("pod_recreated_recently")
        if "back-off restarting failed container" in message.lower():
            found.add("pod_crashloop")
    return found | stale


def _missing_signals(kind: str, name: str) -> set[str]:
    """Signals implied by a 404: what the absence itself tells us."""
    signals = {"resource_missing"}
    if kind == "service" and "headless" in name:
        signals.add("headless_service_missing")
    if kind == "pvc":
        signals.add("pvc_pending")
    return signals


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def build_k8s_tools(backend: K8sBackend, namespace: str) -> list[ToolSpec]:
    def k8s_get_pods(label_selector: str = "", namespace_override: str = "") -> ToolResult:
        ns = namespace_override or namespace
        pods = backend.get_pods(ns, label_selector or None)
        signals = signals_from_pods(pods)
        lines = []
        for pod in pods:
            state = pod.get("containerState", {}) or {}
            waiting = (state.get("state", {}) or {}).get("waiting", {})
            last = (state.get("lastState", {}) or {}).get("terminated", {})
            detail = ""
            if waiting:
                detail = f" waiting={waiting.get('reason')}"
            if last:
                detail += f" lastTerminated={last.get('reason')} exit={last.get('exitCode')}"
            lines.append(
                f"{pod['name']:<10} phase={pod.get('phase'):<9} ready={pod.get('ready')} "
                f"restarts={pod.get('restartCount')} age={pod.get('createdSecondsAgo')}s"
                f"{detail}"
            )
        return ToolResult(
            tool="k8s_get_pods",
            args={"namespace": ns, "label_selector": label_selector},
            ok=True,
            data=pods,
            summary=f"{len(pods)} pods in namespace {ns}",
            raw="\n".join(lines),
            signals=signals,
        )

    def k8s_get_resource(kind: str, name: str = "") -> ToolResult:
        key = READABLE_KINDS.get(kind.strip().lower())
        if key is None:
            return ToolResult(
                tool="k8s_get_resource",
                args={"kind": kind, "name": name},
                ok=False,
                blocked=True,
                block_reason=f"kind '{kind}' is not readable by this tool "
                "(secrets and other kinds are outside the whitelist)",
                error="kind not whitelisted",
            )
        if key == "statefulset" and not name:
            name = "demo"
        try:
            text = backend.get_resource(key, name, namespace)
        except (KeyError, LookupError) as exc:
            return ToolResult(
                tool="k8s_get_resource",
                args={"kind": key, "name": name},
                ok=False,
                error=f"{key}/{name} not found: {exc}",
                summary=f"{key}/{name} not found",
                raw=f'(error) {key} "{name}" not found in namespace {namespace}',
                signals=_missing_signals(key, name),
            )
        signals = signals_from_resource(key, name, text)
        return ToolResult(
            tool="k8s_get_resource",
            args={"kind": key, "name": name},
            ok=True,
            data={"kind": key, "name": name},
            summary=f"{key}/{name}",
            raw=f"kind: {key}\nname: {name}\n{text}",
            signals=signals,
        )

    def k8s_describe(kind: str, name: str) -> ToolResult:
        key = READABLE_KINDS.get(kind.strip().lower())
        if key is None:
            return ToolResult(
                tool="k8s_describe",
                args={"kind": kind, "name": name},
                ok=False,
                blocked=True,
                block_reason=f"kind '{kind}' is not readable by this tool",
                error="kind not whitelisted",
            )
        try:
            text = backend.describe(key, name, namespace)
        except (KeyError, LookupError) as exc:
            return ToolResult(
                tool="k8s_describe",
                args={"kind": key, "name": name},
                ok=False,
                error=f"{key}/{name} not found: {exc}",
                summary=f"{key}/{name} not found",
                raw=f'(error) {key} "{name}" not found in namespace {namespace}',
                signals=_missing_signals(key, name),
            )
        signals = signals_from_resource(key, name, text) | signals_from_logs(text)
        if re.search(
            r"Name:\s+" + re.escape(name) + r"\b.*^Status:\s+Running",
            text,
            flags=re.DOTALL | re.MULTILINE,
        ):
            signals.discard("pod_not_ready")
        return ToolResult(
            tool="k8s_describe",
            args={"kind": key, "name": name},
            ok=True,
            data={"kind": key, "name": name},
            summary=f"describe {key}/{name}",
            raw=text,
            signals=signals,
        )

    def k8s_events(involved_object: str = "") -> ToolResult:
        events = backend.events(namespace, involved_object or None)
        signals = signals_from_events(events)
        lines = [
            f"{e['type']:<8} {e['reason']:<22} {e['object']:<28} x{e['count']} {e['message']}"
            for e in events
        ]
        return ToolResult(
            tool="k8s_events",
            args={"involved_object": involved_object},
            ok=True,
            data=events,
            summary=f"{len(events)} events",
            raw="\n".join(lines),
            signals=signals,
        )

    def k8s_logs(pod: str, tail: int = 120, previous: bool = False) -> ToolResult:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,62}", pod):
            return ToolResult(
                tool="k8s_logs",
                args={"pod": pod},
                ok=False,
                blocked=True,
                block_reason="pod name failed validation",
                error="invalid pod name",
            )
        text = backend.logs(pod, namespace, max(1, min(int(tail), 500)), bool(previous))
        signals = signals_from_logs(text)
        return ToolResult(
            tool="k8s_logs",
            args={"pod": pod, "tail": tail, "previous": previous},
            ok=True,
            data={"pod": pod, "previous": previous, "lines": len(text.splitlines())},
            summary=f"logs {pod}{' (previous container)' if previous else ''}",
            raw=text,
            signals=signals,
        )

    def k8s_delete_pod(pod: str) -> ToolResult:
        """L1 write: recreate a single pod. Only reachable with approval."""
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,62}", pod):
            return ToolResult(
                tool="k8s_delete_pod",
                args={"pod": pod},
                ok=False,
                error="invalid pod name",
            )
        out = backend.delete_pod(pod, namespace)
        return ToolResult(
            tool="k8s_delete_pod",
            args={"pod": pod},
            ok=True,
            data=out,
            summary=f"deleted pod {pod} (StatefulSet will recreate it)",
            raw=f"pod/{pod} deleted; StatefulSet/{namespace} will recreate it",
            signals={"pod_recreated_recently"},
        )

    return [
        ToolSpec(
            name="k8s_get_pods",
            tier="read",
            description="List Redis pods with phase, readiness, restart count and "
            "last termination reason.",
            params={"label_selector": "optional label selector (string)"},
            fn=k8s_get_pods,
            max_result_chars=4000,
        ),
        ToolSpec(
            name="k8s_get_resource",
            tier="read",
            description="Read one object as YAML/text: pod | statefulset | pvc | service | "
            "configmap | node | redis (the CR). Secrets are not readable.",
            params={
                "kind": "one of pod,statefulset,pvc,service,configmap,node,redis",
                "name": "object name",
            },
            fn=k8s_get_resource,
            max_result_chars=6000,
        ),
        ToolSpec(
            name="k8s_describe",
            tier="read",
            description="describe view of an object: status, conditions, resources, probes, events.",
            params={"kind": "object kind", "name": "object name"},
            fn=k8s_describe,
            max_result_chars=6000,
        ),
        ToolSpec(
            name="k8s_events",
            tier="read",
            description="Namespace events, optionally filtered by involved object name.",
            params={"involved_object": "filter, e.g. redis-demo-0 (optional)"},
            fn=k8s_events,
            max_result_chars=5000,
        ),
        ToolSpec(
            name="k8s_logs",
            tier="read",
            description="Pod logs. Use previous=true to read the log of the container "
            "instance that crashed.",
            params={
                "pod": "pod name",
                "tail": "lines to return (<=500)",
                "previous": "true to read the previous container instance",
            },
            fn=k8s_logs,
            max_result_chars=6000,
        ),
        ToolSpec(
            name="k8s_delete_pod",
            tier="write_l1",
            description="Delete a single pod so the StatefulSet recreates it. "
            "Requires human approval.",
            params={"pod": "pod name"},
            fn=k8s_delete_pod,
            max_result_chars=1000,
        ),
    ]

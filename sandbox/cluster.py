"""In-process model of ``namespace=demo`` running the redis-operator's cluster.

The model mirrors the real CRD (``ops.example.com/v1alpha1``):

    StatefulSet demo (3 pods)   -> pods demo-0 (master), demo-1, demo-2 (replicas)
    Service demo-headless       -> stable per-pod DNS: demo-<i>.demo-headless.demo...
    Service demo                -> client entry point
    ConfigMap demo-config       -> redis.conf used by every pod

Only the observable surfaces are rendered; there is no attempt to simulate
Kubernetes itself. That keeps the model small enough to be trusted, and the
evaluation honest about what is simulated and what is not.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

NAMESPACE = "demo"
INSTANCE = "demo"
REDIS_PORT = 6379
HEADLESS_SERVICE = f"{INSTANCE}-headless"
CONFIG_MAP = f"{INSTANCE}-config"
REDIS_IMAGE_REPO = "redis"
REDIS_VERSION = "7.2.5"
REPLICAS = 3

_BASE = datetime(2026, 9, 20, 9, 12, 3, 101000)


def _ts(offset_seconds: float) -> str:
    """Deterministic log timestamp: base time + offset, Redis log format."""
    moment = _BASE + timedelta(seconds=offset_seconds)
    return moment.strftime("%d %b %Y %H:%M:%S.") + f"{moment.microsecond // 1000:03d}"


def human_bytes(value: int) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            return f"{value:.2f}{unit}" if unit != "B" else f"{value}B"
        value /= 1024  # type: ignore[assignment]
    return f"{value}B"


@dataclass
class WallClock:
    """Monotonic-ish clock for the sandbox (kept out of wall-time assertions)."""

    seconds: float = 0.0

    def tick(self, seconds: float) -> float:
        self.seconds += seconds
        return self.seconds


@dataclass
class PodState:
    name: str
    role: str  # "master" | "replica"
    index: int
    phase: str = "Running"
    ready: bool = True
    restarts: int = 0
    created_seconds_ago: int = 5400
    node: str = "rd-node-1"
    image: str = f"{REDIS_IMAGE_REPO}:7.2"
    image_pull_error: str | None = None
    # container termination / waiting
    last_termination_reason: str | None = None
    last_termination_exit_code: int | None = None
    last_termination_message: str | None = None
    waiting_reason: str | None = None
    waiting_message: str | None = None
    # resources
    cpu_request: str = "100m"
    cpu_limit: str = "1"
    memory_request: str = "256Mi"
    memory_limit: str = "1Gi"
    cpu_usage_cores: float = 0.04
    cpu_throttled_ratio: float = 0.0
    memory_usage_bytes: int = 180 * 1024 * 1024
    # probes (rendered by StatefulSet, observed via describe/get)
    readiness_port: int = REDIS_PORT
    readiness_path: str | None = None
    liveness_port: int = REDIS_PORT
    # extra log lines appended by fault injection
    extra_logs: list[str] = field(default_factory=list)
    extra_previous_logs: list[str] = field(default_factory=list)
    # bookkeeping
    uid: str = ""
    deleted: bool = False
    recreated: bool = False
    annotations: dict[str, str] = field(default_factory=dict)

    @property
    def restart_count_recent(self) -> int:
        return self.restarts

    @property
    def promoted(self) -> bool:
        return self.role == "master"


@dataclass
class SlowlogEntry:
    entry_id: int
    timestamp: int
    duration_us: int
    command: str
    client_addr: str = "10.42.3.17:48720"
    client_name: str = ""


@dataclass
class RedisPodState:
    """Everything Redis itself reports for one pod."""

    pod: str
    role: str = "replica"
    link_status: str = "ok"
    master_host: str = f"{INSTANCE}-0.{HEADLESS_SERVICE}.{NAMESPACE}.svc.cluster.local"
    master_link_status: str = "up"
    master_sync_in_progress: int = 0
    master_last_io_seconds_ago: int = 1
    sync_full: int = 0
    sync_partial_ok: int = 0
    sync_partial_err: int = 0
    used_memory: int = 180 * 1024 * 1024
    used_memory_peak: int = 210 * 1024 * 1024
    maxmemory: int = 512 * 1024 * 1024
    maxmemory_policy: str = "noeviction"
    connected_clients: int = 3
    maxclients: int = 10000
    blocked_clients: int = 0
    rejected_connections: int = 0
    errorstat_oom: int = 0
    rdb_last_bgsave_status: str = "ok"
    rdb_last_bgsave_time_sec: int = 1
    rdb_changes_since_last_save: int = 0
    aof_enabled: int = 0
    aof_last_write_status: str = "ok"
    persistence_errors: int = 0
    loading: int = 0
    uptime_seconds: int = 5400
    expire_keys: int = 0
    keys: int = 1284
    slowlog: list[SlowlogEntry] = field(default_factory=list)
    slowlog_len: int = 0
    slowdown_threshold_us: int = 10000
    latency_p99_ms: float = 0.42
    config: dict[str, str] = field(default_factory=dict)
    requirepass: str = ""
    auth_broken: bool = False
    data_dir: str = "/data"
    disk_used_ratio: float = 0.31
    invalid_config_fatal: bool = False
    extra_logs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.config:
            self.config = {
                "maxmemory": str(self.maxmemory),
                "maxmemory-policy": self.maxmemory_policy,
                "maxclients": str(self.maxclients),
                "repl-backlog-size": str(1024 * 1024),
                "repl-timeout": "10",
                "timeout": "0",
                "tcp-keepalive": "60",
                "save": "900 1 300 10 60 10000",
                "appendonly": "no",
                "min-replicas-to-write": "1",
                "min-replicas-max-lag": "10",
                "dir": self.data_dir,
                "port": str(REDIS_PORT),
                "replica-read-only": "yes",
            }


@dataclass
class Event:
    kind: str  # Pod / StatefulSet / PersistentVolumeClaim / Service
    name: str
    reason: str
    message: str
    type: str = "Normal"
    count: int = 1
    age_seconds: int = 600

    def render(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "reason": self.reason,
            "object": f"{self.kind.lower()}/{self.name}",
            "message": self.message,
            "count": self.count,
            "age": f"{self.age_seconds // 60}m{self.age_seconds % 60}s",
        }


@dataclass
class PvcState:
    name: str
    phase: str = "Bound"
    storage_class: str = "local-path"
    capacity: str = "1Gi"
    used_ratio: float = 0.31
    message: str = ""


class SimCluster:
    """Deterministic, resettable model of the diagnosed Redis cluster."""

    def __init__(self, namespace: str = NAMESPACE, instance: str = INSTANCE) -> None:
        self.namespace = namespace
        self.instance = instance
        self.clock = WallClock()
        self.reset()

    # -- lifecycle -------------------------------------------------------
    def reset(self) -> None:
        self.pods: dict[str, PodState] = {}
        self.redis: dict[str, RedisPodState] = {}
        self.events: list[Event] = []
        self.pvcs: dict[str, PvcState] = {}
        self.services: dict[str, dict[str, Any]] = {}
        self.nodes: list[dict[str, Any]] = [
            {
                "name": "rd-node-1",
                "allocatable_cpu_milli": 3900,
                "allocatable_memory_mi": 7800,
                "ready": True,
                "memory_pressure": False,
                "disk_pressure": False,
            }
        ]
        self.config_map_conf: str = ""
        self.config_map_extra: dict[str, str] = {}
        self.secrets: dict[str, dict[str, str]] = {"redis-password": {"password": "s3cr3t-rd-demo"}}
        self.statefulset = {
            "name": self.instance,
            "replicas": REPLICAS,
            "ready_replicas": REPLICAS,
            "service_name": HEADLESS_SERVICE,
            "annotations": {},
            "deleted_pods": [],
        }
        self.alerts: list[str] = []
        self.notes: list[str] = []
        for index in range(REPLICAS):
            name = f"{self.instance}-{index}"
            role = "master" if index == 0 else "replica"
            self.pods[name] = PodState(
                name=name,
                role=role,
                index=index,
                uid=f"uid-{name}-0001",
            )
            self.redis[name] = RedisPodState(pod=name, role=role)
            if role == "replica":
                self.redis[name].master_link_status = "up"
            else:
                self.redis[name].link_status = "ok"
                self.redis[name].master_link_status = "up"
                self.redis[name].master_last_io_seconds_ago = 0
            self.pvcs[f"data-{name}"] = PvcState(name=f"data-{name}")
            self.events.extend(
                [
                    Event(
                        kind="Pod",
                        name=name,
                        reason="Scheduled",
                        message=f"Successfully assigned demo/{name} to rd-node-1",
                        age_seconds=5400 - index * 4,
                    ),
                    Event(
                        kind="Pod",
                        name=name,
                        reason="Pulled",
                        message=f'Container image "{REDIS_IMAGE_REPO}:7.2" already present on machine',
                        age_seconds=5398 - index * 4,
                    ),
                    Event(
                        kind="Pod",
                        name=name,
                        reason="Created",
                        message="Created container redis",
                        age_seconds=5396 - index * 4,
                    ),
                    Event(
                        kind="Pod",
                        name=name,
                        reason="Started",
                        message="Started container redis",
                        age_seconds=5394 - index * 4,
                    ),
                ]
            )
        self.services[self.instance] = {
            "name": self.instance,
            "type": "ClusterIP",
            "cluster_ip": "10.43.12.34",
            "ports": [{"name": "redis", "port": REDIS_PORT, "targetPort": REDIS_PORT}],
            "selector": {"app.kubernetes.io/instance": self.instance},
        }
        self.services[HEADLESS_SERVICE] = {
            "name": HEADLESS_SERVICE,
            "type": "ClusterIP",
            "cluster_ip": "None",
            "ports": [{"name": "redis", "port": REDIS_PORT, "targetPort": REDIS_PORT}],
            "selector": {"app.kubernetes.io/instance": self.instance},
        }
        self.config_map_conf = self.render_redis_conf()
        self.events.append(
            Event(
                kind="StatefulSet",
                name=self.instance,
                reason="SuccessfulCreate",
                message=f"create Pod {self.instance}-0 in StatefulSet {self.instance} successful",
                age_seconds=5400,
            )
        )

    # -- convenience accessors ------------------------------------------
    def pod(self, name: str) -> PodState:
        return self.pods[name]

    def redis_of(self, name: str) -> RedisPodState:
        return self.redis[name]

    def master_pod(self) -> PodState:
        for pod in self.pods.values():
            if pod.role == "master" and not pod.deleted:
                return pod
        return self.pods[f"{self.instance}-0"]

    def replicas(self) -> list[PodState]:
        return [p for p in self.pods.values() if p.role == "replica"]

    def add_event(
        self,
        kind: str,
        name: str,
        reason: str,
        message: str,
        type_: str = "Normal",
        age_seconds: int = 120,
        count: int = 1,
    ) -> None:
        self.events.append(
            Event(
                kind=kind,
                name=name,
                reason=reason,
                message=message,
                type=type_,
                count=count,
                age_seconds=age_seconds,
            )
        )

    def remove_service(self, name: str) -> None:
        self.services.pop(name, None)

    # -- renderers -------------------------------------------------------
    def render_redis_conf(self) -> str:
        base = [
            "# Generated by redis-operator (deterministic). Do not edit in place.",
            "bind 0.0.0.0",
            f"port {REDIS_PORT}",
            "protected-mode no",
            "appendonly no",
            "save 900 1",
            "save 300 10",
            "save 60 10000",
            "repl-diskless-sync yes",
            "repl-diskless-sync-delay 5",
            "tcp-keepalive 60",
            "maxmemory-policy noeviction",
            "dir /data",
        ]
        base.extend(f"{k} {v}" for k, v in sorted(self.config_map_extra.items()))
        return "\n".join(base) + "\n"

    def pod_json(self, pod: PodState) -> dict[str, Any]:
        ready = pod.ready and pod.waiting_reason is None and not pod.deleted
        if pod.deleted:
            return {"name": pod.name, "deleted": True}
        container_state: dict[str, Any] = {}
        if pod.waiting_reason:
            container_state = {
                "state": {
                    "waiting": {"reason": pod.waiting_reason, "message": pod.waiting_message or ""}
                }
            }
        else:
            container_state = {"state": {"running": {"startedAt": "2026-09-20T09:12:03Z"}}}
        if pod.last_termination_reason:
            container_state["lastState"] = {
                "terminated": {
                    "reason": pod.last_termination_reason,
                    "exitCode": pod.last_termination_exit_code
                    if pod.last_termination_exit_code is not None
                    else 137,
                    "message": pod.last_termination_message or "",
                }
            }
        return {
            "name": pod.name,
            "namespace": self.namespace,
            "phase": pod.phase,
            "ready": ready,
            "restartCount": pod.restarts,
            "node": pod.node,
            "image": pod.image,
            "createdSecondsAgo": pod.created_seconds_ago,
            "recreated": pod.recreated,
            "roleHint": pod.role,
            "containerState": container_state,
            "resources": {
                "requests": {"cpu": pod.cpu_request, "memory": pod.memory_request},
                "limits": {"cpu": pod.cpu_limit, "memory": pod.memory_limit},
            },
            "cpuUsageCores": round(pod.cpu_usage_cores, 3),
            "memoryUsageBytes": pod.memory_usage_bytes,
            "readinessProbe": {
                "tcpSocket": {"port": pod.readiness_port},
                "initialDelaySeconds": 3,
                "periodSeconds": 5,
            },
            "livenessProbe": {"tcpSocket": {"port": pod.liveness_port}, "periodSeconds": 10},
            "annotations": dict(pod.annotations),
        }

    def list_pods(self) -> list[dict[str, Any]]:
        return [
            self.pod_json(pod)
            for pod in sorted(self.pods.values(), key=lambda p: p.index)
            if not pod.deleted
        ]

    def describe_pod(self, name: str) -> str:
        pod = self.pods[name]
        info = self.redis[name]
        lines = [
            f"Name:             {pod.name}",
            f"Namespace:        {self.namespace}",
            f"Node:             {pod.node}",
            f"Start Time:       {_ts(-pod.created_seconds_ago)}",
            f"Labels:           app.kubernetes.io/instance={self.instance}",
            "                  app.kubernetes.io/name=redis",
            f"Status:           {pod.phase}",
            f"IP:               10.42.3.{10 + pod.index}",
            "Controlled By:    StatefulSet/" + self.instance,
            "Containers:",
            "  redis:",
            f"    Container ID:   containerd://{pod.uid}",
            f"    Image:          {pod.image}",
            f"    Port:           {REDIS_PORT}/TCP",
            f"    State:          {'Waiting' if pod.waiting_reason else 'Running'}",
        ]
        if pod.waiting_reason:
            lines.append(f"      Reason:       {pod.waiting_reason}")
            lines.append(f"      Message:      {pod.waiting_message or ''}")
        else:
            lines.append("      Started:      " + _ts(-pod.created_seconds_ago))
        lines += [
            f"    Last State:     {'Terminated' if pod.last_termination_reason else 'Running'}",
        ]
        if pod.last_termination_reason:
            lines += [
                f"      Reason:       {pod.last_termination_reason}",
                f"      Exit Code:    {pod.last_termination_exit_code or 137}",
                f"      Message:      {pod.last_termination_message or ''}",
            ]
        lines += [
            f"    Ready:          {'True' if pod.ready and not pod.waiting_reason else 'False'}",
            f"    Restart Count:  {pod.restarts}",
            "    Requests:",
            f"      cpu:      {pod.cpu_request}",
            f"      memory:   {pod.memory_request}",
            "    Limits:",
            f"      cpu:      {pod.cpu_limit}",
            f"      memory:   {pod.memory_limit}",
            f"    Readiness:      tcp-socket :{pod.readiness_port} delay=3s timeout=1s period=5s",
            f"    Liveness:       tcp-socket :{pod.liveness_port} delay=10s timeout=1s period=10s",
            "Conditions:",
            f"  Ready             {'True' if pod.ready and not pod.waiting_reason else 'False'}",
            f"  ContainersReady   {'True' if pod.ready and not pod.waiting_reason else 'False'}",
            "Volumes:",
            f"  data:  pvc/data-{pod.name} ({self.pvcs.get(f'data-{pod.name}').phase if self.pvcs.get(f'data-{pod.name}') else 'Bound'})",
            "Events:",
        ]
        for event in self.events_for_pod(name):
            lines.append(
                f"  {event['type']}  {event['reason']}  {event['message']}  x{event['count']}"
            )
        lines.append("")
        lines.append(
            "# Redis-side view (from the last successful probe): "
            f"role={info.role} link_status={info.link_status} "
            f"master_link_status={info.master_link_status}"
        )
        return "\n".join(lines)

    def describe_statefulset(self) -> str:
        sts = self.statefulset
        lines = [
            f"Name:               {sts['name']}",
            f"Namespace:          {self.namespace}",
            f"Selector:           app.kubernetes.io/instance={self.instance}",
            f"Replicas:           {sts['replicas']} desired | {sts['replicas']} total",
            f"Ready Replicas:     {sts['ready_replicas']}",
            f"Service Name:       {sts['service_name']}",
            "Pod Template:",
            "  Containers:",
            "   redis:",
            f"    Image:      redis:{sts.get('version', '7.2')}",
            f"    Port:       {REDIS_PORT}/TCP",
            f"    Readiness:  tcp-socket :{self.pods[f'{self.instance}-0'].readiness_port} "
            "delay=3s timeout=1s period=5s",
            f"    Liveness:   tcp-socket :{self.pods[f'{self.instance}-0'].liveness_port} "
            "delay=10s timeout=1s period=10s",
            "    Limits:",
            f"      cpu:      {self.pods[f'{self.instance}-0'].cpu_limit}",
            f"      memory:   {self.pods[f'{self.instance}-0'].memory_limit}",
            "    Mounts:",
            "      /etc/redis from config (rw)",
            "      /data from data (rw)",
            "Events:",
        ]
        for event in self.events:
            if event.kind == "StatefulSet":
                lines.append(f"  {event.type}  {event.reason}  {event.message}")
        return "\n".join(lines)

    def events_for_pod(self, name: str) -> list[dict[str, Any]]:
        return [e.render() for e in self.events if e.name == name]

    def list_events(self, involved: str | None = None) -> list[dict[str, Any]]:
        rows = []
        for event in self.events:
            if involved and involved not in event.name and involved not in event.kind.lower():
                continue
            rows.append(event.render())
        return sorted(rows, key=lambda r: (r["type"], r["reason"]))

    def describe_pvc(self, name: str) -> str:
        pvc = self.pvcs[name]
        lines = [
            f"Name:          {pvc.name}",
            f"Namespace:     {self.namespace}",
            f"Status:        {pvc.phase}",
            f"Capacity:      {pvc.capacity if pvc.phase == 'Bound' else ''}",
            f"StorageClass:  {pvc.storage_class}",
            f"Used:          {pvc.used_ratio * 100:.1f}% of {pvc.capacity}",
        ]
        if pvc.message:
            lines.append(f"Message:       {pvc.message}")
        return "\n".join(lines)

    def logs(self, pod_name: str, tail: int = 120, previous: bool = False) -> str:
        pod = self.pods[pod_name]
        info = self.redis[pod_name]
        role_flag = "M" if info.role == "master" else "S"

        if previous:
            lines = [
                f"1:{role_flag} {_ts(0)} * Redis version={REDIS_VERSION}, bits=64, commit=00000000, "
                "modified=0, pid=1, just started",
                f"1:{role_flag} {_ts(0.004)} * Configuration loaded",
                f"1:{role_flag} {_ts(0.021)} * monotonic clock: POSIX clock_gettime",
                f"1:{role_flag} {_ts(0.030)} * Running mode=standalone, port={REDIS_PORT}.",
                f"1:{role_flag} {_ts(0.038)} # WARNING: The TCP backlog setting of 511 cannot be "
                "enforced because /proc/sys/net/core/somaxconn is set to the lower value of 128.",
                f"1:{role_flag} {_ts(0.040)} # WARNING overcommit_memory is set to 0! Background save "
                "may fail under low memory condition.",
                f"1:{role_flag} {_ts(0.051)} * Ready to accept connections tcp",
            ]
            lines.extend(pod.extra_previous_logs)
            return self._tail(lines, tail)

        lines = [
            f"1:{role_flag} {_ts(0)} * Redis version={REDIS_VERSION}, bits=64, commit=00000000, "
            "modified=0, pid=1, just started",
            f"1:{role_flag} {_ts(0.004)} * Configuration loaded",
            f"1:{role_flag} {_ts(0.021)} * monotonic clock: POSIX clock_gettime",
            f"1:{role_flag} {_ts(0.030)} * Running mode=standalone, port={REDIS_PORT}.",
            f"1:{role_flag} {_ts(0.038)} # WARNING: The TCP backlog setting of 511 cannot be "
            "enforced because /proc/sys/net/core/somaxconn is set to the lower value of 128.",
            f"1:{role_flag} {_ts(0.040)} # WARNING overcommit_memory is set to 0! Background save "
            "may fail under low memory condition.",
        ]
        if info.role == "master":
            lines.append(f"1:{role_flag} {_ts(0.052)} * Ready to accept connections tcp")
            lines.append(
                f"1:{role_flag} {_ts(0.900)} * Replica {info.master_host.rsplit('.', 3)[0].replace('-0', '-1')}:"
                f"{REDIS_PORT} asks for synchronization"
            )
            lines.append(
                f"1:{role_flag} {_ts(0.930)} * Partial resynchronization request from "
                f"10.42.3.11:{REDIS_PORT} accepted. Sending 0 bytes of backlog starting from offset 0."
            )
        else:
            lines.append(
                f"1:{role_flag} {_ts(0.052)} * Connecting to MASTER {info.master_host}:{REDIS_PORT}"
            )
            lines.append(f"1:{role_flag} {_ts(0.055)} * MASTER <-> REPLICA sync started")
            lines.append(
                f"1:{role_flag} {_ts(0.059)} * Non blocking connect for SYNC fired the event."
            )
            lines.append(
                f"1:{role_flag} {_ts(0.101)} * Master replied to PING, replication can continue..."
            )
            lines.append(
                f"1:{role_flag} {_ts(0.150)} * Full resync from master: "
                "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678:0"
            )
            lines.append(
                f"1:{role_flag} {_ts(0.310)} * MASTER <-> REPLICA sync: Finished with success"
            )
            lines.append(f"1:{role_flag} {_ts(0.330)} * Ready to accept connections tcp")
        lines.append(f"1:{role_flag} {_ts(1800)} * 100 changes in 300 seconds. Saving...")
        lines.append(f"1:{role_flag} {_ts(1801.2)} * Background saving started by pid 42")
        lines.append(f"1:{role_flag} {_ts(1801.9)} * Background saving terminated with success")
        lines.extend(info.extra_logs)
        lines.extend(pod.extra_logs)
        return self._tail(lines, tail)

    @staticmethod
    def _tail(lines: list[str], tail: int) -> str:
        if tail > 0:
            lines = lines[-tail:]
        return "\n".join(lines)

    def info_text(self, pod_name: str, section: str | None = None) -> str:
        info = self.redis[pod_name]
        blocks: dict[str, list[str]] = {
            "server": [
                f"redis_version:{REDIS_VERSION}",
                "redis_git_sha1:00000000",
                "redis_mode:standalone",
                "os:Linux 6.8.0 x86_64",
                "arch_bits:64",
                "run_id:a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
                f"tcp_port:{REDIS_PORT}",
                f"uptime_in_seconds:{info.uptime_seconds}",
                f"uptime_in_days:{info.uptime_seconds // 86400}",
                "config_file:/etc/redis/redis.conf",
                "executable:/usr/local/bin/redis-server",
            ],
            "clients": [
                f"connected_clients:{info.connected_clients}",
                "cluster_connections:0",
                f"maxclients:{info.maxclients}",
                f"blocked_clients:{info.blocked_clients}",
                "tracking_clients:0",
                f"rejected_connections:{info.rejected_connections}",
            ],
            "memory": [
                f"used_memory:{info.used_memory}",
                f"used_memory_human:{human_bytes(info.used_memory)}",
                f"used_memory_peak:{info.used_memory_peak}",
                f"used_memory_peak_human:{human_bytes(info.used_memory_peak)}",
                f"maxmemory:{info.maxmemory}",
                f"maxmemory_human:{human_bytes(info.maxmemory)}",
                f"maxmemory_policy:{info.maxmemory_policy}",
                "mem_fragmentation_ratio:1.08",
                "mem_allocator:jemalloc-5.3.0",
            ],
            "persistence": [
                f"loading:{info.loading}",
                f"rdb_changes_since_last_save:{info.rdb_changes_since_last_save}",
                "rdb_bgsave_in_progress:0",
                f"rdb_last_bgsave_status:{info.rdb_last_bgsave_status}",
                f"rdb_last_bgsave_time_sec:{info.rdb_last_bgsave_time_sec}",
                "rdb_last_cow_size:1048576",
                f"aof_enabled:{info.aof_enabled}",
                f"aof_last_write_status:{info.aof_last_write_status}",
                "aof_last_bgrewrite_status:ok",
            ],
            "stats": [
                "total_connections_received:1841",
                "total_commands_processed:98442",
                "instantaneous_ops_per_sec:12",
                "total_net_input_bytes:18765432",
                "total_net_output_bytes:98765432",
                f"rejected_connections:{info.rejected_connections}",
                f"sync_full:{info.sync_full}",
                f"sync_partial_ok:{info.sync_partial_ok}",
                f"sync_partial_err:{info.sync_partial_err}",
                "expired_keys:0",
                "evicted_keys:0",
                f"errorstat_OOM:count={info.errorstat_oom}",
                f"errorstat_MISCONF:count={info.persistence_errors}",
                "keyspace_hits:51234",
                "keyspace_misses:412",
                "latest_fork_usec:812",
            ],
            "replication": [
                f"role:{info.role}",
            ],
            "cpu": [
                "used_cpu_sys:42.18",
                "used_cpu_user:31.77",
            ],
            "keyspace": [
                f"db0:keys={info.keys},expires={info.expire_keys},avg_ttl=0",
            ],
        }
        if info.role == "master":
            blocks["replication"] += [
                "connected_slaves:2",
                f"slave0:ip=10.42.3.11,port={REDIS_PORT},state=online,offset=88421,lag=0",
                f"slave1:ip=10.42.3.12,port={REDIS_PORT},state=online,offset=88421,lag=1",
                "master_failover_state:no-failover",
                "master_replid:a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
                "master_replid2:0000000000000000000000000000000000000000",
                "master_repl_offset:88421",
                "second_repl_offset:-1",
                f"repl_backlog_active:{1 if info.config.get('repl-backlog-size', '0') != '0' else 0}",
                f"repl_backlog_size:{info.config.get('repl-backlog-size', '0')}",
                "repl_backlog_first_byte_offset:0",
                "repl_backlog_histlen:12048",
            ]
        else:
            blocks["replication"] += [
                f"master_host:{info.master_host}",
                f"master_port:{REDIS_PORT}",
                f"master_link_status:{info.master_link_status}",
                f"master_last_io_seconds_ago:{info.master_last_io_seconds_ago}",
                f"master_sync_in_progress:{info.master_sync_in_progress}",
                "slave_read_repl_offset:88421",
                "slave_repl_offset:88421",
                "slave_priority:100",
                "slave_read_only:1",
                "replica_announced:1",
                "connected_slaves:0",
                "master_failover_state:no-failover",
                "master_replid:a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
                "master_repl_offset:88421",
            ]
        if info.auth_broken:
            blocks["replication"] = [f"role:{info.role}"]

        if section:
            key = section.strip().lower()
            if key not in blocks:
                return f"# INFO section '{section}' not available"
            return "# " + key.capitalize() + "\r\n" + "\r\n".join(blocks[key])
        out: list[str] = []
        for name, body in blocks.items():
            out.append(f"# {name.capitalize()}")
            out.extend(body)
            out.append("")
        return "\r\n".join(out)

    def slowlog_entries(self, pod_name: str, count: int = 10) -> list[SlowlogEntry]:
        entries = sorted(self.redis[pod_name].slowlog, key=lambda e: e.entry_id, reverse=True)
        return entries[:count]

    def role_text(self, pod_name: str) -> str:
        info = self.redis[pod_name]
        if info.role == "master":
            return "master\n88421\n10.42.3.11 6379 88421 connected\n10.42.3.12 6379 88421 connected"
        return (
            f"slave\n{info.master_host}\n6379\n"
            f"{'connected' if info.master_link_status == 'up' else 'connect'}\n88421"
        )

    def config_get(self, pod_name: str, param: str) -> list[str]:
        info = self.redis[pod_name]
        keys = param.split()
        if not keys:
            keys = sorted(info.config)
        values: list[str] = []
        for key in keys:
            value = info.config.get(key, "")
            values.extend([key, value])
        return values

    def dbsize(self, pod_name: str) -> int:
        return self.redis[pod_name].keys

    # -- Prometheus surface ---------------------------------------------
    def prom_samples(self, template: str, **labels: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        pods = sorted(self.pods.values(), key=lambda p: p.index)
        if template == "redis_memory_usage_ratio":
            for pod in pods:
                info = self.redis[pod.name]
                rows.append(
                    {
                        "metric": "redis_memory_used_ratio",
                        "labels": {"pod": pod.name, "instance": self.instance},
                        "value": round(info.used_memory / max(info.maxmemory, 1), 4),
                    }
                )
        elif template == "redis_connected_clients":
            for pod in pods:
                info = self.redis[pod.name]
                rows.append(
                    {
                        "metric": "redis_connected_clients",
                        "labels": {"pod": pod.name, "instance": self.instance},
                        "value": info.connected_clients,
                    }
                )
        elif template == "redis_maxclients":
            for pod in pods:
                rows.append(
                    {
                        "metric": "redis_config_maxclients",
                        "labels": {"pod": pod.name},
                        "value": self.redis[pod.name].maxclients,
                    }
                )
        elif template == "redis_command_latency_p99_seconds":
            for pod in pods:
                rows.append(
                    {
                        "metric": "redis_commands_duration_seconds",
                        "labels": {"pod": pod.name, "quantile": "0.99"},
                        "value": round(self.redis[pod.name].latency_p99_ms / 1000.0, 5),
                    }
                )
        elif template == "container_cpu_throttled_ratio":
            for pod in pods:
                rows.append(
                    {
                        "metric": "container_cpu_cfs_throttled_periods_ratio",
                        "labels": {"pod": pod.name, "container": "redis"},
                        "value": round(pod.cpu_throttled_ratio, 4),
                    }
                )
        elif template == "container_restart_count":
            for pod in pods:
                rows.append(
                    {
                        "metric": "kube_pod_container_status_restarts_total",
                        "labels": {"pod": pod.name, "container": "redis"},
                        "value": pod.restarts,
                    }
                )
        elif template == "pod_ready":
            for pod in pods:
                ready = pod.ready and pod.waiting_reason is None
                rows.append(
                    {
                        "metric": "kube_pod_container_status_ready",
                        "labels": {"pod": pod.name, "container": "redis"},
                        "value": 1 if ready else 0,
                    }
                )
        elif template == "redis_master_link_up":
            for pod in pods:
                info = self.redis[pod.name]
                if info.role != "replica":
                    continue
                rows.append(
                    {
                        "metric": "redis_master_link_up",
                        "labels": {"pod": pod.name},
                        "value": 1 if info.master_link_status == "up" else 0,
                    }
                )
        elif template == "pvc_used_ratio":
            for pod in pods:
                pvc = self.pvcs.get(f"data-{pod.name}")
                if not pvc:
                    continue
                rows.append(
                    {
                        "metric": "kubelet_volume_stats_used_bytes_ratio",
                        "labels": {"persistentvolumeclaim": pvc.name},
                        "value": round(pvc.used_ratio, 4),
                    }
                )
        elif template == "redis_rejected_connections":
            for pod in pods:
                info = self.redis[pod.name]
                rows.append(
                    {
                        "metric": "redis_rejected_connections_total",
                        "labels": {"pod": pod.name},
                        "value": info.rejected_connections,
                    }
                )
        elif template == "redis_errors_total":
            for pod in pods:
                info = self.redis[pod.name]
                if info.errorstat_oom:
                    rows.append(
                        {
                            "metric": "redis_errorstat_total",
                            "labels": {"pod": pod.name, "error": "OOM"},
                            "value": info.errorstat_oom,
                        }
                    )
                if info.persistence_errors:
                    rows.append(
                        {
                            "metric": "redis_errorstat_total",
                            "labels": {"pod": pod.name, "error": "MISCONF"},
                            "value": info.persistence_errors,
                        }
                    )
        elif template == "redis_replication_sync_full":
            for pod in pods:
                info = self.redis[pod.name]
                rows.append(
                    {
                        "metric": "redis_sync_full_total",
                        "labels": {"pod": pod.name},
                        "value": info.sync_full,
                    }
                )
        elif template == "node_pressure":
            for node in self.nodes:
                rows.append(
                    {
                        "metric": "kube_node_status_condition_pressure",
                        "labels": {"node": node["name"], "condition": "memory"},
                        "value": 1 if node["memory_pressure"] else 0,
                    }
                )
        return rows

    # -- snapshot / restore (used by the evaluation harness) -------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "pods": {k: copy.deepcopy(v) for k, v in self.pods.items()},
            "redis": {k: copy.deepcopy(v) for k, v in self.redis.items()},
            "events": copy.deepcopy(self.events),
            "pvcs": {k: copy.deepcopy(v) for k, v in self.pvcs.items()},
            "services": copy.deepcopy(self.services),
            "statefulset": copy.deepcopy(self.statefulset),
            "config_map_conf": self.config_map_conf,
            "config_map_extra": dict(self.config_map_extra),
            "secrets": copy.deepcopy(self.secrets),
        }

    def restore(self, snap: dict[str, Any]) -> None:
        self.pods = {k: replace(v) for k, v in copy.deepcopy(snap["pods"]).items()}
        self.redis = {k: replace(v) for k, v in copy.deepcopy(snap["redis"]).items()}
        self.events = copy.deepcopy(snap["events"])
        self.pvcs = {k: replace(v) for k, v in copy.deepcopy(snap["pvcs"]).items()}
        self.services = copy.deepcopy(snap["services"])
        self.statefulset = copy.deepcopy(snap["statefulset"])
        self.config_map_conf = snap["config_map_conf"]
        self.config_map_extra = dict(snap["config_map_extra"])
        self.secrets = copy.deepcopy(snap["secrets"])

    def clean_baseline(self) -> None:
        """A diagnosis-ready baseline with no fault present."""
        self.reset()

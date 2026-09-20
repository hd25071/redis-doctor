"""Injection primitives.

Each ``op`` is implemented twice:

* against :class:`sandbox.cluster.SimCluster` — used by CI, the demo and the
  committed evaluation;
* as the ``kubectl`` command list in the same scenario file — used against a
  real k3s cluster.

The sandbox implementation is intentionally an explicit mutation, not a
simulation of Kubernetes internals: the observable effect is what matters.
"""

from __future__ import annotations

from typing import Any

from sandbox.cluster import REDIS_PORT, SimCluster

MIB = 1024 * 1024


def _redis(cluster: SimCluster, pod: str):
    return cluster.redis[pod]


def _pod(cluster: SimCluster, pod: str):
    return cluster.pods[pod]


# ---------------------------------------------------------------------------
# S01 pod deleted
# ---------------------------------------------------------------------------
def delete_master_pod(cluster: SimCluster, pod: str = "demo-0") -> None:
    from tools.k8s import SandboxK8sBackend

    SandboxK8sBackend(cluster).delete_pod(pod, cluster.namespace)


def restore_pod_age(cluster: SimCluster, pod: str = "demo-0") -> None:
    state = _pod(cluster, pod)
    state.recreated = False
    state.created_seconds_ago = 5400
    for replica in cluster.replicas():
        _redis(cluster, replica.name).sync_full = 0


# ---------------------------------------------------------------------------
# S02 OOMKilled
# ---------------------------------------------------------------------------
def oom_kill(
    cluster: SimCluster, pod: str = "demo-0", restarts: int = 3, limit: str = "256Mi"
) -> None:
    state = _pod(cluster, pod)
    state.restarts = restarts
    state.last_termination_reason = "OOMKilled"
    state.last_termination_exit_code = 137
    state.last_termination_message = (
        "container redis was terminated: memory usage exceeded the cgroup limit"
    )
    state.memory_limit = limit
    state.memory_usage_bytes = 268 * MIB
    state.extra_previous_logs = [
        "1:M 20 Sep 2026 09:41:58.884 * 10000 changes in 300 seconds. Saving...",
        "1:M 20 Sep 2026 09:41:59.011 * Background saving started by pid 512",
    ]
    cluster.add_event(
        "Pod",
        pod,
        "OOMKilling",
        f"Memory cgroup out of memory: Killed process 1 (redis-server) "
        f"total-vm:1048576kB, anon-rss:{268 * 1024}kB",
        type_="Warning",
        age_seconds=120,
        count=restarts,
    )
    cluster.add_event(
        "Pod",
        pod,
        "BackOff",
        "Back-off restarting failed container redis in pod " + pod,
        type_="Warning",
        age_seconds=90,
        count=restarts,
    )


def restore_memory(cluster: SimCluster, pod: str = "demo-0") -> None:
    state = _pod(cluster, pod)
    state.restarts = 0
    state.last_termination_reason = None
    state.last_termination_message = None
    state.memory_limit = "1Gi"
    state.memory_usage_bytes = 180 * MIB
    state.extra_previous_logs = []
    cluster.events = [
        e for e in cluster.events if not (e.name == pod and e.reason in {"OOMKilling", "BackOff"})
    ]


# ---------------------------------------------------------------------------
# S03 maxmemory reached
# ---------------------------------------------------------------------------
def set_maxmemory(
    cluster: SimCluster,
    pod: str = "demo-0",
    maxmemory: str = str(64 * MIB),
    policy: str = "noeviction",
) -> None:
    info = _redis(cluster, pod)
    info.maxmemory = int(maxmemory)
    info.used_memory = int(maxmemory)
    info.used_memory_peak = int(maxmemory)
    info.maxmemory_policy = policy
    info.errorstat_oom = 37
    info.config["maxmemory"] = str(maxmemory)
    info.config["maxmemory-policy"] = policy
    info.latency_p99_ms = 12.5
    info.extra_logs = [
        "1:M 20 Sep 2026 09:44:02.118 # OOM command not allowed when used memory > 'maxmemory'.",
        "1:M 20 Sep 2026 09:44:02.118 * Command rejected: writes disabled by "
        "maxmemory policy noeviction",
    ]
    cluster.add_event(
        "Pod",
        pod,
        "RedisWritesRejected",
        "redis-server rejected writes: OOM command not allowed when used memory > 'maxmemory'",
        type_="Warning",
        age_seconds=60,
        count=37,
    )


def restore_maxmemory(cluster: SimCluster, pod: str = "demo-0") -> None:
    info = _redis(cluster, pod)
    info.maxmemory = 512 * MIB
    info.used_memory = 180 * MIB
    info.used_memory_peak = 210 * MIB
    info.maxmemory_policy = "noeviction"
    info.errorstat_oom = 0
    info.latency_p99_ms = 0.42
    info.config["maxmemory"] = str(512 * MIB)
    info.extra_logs = []


# ---------------------------------------------------------------------------
# S04 network partition
# ---------------------------------------------------------------------------
def block_replica_link(cluster: SimCluster, pods: list[str] | None = None) -> None:
    pods = pods or ["demo-1"]
    for pod in pods:
        info = _redis(cluster, pod)
        info.master_link_status = "down"
        info.link_status = "down"
        info.master_last_io_seconds_ago = 412
        info.latency_p99_ms = 6.0
        info.extra_logs = [
            f"1:S 20 Sep 2026 09:45:11.229 * Connecting to MASTER {info.master_host}:{REDIS_PORT}",
            "1:S 20 Sep 2026 09:45:11.230 * MASTER <-> REPLICA sync started",
            "1:S 20 Sep 2026 09:45:41.652 # Error condition on socket for SYNC: "
            "Connection timed out",
        ]
    cluster.add_event(
        "Pod",
        pods[0],
        "NetworkPolicyBlocked",
        "traffic to 6379 denied by networkpolicy redis-doctor-deny-replica-sync",
        type_="Warning",
        age_seconds=180,
    )


def unblock_replica_link(cluster: SimCluster, pods: list[str] | None = None) -> None:
    pods = pods or ["demo-1"]
    for pod in pods:
        info = _redis(cluster, pod)
        info.master_link_status = "up"
        info.link_status = "ok"
        info.master_last_io_seconds_ago = 1
        info.latency_p99_ms = 0.42
        info.extra_logs = []
    cluster.events = [e for e in cluster.events if e.reason != "NetworkPolicyBlocked"]


# ---------------------------------------------------------------------------
# S05 PVC pending
# ---------------------------------------------------------------------------
def pvc_pending(
    cluster: SimCluster,
    pvc: str = "data-demo-1",
    pod: str = "demo-1",
    storage_class: str = "fast-ssd",
) -> None:
    state = cluster.pvcs[pvc]
    state.phase = "Pending"
    state.storage_class = storage_class
    state.used_ratio = 0.0
    state.message = f'storageclass.storage.k8s.io "{storage_class}" not found'
    pod_state = _pod(cluster, pod)
    pod_state.phase = "Pending"
    pod_state.ready = False
    pod_state.extra_logs = []
    cluster.add_event(
        "PersistentVolumeClaim",
        pvc,
        "ProvisioningFailed",
        f'storageclass.storage.k8s.io "{storage_class}" not found',
        type_="Warning",
        age_seconds=240,
        count=12,
    )
    cluster.add_event(
        "Pod",
        pod,
        "FailedScheduling",
        "0/1 nodes are available: pod has unbound immediate PersistentVolumeClaims. "
        "preemption: 0/1 nodes are available",
        type_="Warning",
        age_seconds=240,
        count=12,
    )
    cluster.statefulset["ready_replicas"] = 2


def pvc_bound(cluster: SimCluster, pvc: str = "data-demo-1", pod: str = "demo-1") -> None:
    state = cluster.pvcs[pvc]
    state.phase = "Bound"
    state.storage_class = "local-path"
    state.used_ratio = 0.31
    state.message = ""
    pod_state = _pod(cluster, pod)
    pod_state.phase = "Running"
    pod_state.ready = True
    cluster.events = [
        e for e in cluster.events if e.reason not in {"ProvisioningFailed", "FailedScheduling"}
    ]
    cluster.statefulset["ready_replicas"] = 3


# ---------------------------------------------------------------------------
# S06 image pull failure
# ---------------------------------------------------------------------------
def bad_image(cluster: SimCluster, pod: str = "demo-0", tag: str = "7.2.9-typo") -> None:
    state = _pod(cluster, pod)
    state.image = f"redis:{tag}"
    state.phase = "Pending"
    state.ready = False
    state.waiting_reason = "ImagePullBackOff"
    state.waiting_message = f'Back-off pulling image "redis:{tag}"'
    cluster.statefulset["version"] = tag
    cluster.add_event(
        "Pod",
        pod,
        "Failed",
        f'Failed to pull image "redis:{tag}": rpc error: code = NotFound desc = '
        f'failed to resolve reference "docker.io/library/redis:{tag}": not found',
        type_="Warning",
        age_seconds=300,
        count=9,
    )
    cluster.statefulset["ready_replicas"] = 2


def good_image(cluster: SimCluster, pod: str = "demo-0", tag: str = "7.2") -> None:
    state = _pod(cluster, pod)
    state.image = f"redis:{tag}"
    state.phase = "Running"
    state.ready = True
    state.waiting_reason = None
    state.waiting_message = None
    cluster.statefulset["version"] = tag
    cluster.events = [e for e in cluster.events if e.reason != "Failed"]
    cluster.statefulset["ready_replicas"] = 3


# ---------------------------------------------------------------------------
# S07 auth failure
# ---------------------------------------------------------------------------
def break_auth(
    cluster: SimCluster, pods: list[str] | None = None, secret: str = "redis-password"
) -> None:
    pods = pods or ["demo-1"]
    cluster.secrets.pop(secret, None)
    for pod in pods:
        info = _redis(cluster, pod)
        info.auth_broken = True
        info.master_link_status = "down"
        info.link_status = "down"
        info.extra_logs = [
            f"1:S 20 Sep 2026 09:47:02.551 * Connecting to MASTER {info.master_host}:{REDIS_PORT}",
            "1:S 20 Sep 2026 09:47:02.612 # MASTER aborted replication with an error: "
            "WRONGPASS invalid username-password pair or user is disabled.",
            "1:S 20 Sep 2026 09:47:02.612 # Unable to AUTH to MASTER: NOAUTH "
            "Authentication required.",
        ]
    cluster.add_event(
        "Secret",
        secret,
        "Deleted",
        f"Secret demo/{secret} was deleted; pods of StatefulSet demo are rolling",
        type_="Warning",
        age_seconds=150,
    )


def restore_auth(
    cluster: SimCluster, pods: list[str] | None = None, secret: str = "redis-password"
) -> None:
    pods = pods or ["demo-1"]
    cluster.secrets[secret] = {"password": "s3cr3t-rd-demo"}
    for pod in pods:
        info = _redis(cluster, pod)
        info.auth_broken = False
        info.master_link_status = "up"
        info.link_status = "ok"
        info.extra_logs = []
    cluster.events = [e for e in cluster.events if e.reason != "Deleted"]


# ---------------------------------------------------------------------------
# S08 headless service missing
# ---------------------------------------------------------------------------
def delete_headless_service(cluster: SimCluster, pods: list[str] | None = None) -> None:
    pods = pods or ["demo-1", "demo-2"]
    cluster.remove_service(f"{cluster.instance}-headless")
    for pod in pods:
        info = _redis(cluster, pod)
        info.master_link_status = "down"
        info.link_status = "down"
        info.extra_logs = [
            f"1:S 20 Sep 2026 09:48:33.771 * Connecting to MASTER {info.master_host}:{REDIS_PORT}",
            "1:S 20 Sep 2026 09:48:33.902 # Unable to connect to MASTER: Name or service not known",
            "1:S 20 Sep 2026 09:48:34.010 # Error condition on socket for SYNC: "
            "Name or service not known",
        ]


def restore_headless_service(cluster: SimCluster, pods: list[str] | None = None) -> None:
    pods = pods or ["demo-1", "demo-2"]
    cluster.services[f"{cluster.instance}-headless"] = {
        "name": f"{cluster.instance}-headless",
        "type": "ClusterIP",
        "cluster_ip": "None",
        "ports": [{"name": "redis", "port": REDIS_PORT, "targetPort": REDIS_PORT}],
        "selector": {"app.kubernetes.io/instance": cluster.instance},
    }
    for pod in pods:
        info = _redis(cluster, pod)
        info.master_link_status = "up"
        info.link_status = "ok"
        info.extra_logs = []


# ---------------------------------------------------------------------------
# S09 slow query
# ---------------------------------------------------------------------------
def slow_query_load(cluster: SimCluster, pod: str = "demo-0") -> None:
    from sandbox.cluster import SlowlogEntry

    info = _redis(cluster, pod)
    info.latency_p99_ms = 612.0
    info.slowlog = [
        SlowlogEntry(
            entry_id=1042 + i,
            timestamp=1758351900 + i,
            duration_us=250000 + i * 1000,
            command=cmd,
            client_addr=f"10.42.3.77:{48720 + i}",
        )
        for i, cmd in enumerate(
            [
                "KEYS session:*",
                "SORT mylist BY weight_* GET object_* GET #",
                "KEYS cache:user:*",
                "SMEMBERS bigset",
                "HGETALL big_hash",
            ]
        )
    ]
    info.slowlog_len = len(info.slowlog)
    info.used_memory = 460 * MIB
    _pod(cluster, pod).cpu_usage_cores = 0.92
    info.extra_logs = [
        "1:M 20 Sep 2026 09:50:12.004 * Slow query observed: KEYS session:* took 250ms",
    ]


def clear_slow_query(cluster: SimCluster, pod: str = "demo-0") -> None:
    info = _redis(cluster, pod)
    info.latency_p99_ms = 0.42
    info.slowlog = []
    info.slowlog_len = 0
    info.used_memory = 180 * MIB
    _pod(cluster, pod).cpu_usage_cores = 0.04
    info.extra_logs = []


# ---------------------------------------------------------------------------
# S10 maxclients exhausted
# ---------------------------------------------------------------------------
def exhaust_clients(cluster: SimCluster, pod: str = "demo-0", clients: int = 10000) -> None:
    info = _redis(cluster, pod)
    info.maxclients = clients
    info.connected_clients = clients
    info.blocked_clients = 312
    info.rejected_connections = 1420
    info.latency_p99_ms = 38.0
    info.config["maxclients"] = str(clients)
    info.extra_logs = [
        "1:M 20 Sep 2026 09:51:44.220 # max number of clients reached",
        "1:M 20 Sep 2026 09:51:44.221 # Closing client that reached max query buffer length",
    ]
    cluster.add_event(
        "Pod",
        pod,
        "RedisMaxClients",
        f"connected_clients={clients} reached maxclients={clients}",
        type_="Warning",
        age_seconds=90,
        count=1420,
    )


def release_clients(cluster: SimCluster, pod: str = "demo-0") -> None:
    info = _redis(cluster, pod)
    info.connected_clients = 3
    info.blocked_clients = 0
    info.rejected_connections = 0
    info.latency_p99_ms = 0.42
    info.extra_logs = []
    cluster.events = [e for e in cluster.events if e.reason != "RedisMaxClients"]


# ---------------------------------------------------------------------------
# S11 disk full
# ---------------------------------------------------------------------------
def fill_disk(cluster: SimCluster, pvc: str = "data-demo-0", pod: str = "demo-0") -> None:
    state = cluster.pvcs[pvc]
    state.used_ratio = 1.0
    state.message = "filesystem is full"
    info = _redis(cluster, pod)
    info.rdb_last_bgsave_status = "err"
    info.aof_last_write_status = "err"
    info.persistence_errors = 88
    info.rdb_changes_since_last_save = 25431
    info.extra_logs = [
        "1:M 20 Sep 2026 09:53:02.114 * 10000 changes in 300 seconds. Saving...",
        "1:M 20 Sep 2026 09:53:02.180 * Background saving started by pid 903",
        "1:M 20 Sep 2026 09:53:02.640 # Failed opening the RDB file temp-903.rdb "
        "(in server root dir /data) for saving: No space left on device",
        "1:M 20 Sep 2026 09:53:02.641 # Background saving error",
        "1:M 20 Sep 2026 09:53:02.702 # MISCONF Redis is configured to save RDB snapshots, "
        "but it is currently not able to persist on disk.",
    ]
    pod_state = _pod(cluster, pod)
    pod_state.restarts = 1
    pod_state.disk_used_ratio = 1.0
    cluster.add_event(
        "Pod",
        pod,
        "FailedMount",
        'MountVolume.SetUp failed for volume "data": no space left on device',
        type_="Warning",
        age_seconds=120,
    )


def clear_disk(cluster: SimCluster, pvc: str = "data-demo-0", pod: str = "demo-0") -> None:
    state = cluster.pvcs[pvc]
    state.used_ratio = 0.31
    state.message = ""
    info = _redis(cluster, pod)
    info.rdb_last_bgsave_status = "ok"
    info.aof_last_write_status = "ok"
    info.persistence_errors = 0
    info.rdb_changes_since_last_save = 0
    info.extra_logs = []
    pod_state = _pod(cluster, pod)
    pod_state.restarts = 0
    pod_state.disk_used_ratio = 0.31
    cluster.events = [e for e in cluster.events if e.reason != "FailedMount"]


# ---------------------------------------------------------------------------
# S12 unschedulable: requests too large
# ---------------------------------------------------------------------------
def oversize_requests(
    cluster: SimCluster, pod: str = "demo-2", memory: str = "6Gi", cpu: str = "4"
) -> None:
    state = _pod(cluster, pod)
    state.phase = "Pending"
    state.ready = False
    state.memory_request = memory
    state.cpu_request = cpu
    state.waiting_reason = None
    cluster.add_event(
        "Pod",
        pod,
        "FailedScheduling",
        "0/1 nodes are available: 1 Insufficient memory, 1 Insufficient cpu. "
        "preemption: 0/1 nodes are available: 1 No preemption victims found for incoming pod.",
        type_="Warning",
        age_seconds=200,
        count=18,
    )
    cluster.statefulset["ready_replicas"] = 2


def shrink_requests(cluster: SimCluster, pod: str = "demo-2") -> None:
    state = _pod(cluster, pod)
    state.phase = "Running"
    state.ready = True
    state.memory_request = "256Mi"
    state.cpu_request = "100m"
    cluster.events = [e for e in cluster.events if e.reason != "FailedScheduling"]
    cluster.statefulset["ready_replicas"] = 3


# ---------------------------------------------------------------------------
# S13 CPU throttling
# ---------------------------------------------------------------------------
def low_cpu_limit(
    cluster: SimCluster, pod: str = "demo-0", limit: str = "100m", ratio: float = 0.78
) -> None:
    state = _pod(cluster, pod)
    state.cpu_limit = limit
    state.cpu_usage_cores = 0.13
    state.cpu_throttled_ratio = ratio
    _redis(cluster, pod).latency_p99_ms = 148.0
    cluster.add_event(
        "Pod",
        pod,
        "Throttling",
        f"container redis is being CPU throttled ({ratio * 100:.0f}% of periods)",
        type_="Warning",
        age_seconds=300,
        count=41,
    )


def restore_cpu_limit(cluster: SimCluster, pod: str = "demo-0") -> None:
    state = _pod(cluster, pod)
    state.cpu_limit = "1"
    state.cpu_usage_cores = 0.04
    state.cpu_throttled_ratio = 0.0
    _redis(cluster, pod).latency_p99_ms = 0.42
    cluster.events = [e for e in cluster.events if e.reason != "Throttling"]


# ---------------------------------------------------------------------------
# S14 replication backlog too small -> full resync storm
# ---------------------------------------------------------------------------
def shrink_repl_backlog(
    cluster: SimCluster, pod: str = "demo-1", size_bytes: int = 16 * 1024
) -> None:
    master = cluster.master_pod().name
    master_info = _redis(cluster, master)
    master_info.config["repl-backlog-size"] = str(size_bytes)
    for replica in cluster.replicas():
        info = _redis(cluster, replica.name)
        info.sync_full = 47
        info.sync_partial_ok = 3
        info.master_link_status = "down"
        info.link_status = "down"
        info.master_last_io_seconds_ago = 63
        info.extra_logs = (
            [
                f"1:S 20 Sep 2026 09:55:01.{100 + i} * Connecting to MASTER "
                f"{info.master_host}:{REDIS_PORT}"
                for i in range(0)
            ]
            + [
                f"1:S 20 Sep 2026 09:55:0{i + 1}.220 * Full resync from master: "
                f"a1b2c3d4e5f60718293a4b5c6d7e8f9012345678:{88000 + i * 1000}"
                for i in range(4)
            ]
            + [
                "1:S 20 Sep 2026 09:55:05.900 # MASTER <-> REPLICA sync: Partial "
                "resynchronization not possible (backlog exhausted)",
            ]
        )
    cluster.add_event(
        "Pod",
        pod,
        "ReplicationResync",
        "replica requested full resynchronization 47 times in the last 10m",
        type_="Warning",
        age_seconds=300,
        count=47,
    )


def restore_repl_backlog(
    cluster: SimCluster, pod: str = "demo-1", size_bytes: int = 64 * MIB
) -> None:
    master = cluster.master_pod().name
    _redis(cluster, master).config["repl-backlog-size"] = str(size_bytes)
    for replica in cluster.replicas():
        info = _redis(cluster, replica.name)
        info.sync_full = 0
        info.sync_partial_ok = 0
        info.master_link_status = "up"
        info.link_status = "ok"
        info.master_last_io_seconds_ago = 1
        info.extra_logs = []
    cluster.events = [e for e in cluster.events if e.reason != "ReplicationResync"]


# ---------------------------------------------------------------------------
# S15 readiness probe misconfigured
# ---------------------------------------------------------------------------
def break_readiness_probe(cluster: SimCluster, pod: str = "demo-0", port: int = 6380) -> None:
    state = _pod(cluster, pod)
    state.readiness_port = port
    state.ready = False
    for other in cluster.pods.values():
        other.readiness_port = port
    cluster.add_event(
        "Pod",
        pod,
        "Unhealthy",
        f"Readiness probe failed: dial tcp 10.42.3.10:{port}: connect: connection refused",
        type_="Warning",
        age_seconds=420,
        count=84,
    )


def fix_readiness_probe(cluster: SimCluster, pod: str = "demo-0", port: int = REDIS_PORT) -> None:
    state = _pod(cluster, pod)
    state.readiness_port = port
    state.ready = True
    for other in cluster.pods.values():
        other.readiness_port = port
        other.ready = True
    cluster.events = [e for e in cluster.events if e.reason != "Unhealthy"]


# ---------------------------------------------------------------------------
# S16 invalid configuration -> CrashLoopBackOff
# ---------------------------------------------------------------------------
def invalid_config(
    cluster: SimCluster, pod: str = "demo-0", directive: str = "appendonlyy yes"
) -> None:
    cluster.config_map_extra["appendonlyy"] = "yes"
    cluster.config_map_conf = cluster.render_redis_conf()
    key = directive.split()[0]
    state = _pod(cluster, pod)
    state.waiting_reason = "CrashLoopBackOff"
    state.waiting_message = "back-off 5m0s restarting failed container=redis pod=" + pod
    state.restarts = 7
    state.phase = "Running"
    state.ready = False
    state.extra_previous_logs = [
        f"1:M 20 Sep 2026 09:57:11.004 # *** FATAL CONFIG FILE ERROR (Redis {cluster.redis[pod].config and '7.2.5'}) ***",
        "1:M 20 Sep 2026 09:57:11.004 # Reading the configuration file, at line 18",
        f"1:M 20 Sep 2026 09:57:11.004 # >>> '{directive}'",
        "1:M 20 Sep 2026 09:57:11.004 # Bad directive or wrong number of arguments",
    ]
    state.extra_logs = state.extra_previous_logs
    cluster.add_event(
        "Pod",
        pod,
        "BackOff",
        "Back-off restarting failed container redis in pod " + pod,
        type_="Warning",
        age_seconds=180,
        count=7,
    )
    del key


def restore_config(cluster: SimCluster, pod: str = "demo-0") -> None:
    cluster.config_map_extra.pop("appendonlyy", None)
    cluster.config_map_conf = cluster.render_redis_conf()
    state = _pod(cluster, pod)
    state.waiting_reason = None
    state.waiting_message = None
    state.restarts = 0
    state.ready = True
    state.extra_previous_logs = []
    state.extra_logs = []
    cluster.events = [e for e in cluster.events if not (e.name == pod and e.reason == "BackOff")]


#: op name -> callable, resolved from the scenario YAML.
OPS: dict[str, Any] = {
    "delete_master_pod": delete_master_pod,
    "restore_pod_age": restore_pod_age,
    "oom_kill": oom_kill,
    "restore_memory": restore_memory,
    "set_maxmemory": set_maxmemory,
    "restore_maxmemory": restore_maxmemory,
    "block_replica_link": block_replica_link,
    "unblock_replica_link": unblock_replica_link,
    "pvc_pending": pvc_pending,
    "pvc_bound": pvc_bound,
    "bad_image": bad_image,
    "good_image": good_image,
    "break_auth": break_auth,
    "restore_auth": restore_auth,
    "delete_headless_service": delete_headless_service,
    "restore_headless_service": restore_headless_service,
    "slow_query_load": slow_query_load,
    "clear_slow_query": clear_slow_query,
    "exhaust_clients": exhaust_clients,
    "release_clients": release_clients,
    "fill_disk": fill_disk,
    "clear_disk": clear_disk,
    "oversize_requests": oversize_requests,
    "shrink_requests": shrink_requests,
    "low_cpu_limit": low_cpu_limit,
    "restore_cpu_limit": restore_cpu_limit,
    "shrink_repl_backlog": shrink_repl_backlog,
    "restore_repl_backlog": restore_repl_backlog,
    "break_readiness_probe": break_readiness_probe,
    "fix_readiness_probe": fix_readiness_probe,
    "invalid_config": invalid_config,
    "restore_config": restore_config,
}

"""Redis read tools.

Two independent controls, exactly as designed in the deployment plan:

1. client side — every command goes through :func:`tools.safety.check_redis_command`;
2. server side — the ``doctor`` ACL user in the real deployment grants only
   ``+info +role +dbsize +slowlog|get +slowlog|len +client|list +client|info
   +config|get``.

The sandbox backend hard-codes the same whitelist so the tests can prove the
client half without a cluster.
"""

from __future__ import annotations

import re
from typing import Protocol

from sandbox.cluster import REDIS_PORT, SimCluster
from tools.base import ToolResult, ToolSpec
from tools.safety import check_redis_command

ALLOWED_SECTIONS = {
    "server",
    "clients",
    "memory",
    "persistence",
    "stats",
    "replication",
    "cpu",
    "keyspace",
}


class RedisAuthError(RuntimeError):
    pass


#: redis-py raises AuthenticationError / ResponseError("NOAUTH ...") rather than
#: our RedisAuthError. Both mean the same thing to the diagnosis: authentication
#: is broken, and that is evidence in its own right.
_AUTH_MARKERS = ("noauth", "wrongpass", "invalid username-password", "authentication")


def is_auth_error(exc: BaseException) -> bool:
    blob = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in blob for marker in _AUTH_MARKERS)


def auth_failure_result(tool: str, args: dict, exc: BaseException) -> ToolResult:
    return ToolResult(
        tool=tool,
        args=args,
        ok=False,
        error=str(exc),
        summary=f"redis authentication failed: {exc}",
        raw=f"(error) {exc}",
        signals={"auth_failure"},
    )


class RedisBackend(Protocol):
    def info(self, pod: str, section: str | None) -> str: ...

    def slowlog(self, pod: str, count: int) -> list[dict]: ...

    def config_get(self, pod: str, param: str) -> dict[str, str]: ...

    def dbsize(self, pod: str) -> int: ...

    def role(self, pod: str) -> str: ...


class SandboxRedisBackend:
    def __init__(self, cluster: SimCluster) -> None:
        self.cluster = cluster

    def _state(self, pod: str):
        if pod not in self.cluster.redis:
            raise KeyError(f"pod {pod} is not part of instance {self.cluster.instance}")
        state = self.cluster.redis[pod]
        if state.auth_broken:
            raise RedisAuthError("NOAUTH Authentication required.")
        return state

    def info(self, pod: str, section: str | None) -> str:
        self._state(pod)
        return self.cluster.info_text(pod, section)

    def slowlog(self, pod: str, count: int) -> list[dict]:
        self._state(pod)
        return [
            {
                "id": entry.entry_id,
                "timestamp": entry.timestamp,
                "duration_us": entry.duration_us,
                "command": entry.command,
                "client": entry.client_addr,
            }
            for entry in self.cluster.slowlog_entries(pod, count)
        ]

    def config_get(self, pod: str, param: str) -> dict[str, str]:
        self._state(pod)
        values = self.cluster.config_get(pod, param)
        return dict(zip(values[::2], values[1::2], strict=False))

    def dbsize(self, pod: str) -> int:
        self._state(pod)
        return self.cluster.dbsize(pod)

    def role(self, pod: str) -> str:
        self._state(pod)
        return self.cluster.role_text(pod)


class RealRedisBackend:
    """Direct Redis connection as the read-only ``doctor`` ACL user.

    Uses the per-pod headless DNS name, never ``kubectl exec``: exec would hand
    the agent an unrestricted shell, which RBAC cannot constrain.
    """

    def __init__(
        self,
        namespace: str,
        instance: str,
        username: str = "doctor",
        password: str = "",
        port: int = REDIS_PORT,
        port_forward_base: int = 0,
    ) -> None:
        self.namespace = namespace
        self.instance = instance
        self.username = username
        self.password = password
        self.port = port
        # Running the agent outside the cluster (form A) means cluster DNS is
        # not resolvable and pod IPs are not routable. In that mode the pods are
        # reached through kubectl port-forwards on 127.0.0.1:
        #   redis-demo-<i>  ->  127.0.0.1:(base + i)
        self.port_forward_base = port_forward_base

    def target(self, pod: str) -> tuple[str, int]:
        if self.port_forward_base:
            index = int(pod.rsplit("-", 1)[-1])
            return "127.0.0.1", self.port_forward_base + index
        return (
            f"{pod}.{self.instance}-headless.{self.namespace}.svc.cluster.local",
            self.port,
        )

    def _client(self, pod: str):  # pragma: no cover - requires a real cluster
        import redis

        host, port = self.target(pod)
        return redis.Redis(
            host=host,
            port=port,
            username=self.username or None,
            password=self.password or None,
            socket_timeout=5,
            socket_connect_timeout=3,
            decode_responses=True,
        )

    def info(self, pod: str, section: str | None) -> str:  # pragma: no cover
        return self._client(pod).info(section) if section else self._client(pod).info()

    def slowlog(self, pod: str, count: int) -> list[dict]:  # pragma: no cover
        entries = self._client(pod).slowlog_get(count)
        return [
            {
                "id": e[0],
                "timestamp": e[1],
                "duration_us": e[2],
                "command": " ".join(str(part) for part in e[3]),
                "client": str(e[4]) if len(e) > 4 else "",
            }
            for e in entries
        ]

    def config_get(self, pod: str, param: str) -> dict[str, str]:  # pragma: no cover
        return self._client(pod).config_get(param)

    def dbsize(self, pod: str) -> int:  # pragma: no cover
        return int(self._client(pod).dbsize())

    def role(self, pod: str) -> str:  # pragma: no cover
        return "\n".join(str(part) for part in self._client(pod).execute_command("ROLE"))


# ---------------------------------------------------------------------------
# Signal extraction from INFO text
# ---------------------------------------------------------------------------

_INFO_PATTERN = re.compile(r"^([a-zA-Z_0-9]+):(.+)$", re.MULTILINE)


def parse_info(text: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip() for m in _INFO_PATTERN.finditer(text)}


def signals_from_info(info: dict[str, str]) -> set[str]:
    found: set[str] = set()
    role = (info.get("role") or "").lower()
    if role == "master":
        found.add("master_role_confirmed")
    elif role in {"slave", "replica"}:
        found.add("replica_role_confirmed")
    link = (info.get("master_link_status") or "").lower()
    if role in {"slave", "replica"} and link and link != "up":
        found.add("replica_link_down")
    try:
        used = int(info.get("used_memory", "0"))
        limit = int(info.get("maxmemory", "0"))
    except ValueError:
        used = limit = 0
    policy = (info.get("maxmemory_policy") or "").lower()
    if limit > 0 and used >= limit * 0.9:
        found.add("memory_high_usage")
    if limit > 0 and used >= limit and "noeviction" in policy:
        found.add("maxmemory_reached_noeviction")
    try:
        clients = int(info.get("connected_clients", "0"))
        maxclients = int(info.get("maxclients", "0"))
        rejected = int(info.get("rejected_connections", "0"))
    except ValueError:
        clients = maxclients = rejected = 0
    if maxclients and clients >= maxclients:
        found.add("max_clients_reached")
    if rejected > 0:
        found.add("max_clients_reached")
    if (info.get("rdb_last_bgsave_status") or "ok").lower() != "ok":
        found.add("persistence_write_error")
        found.add("disk_full_write_error")
    if (info.get("aof_last_write_status") or "ok").lower() != "ok":
        found.add("persistence_write_error")
    try:
        if int(info.get("sync_full", "0")) > 10:
            found.add("replication_full_resync_storm")
    except ValueError:
        pass
    if info.get("errorstat_OOM:count"):
        found.add("maxmemory_reached_noeviction")
    return found


def build_redis_tools(backend: RedisBackend, namespace: str, instance: str) -> list[ToolSpec]:
    # redis-operator names pods redis-<instance>-<i>; the unprefixed form is
    # accepted as well so the tool layer is not tied to one naming convention.
    allowed_pods = re.compile(rf"^(?:redis-)?{re.escape(instance)}-\d+$")

    def _guard(pod: str) -> ToolResult | None:
        if not allowed_pods.match(pod):
            return ToolResult(
                tool="redis",
                args={"pod": pod},
                ok=False,
                blocked=True,
                block_reason=(
                    f"pod must belong to instance {instance} (expected redis-{instance}-N)"
                ),
                error="target outside the diagnosed workload",
            )
        return None

    def redis_info(pod: str, section: str = "") -> ToolResult:
        if (bad := _guard(pod)) is not None:
            return bad
        # `all` is how an agent naturally asks for everything; treat it as the
        # default rather than refusing a harmless read.
        if section.strip().lower() in {"all", "default", "everything"}:
            section = ""
        if section and section.lower() not in ALLOWED_SECTIONS:
            return ToolResult(
                tool="redis_info",
                args={"pod": pod, "section": section},
                ok=False,
                blocked=True,
                block_reason=f"section must be one of {sorted(ALLOWED_SECTIONS)}",
                error="section not whitelisted",
            )
        try:
            text = backend.info(pod, section.lower() or None)
        except Exception as exc:
            if is_auth_error(exc):
                return auth_failure_result("redis_info", {"pod": pod, "section": section}, exc)
            raise
        parsed = parse_info(text)
        signals = signals_from_info(parsed)
        keep = {
            "role",
            "connected_slaves",
            "master_host",
            "master_link_status",
            "master_last_io_seconds_ago",
            "master_sync_in_progress",
            "slave_repl_offset",
            "master_repl_offset",
            "repl_backlog_size",
            "repl_backlog_active",
            "used_memory_human",
            "used_memory",
            "maxmemory_human",
            "maxmemory_policy",
            "connected_clients",
            "maxclients",
            "blocked_clients",
            "rejected_connections",
            "rdb_last_bgsave_status",
            "rdb_changes_since_last_save",
            "aof_enabled",
            "aof_last_write_status",
            "sync_full",
            "sync_partial_ok",
            "sync_partial_err",
            "uptime_in_seconds",
            "loading",
            "keyspace_hits",
            "errorstat_OOM:count",
            "errorstat_MISCONF:count",
        }
        rendered = {k: v for k, v in parsed.items() if k in keep}
        body = "\n".join(f"{k}: {v}" for k, v in rendered.items())
        return ToolResult(
            tool="redis_info",
            args={"pod": pod, "section": section},
            ok=True,
            data=rendered,
            summary=f"INFO {section or 'all'} on {pod}: role={parsed.get('role')}",
            raw=body,
            signals=signals,
        )

    def redis_slowlog(pod: str, count: int = 10) -> ToolResult:
        if (bad := _guard(pod)) is not None:
            return bad
        count = max(1, min(int(count), 64))
        try:
            entries = backend.slowlog(pod, count)
        except Exception as exc:
            if is_auth_error(exc):
                return auth_failure_result("redis_slowlog", {"pod": pod, "count": count}, exc)
            raise
        signals: set[str] = set()
        if any(entry["duration_us"] >= 10000 for entry in entries):
            signals.add("slow_query_detected")
        lines = [
            f"{i + 1}) id={e['id']} duration_us={e['duration_us']} ts={e['timestamp']} "
            f"cmd={e['command']} client={e['client']}"
            for i, e in enumerate(entries)
        ] or ["(empty array)"]
        return ToolResult(
            tool="redis_slowlog",
            args={"pod": pod, "count": count},
            ok=True,
            data=entries,
            summary=f"{len(entries)} slowlog entries on {pod}",
            raw="\n".join(lines),
            signals=signals,
        )

    def redis_config_get(pod: str, param: str) -> ToolResult:
        if (bad := _guard(pod)) is not None:
            return bad
        decision = check_redis_command(f"CONFIG GET {param}")
        if not decision.allowed:
            return ToolResult(
                tool="redis_config_get",
                args={"pod": pod, "param": param},
                ok=False,
                blocked=True,
                block_reason=decision.reason,
                error="command rejected by whitelist",
            )
        try:
            values = backend.config_get(pod, param)
        except Exception as exc:
            if is_auth_error(exc):
                return auth_failure_result("redis_config_get", {"pod": pod, "param": param}, exc)
            raise
        body = "\n".join(f"{k} = {v}" for k, v in values.items())
        signals: set[str] = set()
        backlog = values.get("repl-backlog-size")
        if backlog:
            try:
                if int(backlog) <= 1024 * 1024 and "repl-backlog-size" in param:
                    # A small backlog is a *predisposition*, not evidence of a
                    # storm: at the default 1MB it is simply the default. A
                    # storm needs the sync_full counter as well.
                    signals.add("repl_backlog_small")
            except ValueError:
                pass
        if values.get("maxmemory-policy", "").lower() == "noeviction":
            limit = values.get("maxmemory", "0")
            if limit not in {"", "0"}:
                signals.add("maxmemory_reached_noeviction")
        return ToolResult(
            tool="redis_config_get",
            args={"pod": pod, "param": param},
            ok=True,
            data=values,
            summary=f"CONFIG GET {param} on {pod}",
            raw=body or "(no matching parameter)",
            signals=signals,
        )

    def redis_query(pod: str, command: str) -> ToolResult:
        """Whitelisted generic read command: INFO/ROLE/DBSIZE/CLIENT LIST/..."""
        if (bad := _guard(pod)) is not None:
            return bad
        decision = check_redis_command(command)
        if not decision.allowed:
            return ToolResult(
                tool="redis_query",
                args={"pod": pod, "command": command},
                ok=False,
                blocked=True,
                block_reason=f"{decision.reason} (rule: {decision.rule})",
                error="command rejected by whitelist",
            )
        verb = command.split()[0].upper()
        try:
            if verb == "INFO":
                section = command.split()[1] if len(command.split()) > 1 else ""
                return redis_info(pod, section)
            if verb == "ROLE":
                return ToolResult(
                    tool="redis_query",
                    args={"pod": pod, "command": command},
                    ok=True,
                    data={"role": backend.role(pod)},
                    summary=f"ROLE on {pod}",
                    raw=backend.role(pod),
                    signals=set(),
                )
            if verb == "DBSIZE":
                return ToolResult(
                    tool="redis_query",
                    args={"pod": pod, "command": command},
                    ok=True,
                    data={"dbsize": backend.dbsize(pod)},
                    summary=f"DBSIZE on {pod}",
                    raw=f"(integer) {backend.dbsize(pod)}",
                    signals=set(),
                )
            if verb == "SLOWLOG":
                return redis_slowlog(pod, 10)
            if verb == "CONFIG":
                return redis_config_get(pod, command.split("GET", 1)[-1].strip())
            if verb == "CLIENT":
                text = backend.info(pod, "clients")
                return ToolResult(
                    tool="redis_query",
                    args={"pod": pod, "command": command},
                    ok=True,
                    data={"clients": parse_info(text)},
                    summary=f"CLIENT LIST on {pod}",
                    raw=text,
                    signals=signals_from_info(parse_info(text)),
                )
        except Exception as exc:
            if is_auth_error(exc):
                return auth_failure_result("redis_query", {"pod": pod, "command": command}, exc)
            raise
        return ToolResult(
            tool="redis_query",
            args={"pod": pod, "command": command},
            ok=False,
            error="unhandled whitelisted command",
        )

    return [
        ToolSpec(
            name="redis_info",
            tier="read",
            description="Redis INFO for one section: server|clients|memory|persistence|"
            "stats|replication|cpu|keyspace (default all).",
            params={"pod": f"pod name, e.g. {instance}-0", "section": "INFO section (optional)"},
            fn=redis_info,
            max_result_chars=4000,
        ),
        ToolSpec(
            name="redis_slowlog",
            tier="read",
            description="Last N slowlog entries with execution time in microseconds.",
            params={"pod": "pod name", "count": "entries to return (<=64)"},
            fn=redis_slowlog,
            max_result_chars=4000,
        ),
        ToolSpec(
            name="redis_config_get",
            tier="read",
            description="CONFIG GET for a parameter, e.g. maxmemory-policy or repl-backlog-size.",
            params={"pod": "pod name", "param": "parameter name"},
            fn=redis_config_get,
            max_result_chars=2000,
        ),
        ToolSpec(
            name="redis_query",
            tier="read",
            description="Run one whitelisted Redis command: INFO, ROLE, DBSIZE, "
            "CONFIG GET <p>, SLOWLOG GET, CLIENT LIST. Anything else is refused.",
            params={"pod": "pod name", "command": "command line"},
            fn=redis_query,
            max_result_chars=4000,
        ),
    ]

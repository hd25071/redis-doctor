from __future__ import annotations

from tools.k8s import signals_from_events, signals_from_logs


def test_unknown_tool_is_refused_and_counted(registry) -> None:
    result = registry.call("kubectl_delete_everything")
    assert result.blocked is True
    assert "whitelist" in (result.block_reason or "")
    assert registry.audit.unauthorized_attempts == 1


def test_write_tool_requires_approval(registry) -> None:
    result = registry.call("k8s_delete_pod", pod="demo-0")
    assert result.blocked is True
    assert "approval" in (result.block_reason or "")
    assert registry.audit.blocked_unapproved_write == 1
    assert registry.audit.unauthorized_attempts == 1
    # ... and it works once approved by the gate.
    approved = registry.call("k8s_delete_pod", __approved__=True, pod="demo-0")
    assert approved.ok is True
    assert registry.audit.blocked_unapproved_write == 1


def test_secret_kind_is_not_readable(registry) -> None:
    result = registry.call("k8s_get_resource", kind="secret", name="redis-password")
    assert result.blocked is True
    assert registry.audit.blocked_policy == 1


def test_redis_command_denylist_applies_inside_the_tool(registry) -> None:
    result = registry.call("redis_query", pod="demo-0", command="FLUSHALL")
    assert result.blocked is True
    assert "denylist" in (result.block_reason or "")
    assert registry.audit.unauthorized_attempts == 1


def test_tools_cannot_target_other_workloads(registry) -> None:
    result = registry.call("redis_info", pod="zhiyuan-redis", section="server")
    assert result.blocked is True
    assert registry.audit.blocked_policy == 1


def test_budget_is_enforced(ctx) -> None:
    registry = ctx.build_registry(include_kb=False)
    registry.budget.max_tool_calls = 2
    registry.call("k8s_get_pods")
    registry.call("k8s_events")
    third = registry.call("redis_info", pod="demo-0", section="server")
    assert third.blocked is True
    assert registry.audit.blocked_budget == 1


def test_info_sections_are_validated(registry) -> None:
    assert registry.call("redis_info", pod="demo-0", section="all").ok is True
    bad = registry.call("redis_info", pod="demo-0", section="commandstats")
    assert bad.blocked is True


def test_log_signal_extraction() -> None:
    assert "pod_oom_killed" in signals_from_pods_helper("OOMKilled")
    signals = signals_from_logs("# OOM command not allowed when used memory > 'maxmemory'.")
    assert "maxmemory_reached_noeviction" in signals
    dns = signals_from_logs("# Unable to connect to MASTER: Name or service not known")
    assert "dns_resolution_failure" in dns
    storm = signals_from_logs("\n".join(["* Full resync from master: x:1"] * 4))
    assert "replication_full_resync_storm" in storm
    single = signals_from_logs("* Full resync from master: x:1")
    assert "replication_full_resync_storm" not in single


def signals_from_pods_helper(_: str) -> set[str]:
    from tools.k8s import signals_from_pods

    return signals_from_pods(
        [
            {
                "name": "demo-0",
                "phase": "Running",
                "ready": True,
                "restartCount": 3,
                "containerState": {"lastState": {"terminated": {"reason": "OOMKilled"}}},
            }
        ]
    )


def test_event_signal_extraction() -> None:
    signals = signals_from_events(
        [
            {
                "type": "Warning",
                "reason": "FailedScheduling",
                "message": "0/1 nodes are available: 1 Insufficient memory",
            },
            {"type": "Normal", "reason": "Killing", "message": "Stopping container redis"},
        ]
    )
    assert "insufficient_resources" in signals
    assert "pod_recreated_recently" in signals


def test_output_is_redacted_before_it_reaches_the_model(ctx) -> None:
    ctx.cluster.secrets["demo-secret"] = {"password": "s3cr3t-rd-demo"}
    ctx.cluster.redis["demo-0"].requirepass = "s3cr3t-rd-demo"
    ctx.cluster.redis["demo-0"].extra_logs = ["AUTH with password s3cr3t-rd-demo failed"]
    registry = ctx.build_registry(include_kb=False)
    result = registry.call("k8s_logs", pod="demo-0", tail=50)
    assert "s3cr3t-rd-demo" not in result.raw

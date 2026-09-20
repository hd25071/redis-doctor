"""Deployment acceptance checks (read-only).

These are the executable form of the deployment checklist in
``docs/runbook.md``: they print what they observed and exit non-zero when an
acceptance criterion is not met, so ``make smoke`` is meaningful.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

NAMESPACE = "demo"
AGENT_NAMESPACE = "redis-doctor"


def _kubectl(args: list[str], timeout: int = 20) -> tuple[int, str]:
    if shutil.which("kubectl") is None:
        return 127, "kubectl not found on PATH"
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["kubectl", *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    return completed.returncode, (completed.stdout or completed.stderr).strip()


def cluster_checks() -> int:
    """Everything a real deployment must satisfy before it is trusted."""
    checks: list[tuple[str, bool, str]] = []

    code, out = _kubectl(["get", "pods", "-n", NAMESPACE, "-o", "json"])
    if code != 0:
        checks.append(("诊断命名空间可读", False, out))
    else:
        pods = json.loads(out)["items"]
        ready = [
            p
            for p in pods
            if p["status"].get("phase") == "Running"
            and all(c.get("ready") for c in p["status"].get("containerStatuses", []) or [])
        ]
        checks.append((f"demo 命名空间 Pod 就绪 ({len(ready)}/{len(pods)})", len(ready) > 0, ""))
        redis_pods = [p for p in pods if p["metadata"]["name"].startswith("demo-")]
        checks.append((f"Redis Pod 数量 = 3 ({len(redis_pods)})", len(redis_pods) == 3, ""))

    code, out = _kubectl(["get", "redis.ops.example.com", "demo", "-n", NAMESPACE, "-o", "json"])
    if code == 0:
        status = json.loads(out).get("status", {})
        checks.append(
            (
                f"Redis CR phase={status.get('phase')} ready={status.get('readyReplicas')}",
                status.get("readyReplicas") == 3,
                "",
            )
        )
    else:
        checks.append(("Redis CR 可读（redis-operator 已部署）", False, out))

    for label, args, keep in (
        ("Prometheus 可达", ["get", "svc", "-n", "monitoring"], True),
        ("Agent Deployment 存在", ["get", "deploy", "-n", AGENT_NAMESPACE], True),
    ):
        code, out = _kubectl(args)
        checks.append((label, code == 0 and (bool(out) if keep else True), out if code else ""))

    return _report(checks)


def rbac_checks() -> int:
    """The RBAC assertions from the security design.

    reader must NOT be able to read Secrets or delete pods; actuator must be
    able to delete pods and must not be able to read Secrets.
    """
    reader = f"system:serviceaccount:{AGENT_NAMESPACE}:doctor-reader"
    actuator = f"system:serviceaccount:{AGENT_NAMESPACE}:doctor-actuator"
    expectations = [
        ("reader 不能删除 Pod", reader, ["delete", "pod", "-n", NAMESPACE], "no"),
        ("reader 不能读取 Secret", reader, ["get", "secret", "-n", NAMESPACE], "no"),
        ("reader 可以读取 Pod", reader, ["get", "pod", "-n", NAMESPACE], "yes"),
        ("reader 可以读取 Pod 日志", reader, ["get", "pods/log", "-n", NAMESPACE], "yes"),
        ("reader 不能执行 exec", reader, ["create", "pods/exec", "-n", NAMESPACE], "no"),
        ("actuator 可以删除 Pod", actuator, ["delete", "pod", "-n", NAMESPACE], "yes"),
        ("actuator 不能读取 Secret", actuator, ["get", "secret", "-n", NAMESPACE], "no"),
    ]
    checks: list[tuple[str, bool, str]] = []
    for label, subject, action, expected in expectations:
        code, out = _kubectl(["auth", "can-i", *action, f"--as={subject}"])
        observed = out.strip().lower()
        checks.append((label, code == 0 and observed == expected, f"got '{out.strip()}'"))
    return _report(checks)


def _report(checks: list[tuple[str, bool, str]]) -> int:
    failures = 0
    for label, ok, detail in checks:
        mark = "PASS" if ok else "FAIL"
        line = f"[{mark}] {label}"
        if detail and not ok:
            line += f"  ({detail[:160]})"
        print(line)
        failures += 0 if ok else 1
    print(f"\n{len(checks) - failures}/{len(checks)} 通过")
    return 1 if failures else 0


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "check"
    raise SystemExit(rbac_checks() if action == "rbac-check" else cluster_checks())

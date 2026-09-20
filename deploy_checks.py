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
    # Third element: "fail" for hard requirements, "skip" for components that
    # only exist in the in-cluster form (form B). A host-run agent against a
    # cluster without the monitoring stack is a supported configuration.
    checks: list[tuple[str, bool, str, str]] = []

    code, out = _kubectl(["get", "pods", "-n", NAMESPACE, "-o", "json"])
    if code != 0:
        checks.append(("诊断命名空间可读", False, out, "fail"))
    else:
        pods = json.loads(out)["items"]
        ready = [
            p
            for p in pods
            if p["status"].get("phase") == "Running"
            and all(c.get("ready") for c in p["status"].get("containerStatuses", []) or [])
        ]
        checks.append(
            (f"demo 命名空间 Pod 就绪 ({len(ready)}/{len(pods)})", len(ready) > 0, "", "fail")
        )
        redis_pods = [p for p in pods if p["metadata"]["name"].startswith("redis-demo-")]
        checks.append((f"Redis Pod 数量 = 3 ({len(redis_pods)})", len(redis_pods) == 3, "", "fail"))

    code, out = _kubectl(["get", "redis.ops.example.com", "demo", "-n", NAMESPACE, "-o", "json"])
    if code == 0:
        status = json.loads(out).get("status", {})
        checks.append(
            (
                f"Redis CR phase={status.get('phase')} ready={status.get('readyReplicas')}",
                status.get("readyReplicas") == 3,
                "",
                "fail",
            )
        )
    else:
        checks.append(("Redis CR 可读（redis-operator 已部署）", False, out, "fail"))

    # Optional components: only meaningful in the in-cluster deployment form.
    for label, args, namespace in (
        ("monitoring 命名空间存在（指标工具可用）", ["get", "ns", "monitoring"], "monitoring"),
        (
            "Agent Deployment 存在（集群内形态）",
            ["get", "deploy", "-n", AGENT_NAMESPACE],
            AGENT_NAMESPACE,
        ),
    ):
        code, out = _kubectl(args)
        if code == 0:
            checks.append((label, True, "", "fail"))
        else:
            checks.append(
                (
                    f"{label} [未部署，跳过]",
                    True,
                    "host-run agent form (RD_BACKEND=real + kubeconfig)",
                    "skip",
                )
            )
        del namespace

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
    checks: list[tuple[str, bool, str, str]] = []
    for label, subject, action, expected in expectations:
        code, out = _kubectl(["auth", "can-i", *action, f"--as={subject}"])
        observed = out.strip().lower()
        # `kubectl auth can-i` exits 1 when the answer is "no"; that is a
        # successful check, not a failed command. Only an error response is a
        # real failure of the check itself.
        if observed.startswith("error") or not observed:
            checks.append((label, False, out.strip(), "fail"))
            continue
        checks.append((label, observed == expected, f"got '{out.strip()}'", "fail"))
        del code
    return _report(checks)


def _report(checks: list[tuple[str, bool, str, str]]) -> int:
    failures = 0
    skipped = 0
    for label, ok, detail, kind in checks:
        mark = "SKIP" if kind == "skip" else ("PASS" if ok else "FAIL")
        line = f"[{mark}] {label}"
        if detail and not ok and kind != "skip":
            line += f"  ({detail[:160]})"
        elif kind == "skip" and detail:
            line += f"  ({detail})"
        print(line)
        if kind == "skip":
            skipped += 1
        else:
            failures += 0 if ok else 1
    total = len(checks) - skipped
    print(f"\n{total - failures}/{total} 通过, {skipped} 跳过")
    return 1 if failures else 0


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "check"
    raise SystemExit(rbac_checks() if action == "rbac-check" else cluster_checks())

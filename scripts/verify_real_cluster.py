"""Verify faultlab's kubectl paths against a real cluster.

For each scenario it:

  1. runs ``inject_kubectl`` (real, ``kubectl`` on PATH, KUBECONFIG in env);
  2. waits, then runs the diagnosis with ``RD_BACKEND=real``;
  3. records whether the scenario's required evidence signals were observed;
  4. runs ``recover_kubectl`` and re-checks the cluster is healthy.

Output: ``eval/results/real-cluster.json`` plus a printed table. The point is to
turn "the kubectl path looks right" into per-scenario evidence, and to surface
the scenarios where the operator does not expose the knob the fault needs.

    KUBECONFIG=./k3s.yaml python scripts/verify_real_cluster.py --scenarios S01,S06
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import rdconfig  # noqa: E402
from agent.graph import DiagnosisGraph  # noqa: E402
from agent.reference import ReferenceReasoner  # noqa: E402
from agent.variants import VARIANTS  # noqa: E402
from faultlab.runner import FaultLab, run_shell  # noqa: E402
from sandbox.cluster import SimCluster  # noqa: E402
from tools.context import ToolContext  # noqa: E402


class PortForwards:
    """Keep ``kubectl port-forward`` tunnels for the doctor user alive.

    A host-run agent (form A) cannot resolve cluster DNS, so the Redis protocol
    side goes through 127.0.0.1 tunnels.
    """

    def __init__(self, namespace: str, instance: str, base: int, replicas: int = 3) -> None:
        self.processes: list[subprocess.Popen] = []
        self.base = base
        if not base:
            return
        for index in range(replicas):
            pod = f"redis-{instance}-{index}"
            self.processes.append(
                subprocess.Popen(  # noqa: S603
                    [
                        "kubectl",
                        "-n",
                        namespace,
                        "port-forward",
                        f"pod/{pod}",
                        f"{base + index}:6379",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        time.sleep(4)

    def stop(self) -> None:
        for process in self.processes:
            process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


def _kubectl(args: list[str], timeout: int = 180) -> tuple[int, str]:
    done = subprocess.run(  # noqa: S603 - fixed argv
        ["kubectl", *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    return done.returncode, (done.stdout or done.stderr).strip()


def _sh(command: str, timeout: int = 300) -> tuple[int, str]:
    return run_shell(command, timeout=timeout)


def _cluster_healthy(namespace: str) -> bool:
    code, out = _kubectl(["get", "redis", "demo", "-n", namespace, "-o", "json"])
    if code != 0:
        return False
    status = json.loads(out).get("status", {})
    if status.get("readyReplicas") != 3 or status.get("phase") != "Running":
        return False
    for pod in ("redis-demo-1", "redis-demo-2"):
        code, out = _kubectl(
            [
                "exec",
                pod,
                "-n",
                namespace,
                "--",
                "sh",
                "-c",
                'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning INFO replication',
            ]
        )
        if code != 0 or "master_link_status:up" not in out:
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenarios", default="")
    parser.add_argument("--wait", type=int, default=25, help="seconds to let the fault settle")
    parser.add_argument("--settle", type=int, default=45, help="seconds to wait after recovery")
    parser.add_argument("--output", default="eval/results/real-cluster.json")
    parser.add_argument("--inject-only", action="store_true")
    args = parser.parse_args()

    settings = rdconfig.Settings.from_env()
    settings.backend = "real"
    settings.approval_required = True
    cluster = SimCluster(settings.namespace, settings.instance)  # scenario metadata only
    lab = FaultLab(cluster, REPO_ROOT / "faultlab" / "scenarios")
    ids = args.scenarios.split(",") if args.scenarios else lab.ids()

    results = []
    tunnels = PortForwards(settings.namespace, settings.instance, settings.redis_port_forward_base)
    for scenario_id in ids:
        scenario = lab.get(scenario_id)
        print(f"\n=== {scenario.id} {scenario.category} ===", flush=True)
        record = {
            "scenario_id": scenario.id,
            "category": scenario.category,
            "inject_ok": False,
            "recover_ok": False,
            "healthy_before": _cluster_healthy(settings.namespace),
            "signals_seen": [],
            "required_signals": scenario.required_signals,
            "predicted": None,
            "inject_outputs": [],
            "recover_outputs": [],
            "error": "",
        }
        if not record["healthy_before"]:
            # A previous scenario left the cluster degraded: the run would be
            # attributed to the wrong fault, so it is marked invalid instead.
            record["error"] = "cluster was not healthy before injection; run marked invalid"
            record["invalid_run"] = True
            print(f"  SKIP: {record['error']}", flush=True)
            results.append(record)
            continue
        try:
            for command in scenario.inject_kubectl:
                code, out = _sh(command)
                record["inject_outputs"].append(
                    {"command": command, "code": code, "output": out[:400]}
                )
                print(f"  inject [{code}] {command[:100]}", flush=True)
            record["inject_ok"] = all(item["code"] == 0 for item in record["inject_outputs"])
            time.sleep(args.wait)

            if not args.inject_only:
                ctx = ToolContext(settings=settings, kb_index=None)
                reasoner = ReferenceReasoner(VARIANTS["D"])
                run = DiagnosisGraph(
                    ctx,
                    VARIANTS["D"],
                    reasoner=reasoner,
                    approver=lambda *_: False,
                    settings=settings,
                ).run(scenario.alert)
                record["signals_seen"] = sorted(run.state.observed_signals)
                record["predicted"] = run.report.root_cause if run.report else None
                record["root_cause_match"] = record["predicted"] == scenario.category
                record["required_seen"] = sorted(
                    set(scenario.required_signals) & set(run.state.observed_signals)
                )
                record["tool_calls"] = run.registry.audit.total
                record["blocked"] = run.registry.audit.blocked_policy
                print(
                    f"  diagnose -> {record['predicted']} (seen: {record['signals_seen']})",
                    flush=True,
                )

            for command in scenario.recover_kubectl:
                code, out = _sh(command)
                record["recover_outputs"].append(
                    {"command": command, "code": code, "output": out[:400]}
                )
                print(f"  recover [{code}] {command[:100]}", flush=True)
            record["recover_ok"] = all(item["code"] == 0 for item in record["recover_outputs"])
            time.sleep(args.settle)
            record["healthy_after"] = _cluster_healthy(settings.namespace)
        except Exception as exc:  # keep going: one scenario must not stop the run
            record["error"] = f"{type(exc).__name__}: {exc}"
            print(f"  ERROR {record['error']}", flush=True)
        results.append(record)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    tunnels.stop()
    print("\n| 场景 | 注入 | 诊断 | 关键证据 | 恢复 | 恢复后健康 |")
    print("|---|---|---|---|---|---|")
    for item in results:
        seen = "".join(
            "✅" if s in item["signals_seen"] else "❌" for s in item["required_signals"]
        )
        match = item.get("root_cause_match")
        print(
            f"| {item['scenario_id']} | {'✅' if item['inject_ok'] else '❌'} | "
            f"{item.get('predicted') or '—'} {'✅' if match else '❌' if match is False else ''} | "
            f"{seen} | {'✅' if item['recover_ok'] else '❌'} | "
            f"{'✅' if item.get('healthy_after') else '❌'} |"
        )
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

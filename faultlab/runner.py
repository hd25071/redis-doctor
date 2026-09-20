"""Inject, recover, and verify cleanliness between evaluation runs."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from faultlab.ops import OPS
from faultlab.schema import Scenario, load_all, load_scenario
from sandbox.cluster import REDIS_PORT, SimCluster

DEFAULT_SCENARIOS = Path("faultlab/scenarios")


_SHELL_CACHE: list[str | None] = []
_SHELL_META = re.compile(r"[|&;<>`]|\$\(")


def posix_shell() -> str | None:
    """A *working* POSIX shell, or None.

    The scenario commands use POSIX quoting (``-p '{"json"}'``). On Windows
    ``shell=True`` hands them to cmd.exe, which does not strip single quotes —
    that made every quoting-sensitive command fail. Candidates are probed with a
    real command, because some machines ship a ``bash.exe`` stub (the WSL relay)
    that exists but cannot run anything.
    """
    if _SHELL_CACHE:
        return _SHELL_CACHE[0]
    candidates = [os.environ.get("RD_SHELL"), "sh", "bash"]
    for candidate in candidates:
        if not candidate:
            continue
        path = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if not path:
            continue
        try:
            probe = subprocess.run(  # noqa: S603
                [path, "-c", "echo rd-shell-ok"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and "rd-shell-ok" in probe.stdout:
            _SHELL_CACHE.append(path)
            return path
    _SHELL_CACHE.append(None)
    return None


def run_shell(command: str, timeout: int = 300) -> tuple[int, str]:
    """Run one scenario command, preferring a POSIX shell.

    Without one, commands that need no shell features are split with
    ``shlex`` and executed directly, so quoting still behaves. Commands that do
    need a shell (``$VAR``, pipes) fall back to the platform shell and are
    reported as such.
    """
    shell = posix_shell()
    if shell:
        argv: list[str] | str = [shell, "-c", command]
        use_shell = False
    elif not _SHELL_META.search(command):
        argv = shlex.split(command)
        use_shell = False
    else:
        argv = command
        use_shell = True
    done = subprocess.run(  # noqa: S602,S603 - reviewed scenario command
        argv,
        shell=use_shell,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    output = (done.stdout or done.stderr).strip()
    if use_shell:
        output = f"(platform shell) {output}"
    return done.returncode, output


class FaultLab:
    """Scenario library + inject/recover driver."""

    def __init__(self, cluster: SimCluster, scenarios_dir: Path | str = DEFAULT_SCENARIOS):
        self.cluster = cluster
        self.scenarios_dir = Path(scenarios_dir)
        self.scenarios: dict[str, Scenario] = {
            scenario.id: scenario for scenario in load_all(self.scenarios_dir)
        }

    # -- library ---------------------------------------------------------
    def get(self, scenario_id: str) -> Scenario:
        return self.scenarios[scenario_id]

    def ids(self) -> list[str]:
        return sorted(self.scenarios)

    def all(self) -> list[Scenario]:
        return [self.scenarios[k] for k in self.ids()]

    def held_out(self) -> list[Scenario]:
        return [s for s in self.all() if s.held_out]

    # -- execution -------------------------------------------------------
    def reset(self) -> None:
        """Back to a clean, fault-free cluster before every run."""
        self.cluster.reset()

    def inject(self, scenario_id: str) -> Scenario:
        scenario = self.get(scenario_id)
        for step in scenario.inject_sim:
            self._apply(step.op, step.params)
        self.cluster.notes.append(f"injected {scenario.id}")
        return scenario

    def recover(self, scenario_id: str) -> Scenario:
        scenario = self.get(scenario_id)
        for step in scenario.recover_sim:
            self._apply(step.op, step.params)
        return scenario

    def _apply(self, op: str, params: dict) -> None:
        func = OPS.get(op)
        if func is None:
            raise KeyError(f"unknown faultlab op: {op}")
        func(self.cluster, **params)

    def wait_stable(self, scenario: Scenario, settle: bool = False, timeout: float = 30.0) -> bool:
        """Let the cluster settle after injection.

        The sandbox is deterministic and already settled, so ``settle=False``
        (the default) is a no-op. Against a real cluster the harness passes
        ``settle=True`` and the scenario's ``wait_seconds`` is honoured —
        otherwise a fast diagnosis would race the cluster's own reaction.
        """
        if not settle:
            return True
        deadline = time.time() + min(timeout, float(scenario.wait_seconds))
        while time.time() < deadline:
            time.sleep(0.05)
        return True

    def is_clean(self) -> bool:
        """True when no injected fault is observable any more."""
        for info in self.cluster.redis.values():
            if info.master_link_status != "up" or info.auth_broken:
                return False
            if info.errorstat_oom or info.persistence_errors:
                return False
            if info.slowlog or info.latency_p99_ms > 1.0:
                return False
            if info.rejected_connections or info.connected_clients >= info.maxclients:
                return False
        for pod in self.cluster.pods.values():
            if pod.restarts or pod.waiting_reason or not pod.ready:
                return False
            if pod.last_termination_reason:
                return False
            if pod.recreated:
                return False
            if pod.cpu_throttled_ratio > 0.5 or pod.readiness_port != REDIS_PORT:
                return False
        for pvc in self.cluster.pvcs.values():
            if pvc.phase != "Bound":
                return False
        if f"{self.cluster.instance}-headless" not in self.cluster.services:
            return False
        return len(self.cluster.services) >= 2 and self.cluster.config_map_extra == {}

    # -- real cluster ----------------------------------------------------
    def real_commands(self, scenario_id: str, phase: str) -> list[str]:
        scenario = self.get(scenario_id)
        return scenario.inject_kubectl if phase == "inject" else scenario.recover_kubectl

    def run_kubectl(self, commands: list[str], dry_run: bool = True) -> list[str]:
        """Run the kubectl half of a scenario.

        ``dry_run`` is the default everywhere: the sandbox is the supported path
        in this repository, and the kubectl commands are marked
        ``kubectl_verified: false`` until someone runs them on a real k3s host.
        """
        if shutil.which("kubectl") is None:
            raise RuntimeError("kubectl not found on PATH")
        outputs = []
        for command in commands:
            if dry_run:
                outputs.append(f"[dry-run] {command}")
                continue
            code, out = run_shell(command, timeout=300)
            outputs.append(f"[{code}] {out}")
        return outputs


def load_scenario_by_id(scenario_id: str, directory: Path | str = DEFAULT_SCENARIOS) -> Scenario:
    for path in sorted(Path(directory).glob("S*.yaml")):
        if path.stem.upper().startswith(scenario_id.upper()):
            return load_scenario(path)
    raise KeyError(scenario_id)

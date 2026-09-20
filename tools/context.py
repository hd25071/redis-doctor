"""Wiring: settings + backends -> one :class:`ToolRegistry` per diagnosis."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import rdconfig
from sandbox.cluster import SimCluster
from tools.base import Budget, CallAudit, ToolRegistry
from tools.k8s import RealK8sBackend, SandboxK8sBackend, build_k8s_tools
from tools.kb import DEFAULT_INDEX, KnowledgeBase, build_kb_tool
from tools.prom import RealPromBackend, SandboxPromBackend, build_prom_tools
from tools.redis import RealRedisBackend, SandboxRedisBackend, build_redis_tools
from tools.safety import Sanitizer


@dataclass
class ToolContext:
    """Everything a tool needs, including which world it is looking at."""

    settings: rdconfig.Settings = field(default_factory=rdconfig.Settings.from_env)
    cluster: SimCluster | None = None
    kb_index: Path | None = None
    sanitizer: Sanitizer = field(default_factory=Sanitizer.from_env)

    def __post_init__(self) -> None:
        if self.kb_index is None:
            candidate = self.settings.resolve(self.settings.kb_index)
            self.kb_index = candidate if candidate.exists() else DEFAULT_INDEX
        if self.settings.backend == "sandbox" and self.cluster is None:
            self.cluster = SimCluster(self.settings.namespace, self.settings.instance)
        if self.cluster is not None:
            # A diagnosis must never echo the credentials it was given. In the
            # sandbox the "environment" secrets live on the simulated cluster,
            # so seed the sanitizer from there as well as from the process env.
            values = tuple(
                value
                for secret in self.cluster.secrets.values()
                for value in secret.values()
                if value
            )
            if values:
                self.sanitizer = Sanitizer(
                    extra_secrets=tuple(self.sanitizer.extra_secrets) + values
                )

    # -- backends --------------------------------------------------------
    def k8s_backend(self):
        if self.settings.backend == "real":
            return RealK8sBackend(self.settings.kubeconfig or None)
        assert self.cluster is not None
        return SandboxK8sBackend(self.cluster)

    def redis_backend(self):
        if self.settings.backend == "real":
            return RealRedisBackend(
                namespace=self.settings.namespace,
                instance=self.settings.instance,
                username=self.settings.redis_acl_user,
                password=self.settings.redis_acl_password,
            )
        assert self.cluster is not None
        return SandboxRedisBackend(self.cluster)

    def prom_backend(self):
        if self.settings.backend == "real":
            return RealPromBackend(self.settings.prom_url or "http://127.0.0.1:9090")
        assert self.cluster is not None
        return SandboxPromBackend(self.cluster)

    def knowledge_base(self) -> KnowledgeBase:
        path = self.kb_index
        if path and Path(path).exists():
            return KnowledgeBase.from_file(path)
        return KnowledgeBase.empty()

    # -- registry --------------------------------------------------------
    def build_registry(
        self,
        budget: Budget | None = None,
        allow_write: bool = False,
        include_kb: bool = True,
    ) -> ToolRegistry:
        specs = []
        specs += build_k8s_tools(self.k8s_backend(), self.settings.namespace)
        specs += build_redis_tools(
            self.redis_backend(), self.settings.namespace, self.settings.instance
        )
        specs += build_prom_tools(self.prom_backend())
        if include_kb:
            specs += build_kb_tool(self.knowledge_base())
        return ToolRegistry(
            specs,
            sanitize=self.sanitizer.redact,
            budget=budget
            or Budget(
                max_steps=self.settings.max_steps,
                max_tool_calls=self.settings.max_tool_calls,
                max_seconds=self.settings.max_seconds,
            ),
            audit=CallAudit(),
            allow_write=allow_write,
        )

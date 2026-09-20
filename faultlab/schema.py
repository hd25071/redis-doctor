"""Scenario schema.

One YAML per fault. The file is the single source of truth for:

* how to inject and how to recover (both in the sandbox and with kubectl);
* the alert text the agent receives;
* the ground-truth root-cause category;
* the key evidence the agent is expected to observe (``required_signals``).

Keeping injection, recovery and ground truth in one reviewed file is what makes
the evaluation reproducible: the harness reads this file and nothing else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from agent.state import ROOT_CAUSES


class Step(BaseModel):
    """One injection/recovery operation."""

    op: str
    params: dict[str, Any] = Field(default_factory=dict)


class Scenario(BaseModel):
    id: str
    name: str
    category: str
    severity: str = "P1"
    held_out: bool = False
    alert: str
    summary: str = ""
    symptom_keywords: list[str] = Field(default_factory=list)
    required_signals: list[str] = Field(default_factory=list)
    acceptable_actions: list[str] = Field(default_factory=list)
    wait_seconds: int = 5
    inject_sim: list[Step] = Field(default_factory=list)
    recover_sim: list[Step] = Field(default_factory=list)
    inject_kubectl: list[str] = Field(default_factory=list)
    recover_kubectl: list[str] = Field(default_factory=list)
    kubectl_verified: bool = False
    notes: str = ""

    @field_validator("category")
    @classmethod
    def _known_category(cls, value: str) -> str:
        if value not in ROOT_CAUSES:
            raise ValueError(f"unknown root cause category: {value}")
        return value

    @property
    def pods_involved(self) -> list[str]:
        pods = []
        for step in [*self.inject_sim, *self.recover_sim]:
            pod = step.params.get("pod")
            if isinstance(pod, str):
                pods.append(pod)
            pods.extend(step.params.get("pods", []) or [])
        return sorted(set(pods))


def load_scenario(path: Path | str) -> Scenario:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Scenario(**data)


def load_all(directory: Path | str) -> list[Scenario]:
    directory = Path(directory)
    return [load_scenario(path) for path in sorted(directory.glob("S*.yaml"))]

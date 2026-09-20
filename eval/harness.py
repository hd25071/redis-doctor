"""Run the ablation matrix against injectable scenarios."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import rdconfig
from agent.graph import DiagnosisGraph
from agent.llm import CassetteLLM, build_llm
from agent.reference import ReferenceReasoner
from agent.state import ApprovalRequest, DiagnosisState
from agent.variants import VARIANTS, Variant
from eval.metrics import (
    ScenarioRun,
    aggregate,
    failure_breakdown,
    per_scenario_matrix,
    score_run,
)
from faultlab.runner import FaultLab
from sandbox.cluster import SimCluster
from tools.context import ToolContext

MVP = "0.1.0"


@dataclass
class EvalConfig:
    variants: list[str] = field(default_factory=lambda: ["A", "B", "C", "D"])
    runs: int = 3
    scenario_ids: list[str] = field(default_factory=list)
    approval_required: bool = True
    auto_approve_l1: bool = True
    budget_pressure: bool = False
    backend: str = "sandbox"
    model: str = ""
    policy: str = "reference"
    notes: str = ""


@dataclass
class EvalResult:
    runs: list[ScenarioRun]
    meta: dict[str, Any]

    @property
    def aggregates(self) -> dict[str, dict[str, Any]]:
        return aggregate(self.runs)

    def to_json(self) -> dict[str, Any]:
        return {
            "meta": self.meta,
            "aggregates": self.aggregates,
            "matrix": per_scenario_matrix(self.runs),
            "failures": failure_breakdown(self.runs),
            "runs": [run.to_dict() for run in self.runs],
        }

    def write(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path


def _simulated_human(request: ApprovalRequest, state: DiagnosisState) -> bool:
    """Stand-in for the on-call engineer at the approval gate.

    Approves only single-pod recreation, and only when the diagnosis named a
    root cause where that is a safe action. Everything else is declined — which
    is the behaviour the security section claims.
    """
    action = request.action
    safe_for = {"pod_restart", "dns_resolution", "auth_failure", "oom_killed"}
    predicted = state.report.root_cause if state.report else ""
    if action.tier == "write_l1" and "delete pod" in (action.command or ""):
        return predicted in safe_for
    return False


def run_evaluation(config: EvalConfig, settings: rdconfig.Settings | None = None) -> EvalResult:
    settings = settings or rdconfig.Settings.from_env()
    settings.approval_required = config.approval_required
    if config.backend:
        settings.backend = config.backend

    scenario_dir = settings.resolve(settings.scenarios_dir)
    cluster = (
        SimCluster(settings.namespace, settings.instance) if settings.backend == "sandbox" else None
    )
    lab = FaultLab(cluster, scenario_dir) if cluster is not None else None
    scenario_ids = config.scenario_ids or (lab.ids() if lab else [])

    llm = build_llm(
        config.policy,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        max_output_tokens=settings.llm_max_output_tokens,
        cassette_path=str(settings.resolve("eval/cassettes/reference.jsonl")),
    )

    runs: list[ScenarioRun] = []
    started = time.time()
    for key in config.variants:
        variant = VARIANTS[key]
        for scenario_id in scenario_ids:
            assert lab is not None
            scenario = lab.get(scenario_id)
            for run_index in range(config.runs):
                lab.reset()
                lab.inject(scenario_id)
                lab.wait_stable(scenario, settle=settings.backend == "real")
                run_settings = _variant_settings(settings, variant, config)
                ctx = ToolContext(settings=run_settings, cluster=cluster)
                reasoner = _reasoner(variant, llm)
                graph = DiagnosisGraph(
                    ctx,
                    variant,
                    reasoner=reasoner,
                    approver=_simulated_human if config.approval_required else None,
                    settings=run_settings,
                )
                graph_run = graph.run(scenario.alert)
                lab.recover(scenario_id)
                clean = lab.is_clean()
                state = graph_run.state.model_dump()
                state["elapsed_seconds"] = graph_run.state.elapsed_seconds
                state["cost_usd"] = graph_run.state.cost_usd
                runs.append(
                    score_run(
                        scenario=scenario,
                        variant_key=variant.key,
                        run_index=run_index,
                        report=graph_run.report,
                        trace=graph_run.trace,
                        state=state,
                        audit=graph_run.registry.audit.to_dict(),
                        budget=graph_run.registry.budget.to_dict(),
                        clean_after_recover=clean,
                    )
                )

    meta = {
        "version": MVP,
        "started_at": time.time(),
        "duration_seconds": round(time.time() - started, 2),
        "variants": {k: VARIANTS[k].description for k in config.variants},
        "runs_per_cell": config.runs,
        "scenarios": scenario_ids,
        "held_out": [s.id for s in (lab.held_out() if lab else [])],
        "policy": config.policy,
        "model": config.model or settings.llm_model,
        "backend": settings.backend,
        "approval_required": config.approval_required,
        "budget": {
            "max_steps": settings.max_steps,
            "max_tool_calls": settings.max_tool_calls,
            "max_seconds": settings.max_seconds,
        },
        "budget_pressure": config.budget_pressure,
        "notes": config.notes,
        "llm_name": getattr(llm, "name", "reference-policy") if llm else "reference-policy",
        "cassette": isinstance(llm, CassetteLLM),
        "prompt_version": MVP,
    }
    return EvalResult(runs=runs, meta=meta)


def _variant_settings(
    settings: rdconfig.Settings, variant: Variant, config: EvalConfig
) -> rdconfig.Settings:
    clone = rdconfig.Settings(**settings.__dict__)
    clone.approval_required = config.approval_required
    if config.budget_pressure:
        clone.max_tool_calls = min(clone.max_tool_calls, 3)
    if variant.key == "A":
        # Group A must not even have tools available; the graph honours
        # max_rounds=0, so the only difference left is budget bookkeeping.
        clone.max_tool_calls = 0
    return clone


def _reasoner(variant: Variant, llm):
    if llm is None:
        return ReferenceReasoner(variant)
    from agent.llm_reasoner import LLMReasoner

    return LLMReasoner(variant, llm)

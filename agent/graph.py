"""The diagnosis graph.

    triage -> plan -> collect -> evaluate -(loop)-> report -> approval -> execute -> verify

The engine is intentionally small and explicit rather than a framework import:
the loop, the budgets, the trace and the approval gate are the parts being
evaluated, so they should be readable in one sitting. The node contract is the
same one a LangGraph implementation would use (typed state, node functions,
checkpointable state, interrupt at the approval gate), and the state object is
serialisable for exactly that reason.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import rdconfig
from agent.reference import ReferenceReasoner
from agent.state import (
    ROOT_CAUSES,
    ApprovalRequest,
    DiagnosisReport,
    DiagnosisState,
    ExecutionResult,
    SuggestedAction,
)
from agent.variants import Variant
from tools.base import ToolRegistry
from tools.context import ToolContext
from tools.safety import detect_injection

Approver = Callable[[ApprovalRequest, DiagnosisState], bool]


@dataclass
class GraphRun:
    state: DiagnosisState
    registry: ToolRegistry
    trace: list[dict[str, Any]] = field(default_factory=list)

    @property
    def report(self) -> DiagnosisReport | None:
        return self.state.report

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state.model_dump(),
            "audit": self.registry.audit.to_dict(),
            "budget": self.registry.budget.to_dict(),
            "trace": self.trace,
        }


class DiagnosisGraph:
    def __init__(
        self,
        ctx: ToolContext,
        variant: Variant,
        reasoner: Any | None = None,
        approver: Approver | None = None,
        settings: rdconfig.Settings | None = None,
    ) -> None:
        self.ctx = ctx
        self.variant = variant
        self.settings = settings or ctx.settings
        self.reasoner = reasoner or ReferenceReasoner(variant)
        self.approver = approver

    # -- public API ------------------------------------------------------
    def run(self, alert_text: str, diagnosis_id: str | None = None) -> GraphRun:
        state = DiagnosisState(
            diagnosis_id=diagnosis_id or f"dx-{uuid.uuid4().hex[:10]}",
            alert_text=alert_text,
            variant=self.variant.key,
        )
        registry = self.ctx.build_registry(include_kb=self.variant.use_kb)
        run = GraphRun(state=state, registry=registry)
        try:
            self._triage(run, alert_text)
            kb_categories = self._plan(run)
            self._collect_loop(run, kb_categories)
            self._report(run)
            self._approval(run)
        except Exception as exc:  # a crash must still leave a trace
            state.status = "error"
            state.error = f"{type(exc).__name__}: {exc}"
        finally:
            state.finished_at = time.time()
            state.audit = registry.audit.to_dict()
            state.budget = registry.budget.to_dict()
            state.budget["exhausted"] = registry.budget.exhausted
            if state.report is not None:
                state.report.budget_exhausted = state.report.budget_exhausted or (
                    registry.budget.exhausted
                )
        return run

    # -- nodes -----------------------------------------------------------
    def _triage(self, run: GraphRun, alert_text: str) -> None:
        run.registry.budget.charge_step()
        state = run.state
        if getattr(self.reasoner, "is_llm", False):
            state.triage = self.reasoner.triage(
                alert_text,
                self.settings.namespace,
                self.settings.instance,
                catalog=run.registry.catalog(),
            )
        else:
            state.triage = self.reasoner.triage(
                alert_text, self.settings.namespace, self.settings.instance
            )
        state.injection_hits = sorted(set(state.injection_hits) | set(detect_injection(alert_text)))

    def _plan(self, run: GraphRun) -> list[str]:
        run.registry.budget.charge_step()
        state = run.state
        kb_categories: list[str] = []
        if self.variant.use_kb:
            query = " ".join(
                [state.triage.symptom_class, *state.triage.keywords, state.alert_text[:200]]
            )
            result = run.registry.call("kb_search", query=query, top_k=4)
            self._absorb(run, result)
            kb_categories = [
                item["category"]
                for item in (result.data or [])
                if isinstance(item, dict) and item.get("category")
            ]
        if getattr(self.reasoner, "is_llm", False):
            state.hypotheses = self.reasoner.plan(state, kb_categories, run.registry.catalog())
        else:
            state.hypotheses = self.reasoner.plan(state, kb_categories, run.registry.catalog())
        return kb_categories

    def _collect_loop(self, run: GraphRun, kb_categories: list[str]) -> None:
        state = run.state
        registry = run.registry
        already: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
        rounds = 0
        while rounds < self.variant.max_rounds and not registry.budget.exhausted:
            registry.budget.charge_step()
            probes = self.reasoner.choose_probes(
                state,
                registry.catalog(),
                rounds,
                already,
                self.variant.probes_per_round,
            )
            if not probes:
                break
            for probe in probes:
                if registry.budget.exhausted:
                    break
                tool = probe["tool"]
                args = probe.get("args", {})
                already.add((tool, tuple(sorted((k, str(v)) for k, v in args.items()))))
                result = registry.call(tool, **args)
                self._absorb(run, result)
            state.hypotheses = getattr(self.reasoner, "refresh", lambda s, h: h)(
                state, state.hypotheses
            )
            self._evaluate(run)
            rounds += 1
            state.rounds = rounds
            if not self.variant.iterative:
                break
            # One confirmation round after convergence, then stop.
            if self.reasoner.converged(state) and rounds >= 2:
                break
        state.rounds = rounds

    def _evaluate(self, run: GraphRun) -> None:
        run.registry.budget.charge_step()
        self.reasoner.evaluate(run.state)

    def _report(self, run: GraphRun) -> None:
        run.registry.budget.charge_step()
        state = run.state
        if getattr(self.reasoner, "is_llm", False):
            report = self.reasoner.report(state, run.registry.trace, run.registry.catalog())
        else:
            report = self.reasoner.report(state, run.registry.trace)
        if report.root_cause not in ROOT_CAUSES:
            fallback = (
                max(state.hypotheses, key=lambda h: h.confidence).category
                if state.hypotheses
                else "pod_restart"
            )
            report.uncertainty.append(
                f"模型给出的根因 '{report.root_cause}' 不在允许的类别集合内，"
                f"已回退到最高置信度假设 {fallback}"
            )
            report.root_cause = fallback
        report.iterations = state.rounds
        state.report = report

    def _approval(self, run: GraphRun) -> None:
        state = run.state
        if state.report is None:
            return
        # Only L1 actions are executable, so only L1 actions are parked at the
        # gate. L2 stays advisory in this MVP: it shows up in the report with a
        # concrete command, and nothing more.
        writes = [a for a in state.report.suggested_actions if a.tier == "write_l1"]
        if not writes:
            return
        request = ApprovalRequest(id=f"ap-{uuid.uuid4().hex[:8]}", action=writes[0])
        state.approval = request
        if not self.settings.approval_required:
            request.status = "approved"
            request.decided_by = "auto (approval gate disabled)"
            self._execute(run, request)
            return
        if self.approver is None:
            state.status = "waiting_approval"
            return
        approved = bool(self.approver(request, state))
        request.status = "approved" if approved else "denied"
        request.decided_by = "approver"
        request.decided_at = time.time()
        if approved:
            self._execute(run, request)
        else:
            state.status = "denied"

    def _execute(self, run: GraphRun, request: ApprovalRequest) -> None:
        """Actuator path. Only reachable with an explicit approval."""
        state = run.state
        registry = run.registry
        call = plan_write_call(request.action)
        if call is None:
            state.status = "ok"
            state.execution = ExecutionResult(
                action=request.action,
                call_id="",
                ok=False,
                output="action is advisory (L2) in this MVP; nothing was executed",
            )
            return
        tool, args = call
        result = registry.call(tool, __approved__=True, **args)
        self._absorb(run, result)
        execution = ExecutionResult(
            action=request.action,
            call_id=result.call_id,
            ok=result.ok,
            output=result.raw or result.summary,
        )
        verified, notes = self._verify(run)
        execution.verified = verified
        execution.verification = notes
        state.execution = execution
        state.status = "approved_executed" if result.ok else "error"

    def _verify(self, run: GraphRun) -> tuple[bool, list[str]]:
        registry = run.registry
        notes: list[str] = []
        pods = registry.call("k8s_get_pods")
        self._absorb(run, pods)
        ready = [p for p in (pods.data or []) if p.get("ready")]
        notes.append(f"ready pods: {len(ready)}/{(pods.data or []) and len(pods.data)}")
        master = f"{self.settings.instance}-0"
        info = registry.call("redis_info", pod=master, section="replication")
        self._absorb(run, info)
        link_ok = "master_link_status: up" in (info.raw or "") or "role: master" in (info.raw or "")
        notes.append(f"{master} replication: {'healthy' if link_ok else 'degraded'}")
        return bool(ready) and link_ok, notes

    # -- helpers ---------------------------------------------------------
    def _absorb(self, run: GraphRun, result) -> None:
        run.state.record_signals(set(result.signals))
        run.state.tool_calls.append(result.to_trace())
        run.trace.append(result.to_trace())
        if result.raw:
            hits = detect_injection(result.raw)
            if hits:
                run.state.injection_hits = sorted(set(run.state.injection_hits) | set(hits))
        run.state.token_usage = {
            "prompt_tokens": run.state.token_usage.get("prompt_tokens", 0),
            "completion_tokens": run.state.token_usage.get("completion_tokens", 0),
            "total_tokens": run.state.token_usage.get("total_tokens", 0),
        }


def plan_write_call(action: SuggestedAction) -> tuple[str, dict[str, Any]] | None:
    """Map a suggested action to an actuator call, or ``None`` when advisory.

    The mapping is structural (tool + object name), never free text: the model
    cannot smuggle a command through the approval gate.
    """
    if action.tier != "write_l1":
        return None
    match = re.search(r"\bdemo-\d+\b", action.command or "")
    if "delete pod" in (action.command or "") and match:
        return "k8s_delete_pod", {"pod": match.group(0)}
    return None


def execute_approved_action(
    ctx: ToolContext, pod: str, settings: rdconfig.Settings | None = None
) -> dict[str, Any]:
    """Execute a previously approved L1 action, then re-verify.

    Used by the API when a human approves the interrupt; the graph's diagnosis
    loop has already finished by then, so this is a standalone actuator call.
    """
    settings = settings or ctx.settings
    registry = ctx.build_registry(include_kb=False)
    result = registry.call("k8s_delete_pod", __approved__=True, pod=pod)
    verification = registry.call("k8s_get_pods")
    info = registry.call("redis_info", pod=f"{settings.instance}-0", section="replication")
    return {
        "ok": result.ok,
        "call": result.to_trace(),
        "verification": {
            "pods": verification.to_trace(),
            "replication": info.to_trace(),
            "ready": bool([p for p in (verification.data or []) if p.get("ready")]),
        },
    }

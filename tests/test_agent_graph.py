from __future__ import annotations

from agent.graph import DiagnosisGraph, execute_approved_action, plan_write_call
from agent.reference import ReferenceReasoner
from agent.state import SuggestedAction
from agent.variants import VARIANTS


def _run(ctx, settings, scenario, variant="D", approver=None):
    reasoner = ReferenceReasoner(VARIANTS[variant])
    graph = DiagnosisGraph(
        ctx, VARIANTS[variant], reasoner=reasoner, approver=approver, settings=settings
    )
    return graph.run(scenario.alert)


def test_full_pipeline_finds_the_root_cause(lab, ctx, settings) -> None:
    scenario = lab.inject("S02")
    run = _run(ctx, settings, scenario)
    assert run.report is not None
    assert run.report.root_cause == "oom_killed"
    assert run.report.confidence >= 0.6
    assert run.report.evidence, "a conclusion must cite evidence"


def test_evidence_always_traces_back_to_a_tool_call(lab, ctx, settings) -> None:
    scenario = lab.inject("S11")
    run = _run(ctx, settings, scenario)
    trace_by_id = {call["call_id"]: call for call in run.trace}
    for ref in run.report.evidence:
        entry = trace_by_id.get(ref.call_id)
        assert entry is not None, f"evidence cites an unknown call: {ref.call_id}"
        assert ref.signal in entry["signals"], "evidence signal must come from that call"


def test_variant_a_has_no_tools(lab, ctx, settings) -> None:
    scenario = lab.inject("S03")
    run = _run(ctx, settings, scenario, variant="A")
    assert run.registry.audit.total == 0
    assert run.state.observed_signals == []
    assert run.report is not None  # it still answers, just without evidence


def test_budget_stops_collection_without_crashing(lab, ctx, settings) -> None:
    scenario = lab.inject("S14")
    settings.max_tool_calls = 3
    run = _run(ctx, settings, scenario)
    assert run.registry.budget.tool_calls <= 3
    assert run.state.status in {"ok", "denied"}
    assert run.report is not None


def test_approval_gate_blocks_until_a_human_decides(lab, ctx, settings) -> None:
    scenario = lab.inject("S02")
    run = _run(ctx, settings, scenario)
    assert run.state.status == "waiting_approval"
    assert run.state.approval is not None
    assert run.state.approval.action.tier == "write_l1"
    assert run.state.execution is None, "nothing may run before approval"


def test_approved_action_executes_and_verifies(lab, ctx, settings) -> None:
    scenario = lab.inject("S02")
    decisions: list[str] = []

    def approver(request, state):
        decisions.append(request.action.command)
        return True

    run = _run(ctx, settings, scenario, approver=approver)
    assert decisions, "the approver must be consulted"
    assert run.state.execution is not None
    assert run.state.execution.ok is True
    assert run.state.execution.verified is True
    assert run.state.status == "approved_executed"


def test_denied_action_is_not_executed(lab, ctx, settings) -> None:
    scenario = lab.inject("S02")
    run = _run(ctx, settings, scenario, approver=lambda *_: False)
    assert run.state.status == "denied"
    assert run.state.execution is None


def test_l2_actions_are_advisory_only() -> None:
    action = SuggestedAction(
        action="提高 memory limit",
        tier="write_l2",
        command="kubectl patch statefulset demo -n demo -p ...",
        risk="high",
    )
    assert plan_write_call(action) is None


def test_l1_action_maps_to_a_structured_call() -> None:
    action = SuggestedAction(
        action="重建 Pod",
        tier="write_l1",
        command="kubectl delete pod redis-demo-0 -n demo",
        risk="medium",
    )
    assert plan_write_call(action) == ("k8s_delete_pod", {"pod": "redis-demo-0"})


def test_standalone_actuator_reverifies(lab, ctx, settings) -> None:
    lab.inject("S02")
    outcome = execute_approved_action(ctx, "redis-demo-0", settings)
    assert outcome["ok"] is True
    assert outcome["verification"]["ready"] is True

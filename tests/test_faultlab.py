"""Every scenario must inject, be observable, and recover to a clean state."""

from __future__ import annotations

import pytest

from agent.graph import DiagnosisGraph
from agent.reference import ReferenceReasoner
from agent.variants import VARIANTS


def test_all_sixteen_scenarios_present(lab) -> None:
    assert lab.ids() == [f"S{i:02d}" for i in range(1, 17)]


@pytest.mark.parametrize("scenario_id", [f"S{i:02d}" for i in range(1, 17)])
def test_scenario_injects_and_recovers_cleanly(lab, scenario_id: str) -> None:
    lab.reset()
    assert lab.is_clean(), "baseline must be clean"
    lab.inject(scenario_id)
    assert not lab.is_clean(), f"{scenario_id} produced no observable fault"
    lab.recover(scenario_id)
    assert lab.is_clean(), f"{scenario_id} left the cluster dirty after recovery"


@pytest.mark.parametrize("scenario_id", [f"S{i:02d}" for i in range(1, 17)])
def test_required_signals_are_reachable(lab, ctx, settings, scenario_id: str) -> None:
    """The scenario's ground truth must be observable through the tool layer."""
    lab.reset()
    scenario = lab.inject(scenario_id)
    settings.max_tool_calls = 60
    settings.max_steps = 20
    context = ctx
    context.settings = settings
    reasoner = ReferenceReasoner(VARIANTS["D"])
    run = DiagnosisGraph(context, VARIANTS["D"], reasoner=reasoner, settings=settings).run(
        scenario.alert
    )
    observed = set(run.state.observed_signals)
    missing = set(scenario.required_signals) - observed
    assert not missing, f"{scenario_id}: required signals never observed: {missing}"


def test_held_out_scenarios_are_the_documented_four(lab) -> None:
    assert {s.id for s in lab.held_out()} == {"S04", "S11", "S13", "S15"}

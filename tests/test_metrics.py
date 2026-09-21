from __future__ import annotations

from agent.state import DiagnosisReport, EvidenceRef
from eval.metrics import aggregate, failure_breakdown, per_scenario_matrix, score_run
from faultlab.schema import Scenario


def _scenario() -> Scenario:
    return Scenario(
        id="S99",
        name="test",
        category="oom_killed",
        alert="alert",
        required_signals=["pod_oom_killed"],
    )


def _state(signals, hypotheses=None, tokens=0):
    return {
        "observed_signals": signals,
        "hypotheses": hypotheses or [],
        "token_usage": {"total_tokens": tokens},
        "elapsed_seconds": 1.5,
        "status": "ok",
    }


def test_score_marks_top1_and_evidence_recall() -> None:
    trace = [
        {"call_id": "c1", "tool": "k8s_get_pods", "signals": ["pod_oom_killed"], "output": "x"}
    ]
    report = DiagnosisReport(
        root_cause="oom_killed",
        summary="s",
        confidence=0.9,
        evidence=[
            EvidenceRef(call_id="c1", tool="k8s_get_pods", signal="pod_oom_killed", quote="x")
        ],
    )
    run = score_run(
        scenario=_scenario(),
        variant_key="D",
        run_index=0,
        report=report,
        trace=trace,
        state=_state(["pod_oom_killed"]),
        audit={"total": 1, "unauthorized_attempts": 0},
        budget={"steps": 4},
    )
    assert run.top1 is True
    assert run.top3_hit is True
    assert run.evidence_recall_seen == 1.0
    assert run.evidence_recall_cited == 1.0
    assert run.hallucination_rate == 0.0


def test_unsupported_evidence_counts_as_hallucination() -> None:
    trace = [{"call_id": "c1", "tool": "k8s_get_pods", "signals": [], "output": ""}]
    report = DiagnosisReport(
        root_cause="oom_killed",
        summary="s",
        confidence=0.9,
        evidence=[
            EvidenceRef(call_id="c1", tool="k8s_get_pods", signal="pod_oom_killed", quote="x"),
            EvidenceRef(call_id="ghost", tool="k8s_get_pods", signal="pod_restart", quote="y"),
        ],
    )
    run = score_run(
        scenario=_scenario(),
        variant_key="D",
        run_index=0,
        report=report,
        trace=trace,
        state=_state(["pod_oom_killed"]),
        audit={"total": 1, "unauthorized_attempts": 0},
        budget={"steps": 4},
    )
    assert run.unsupported_refs == 2
    assert run.hallucination_rate == 1.0


def test_aggregate_and_failure_classification() -> None:
    trace = [{"call_id": "c1", "tool": "k8s_get_pods", "signals": [], "output": ""}]
    good = score_run(
        scenario=_scenario(),
        variant_key="D",
        run_index=0,
        report=DiagnosisReport(root_cause="oom_killed", summary="", confidence=0.9),
        trace=trace,
        state=_state(["pod_oom_killed"]),
        audit={"total": 2, "unauthorized_attempts": 0},
        budget={"steps": 3},
    )
    bad = score_run(
        scenario=_scenario(),
        variant_key="D",
        run_index=1,
        report=DiagnosisReport(root_cause="disk_full", summary="", confidence=0.4),
        trace=trace,
        state=_state([]),
        audit={"total": 1, "unauthorized_attempts": 0},
        budget={"steps": 3},
    )
    agg = aggregate([good, bad])["D"]
    assert agg["runs"] == 2
    assert agg["top1"] == 0.5
    kinds = {row["kind"] for row in failure_breakdown([good, bad])}
    assert kinds == {"证据不足"}


def test_grounded_top1_and_repeat_matrix() -> None:
    """A correct answer with no cited evidence is not 'grounded'."""
    lucky = score_run(
        scenario=_scenario(),
        variant_key="D",
        run_index=0,
        report=DiagnosisReport(root_cause="oom_killed", summary="", confidence=0.5),
        trace=[],
        state=_state(["pod_oom_killed"]),
        audit={"total": 0, "unauthorized_attempts": 0},
        budget={"steps": 1},
    )
    assert lucky.top1 is True
    assert lucky.grounded is False
    assert lucky.unreferenced is True

    agg = aggregate([lucky])["D"]
    assert agg["top1"] == 1.0
    assert agg["grounded_top1"] == 0.0
    assert agg["unreferenced_conclusions"] == 1.0
    assert matrix_of([lucky])["S99"]["D"] == {"hits": 1, "runs": 1}


def matrix_of(runs):
    return per_scenario_matrix(runs)

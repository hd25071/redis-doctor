"""Evaluation harness: ablation runs, metrics, reports."""

from eval.harness import EvalConfig, EvalResult, run_evaluation
from eval.metrics import ScenarioRun, aggregate, score_run

__all__ = [
    "EvalConfig",
    "EvalResult",
    "ScenarioRun",
    "aggregate",
    "run_evaluation",
    "score_run",
]

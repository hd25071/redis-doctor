"""Ablation variants (the experimental design of the evaluation)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Variant:
    key: str
    name: str
    use_tools: bool
    use_kb: bool
    iterative: bool
    max_rounds: int
    probes_per_round: int
    description: str


VARIANTS: dict[str, Variant] = {
    "A": Variant(
        key="A",
        name="LLM only",
        use_tools=False,
        use_kb=False,
        iterative=False,
        max_rounds=0,
        probes_per_round=0,
        description="标题告警文本直接给模型，没有任何工具与手册；衡量纯语言先验。",
    ),
    "B": Variant(
        key="B",
        name="Read-only tools (single pass)",
        use_tools=True,
        use_kb=False,
        iterative=False,
        max_rounds=1,
        probes_per_round=6,
        description="一轮 ReAct：按告警关键词选一批只读工具，看到结果直接给结论。",
    ),
    "C": Variant(
        key="C",
        name="+ Knowledge base",
        use_tools=True,
        use_kb=True,
        iterative=False,
        max_rounds=1,
        probes_per_round=6,
        description="在 B 的基础上加入排障手册检索，验证 RAG 的增益。",
    ),
    "D": Variant(
        key="D",
        name="+ Hypothesis verification loop",
        use_tools=True,
        use_kb=True,
        iterative=True,
        max_rounds=4,
        probes_per_round=4,
        description="完整版：triage → plan → collect → evaluate 循环，假设驱动地补证据。",
    ),
}

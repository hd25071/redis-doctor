"""LLM-backed reasoner.

Same contract as :class:`agent.reference.ReferenceReasoner`, so the ablation
compares pipelines rather than code paths. Anything the LLM returns is
validated before it reaches the state:

* ``root_cause`` must be inside the taxonomy (the graph falls back otherwise);
* every evidence reference must name a ``call_id`` that exists *and* a signal
  that call actually produced — otherwise it is counted as an unsupported claim.
"""

from __future__ import annotations

import json
import time
from typing import Any

import rdconfig
from agent import prompts
from agent.llm import LLM
from agent.state import (
    ROOT_CAUSES,
    DiagnosisReport,
    DiagnosisState,
    EvidenceRef,
    Hypothesis,
    RuledOut,
    SuggestedAction,
    Triage,
)
from agent.variants import Variant


def _confidence(value: Any) -> float:
    """Never let a malformed number from the model crash a diagnosis."""
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.3


class LLMReasoner:
    is_llm = True

    def __init__(self, variant: Variant, llm: LLM) -> None:
        self.variant = variant
        self.llm = llm
        self.name = getattr(llm, "name", "llm")

    # -- helpers ---------------------------------------------------------
    def _ask(
        self, state: DiagnosisState, user: str, expect_json: bool = True
    ) -> dict[str, Any] | None:
        response = self.llm.complete(prompts.SYSTEM, user, expect_json=expect_json)
        usage = state.token_usage or {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        usage["prompt_tokens"] += response.prompt_tokens
        usage["completion_tokens"] += response.completion_tokens
        usage["total_tokens"] += response.prompt_tokens + response.completion_tokens
        state.token_usage = usage
        state.cost_usd = round(state.cost_usd + response.cost_usd, 6)
        if expect_json and response.parsed is None:
            self._record_unparsed(user, response)
            # A parse failure must not look like a confident answer: the report
            # step falls back to ``unresolved`` and this counter feeds the
            # evaluation so failures are visible instead of being scored.
            state.notes = "json_parse_failure"
        return response.parsed

    @staticmethod
    def _record_unparsed(user: str, response: Any) -> None:
        """Keep the raw text when a JSON step does not parse.

        Without this the only symptom is a silent fallback to the default
        category, which is impossible to diagnose after the fact.
        """
        try:
            path = rdconfig.REPO_ROOT / "eval/results/llm-raw.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "at": time.time(),
                            "prompt_chars": len(user),
                            "response_chars": len(response.text),
                            "text": response.text[:4000],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception:  # diagnostics must never break a run
            pass

    def _state_json(self, state: DiagnosisState) -> dict[str, Any]:
        return {
            "alert": state.alert_text,
            "triage": state.triage.model_dump(),
            "hypotheses": [h.model_dump() for h in state.hypotheses],
            "observed_signals": state.observed_signals,
            "tool_calls": [
                {
                    "call_id": call.get("call_id"),
                    "tool": call.get("tool"),
                    "args": call.get("args"),
                    "signals": call.get("signals"),
                    "summary": call.get("summary"),
                }
                for call in state.tool_calls
            ],
            "rounds": state.rounds,
        }

    # -- nodes -----------------------------------------------------------
    def triage(
        self, alert_text: str, namespace: str, instance: str, catalog: list[dict] | None = None
    ) -> Triage:
        parsed = self._ask(
            DiagnosisState(diagnosis_id="triage", alert_text=alert_text),
            prompts.triage_prompt(alert_text, namespace, instance, catalog or []),
        )
        if not parsed:
            return Triage(namespace=namespace, instance=instance, notes="LLM triage failed")
        try:
            return Triage(**{k: v for k, v in parsed.items() if k in Triage.model_fields})
        except Exception:
            return Triage(namespace=namespace, instance=instance, notes="LLM triage invalid")

    def plan(
        self, state: DiagnosisState, kb_categories: list[str], catalog: list[dict]
    ) -> list[Hypothesis]:
        payload = self._state_json(state)
        payload["kb_categories"] = kb_categories
        parsed = self._ask(state, prompts.plan_prompt(payload, catalog))
        items = (parsed or {}).get("hypotheses") or []
        hypotheses: list[Hypothesis] = []
        for index, item in enumerate(items):
            category = item.get("category")
            if category not in ROOT_CAUSES:
                continue
            hypotheses.append(
                Hypothesis(
                    id=item.get("id") or f"h{index + 1}",
                    category=category,
                    statement=item.get("statement", ""),
                    confidence=float(item.get("confidence", 0.2)),
                    needs=[str(n) for n in item.get("needs", [])],
                )
            )
        if not hypotheses:
            hypotheses = [
                Hypothesis(
                    id="h1",
                    category="pod_restart",
                    statement="LLM 未给出可用假设，退化为重启类假设",
                    confidence=0.2,
                )
            ]
        return hypotheses

    def choose_probes(
        self,
        state: DiagnosisState,
        catalog: list[dict],
        round_index: int,
        already: set[tuple[str, tuple[tuple[str, str], ...]]],
        max_probes: int,
    ) -> list[dict]:
        if max_probes <= 0:
            return []
        parsed = self._ask(
            state, prompts.collect_prompt(self._state_json(state), catalog, max_probes)
        )
        probes: list[dict] = []
        allowed = {entry["name"] for entry in catalog}
        for item in (parsed or {}).get("probes") or []:
            tool = item.get("tool")
            if tool not in allowed:
                continue
            args = item.get("args") or {}
            key = (tool, tuple(sorted((k, str(v)) for k, v in args.items())))
            if key in already:
                continue
            probes.append({"tool": tool, "args": args})
            if len(probes) >= max_probes:
                break
        return probes

    def evaluate(self, state: DiagnosisState) -> None:
        parsed = self._ask(state, prompts.evaluate_prompt(self._state_json(state)))
        updates = {item.get("id"): item for item in (parsed or {}).get("hypotheses") or []}
        for hypothesis in state.hypotheses:
            item = updates.get(hypothesis.id)
            if not item:
                continue
            status = item.get("status")
            if status in {"supported", "refuted", "unresolved", "open"}:
                hypothesis.status = status
            hypothesis.confidence = float(item.get("confidence", hypothesis.confidence))
            hypothesis.supporting = [str(s) for s in item.get("supporting", [])]
            hypothesis.contradicting = [str(s) for s in item.get("contradicting", [])]
        state.notes = (parsed or {}).get("next_focus", "")

    def converged(self, state: DiagnosisState) -> bool:
        supported = [h for h in state.hypotheses if h.status == "supported"]
        return len(supported) == 1 and supported[0].confidence >= 0.7

    def report(
        self, state: DiagnosisState, trace: list[dict], catalog: list[dict] | None = None
    ) -> DiagnosisReport:
        parsed = self._ask(state, prompts.report_prompt(self._state_json(state), trace)) or {}
        report, invalid = self._build_report(parsed, state, trace)
        if invalid:
            # The model cited calls or signals that do not exist. Ask once more
            # with the offending citations spelled out; keep whichever version
            # has fewer unsupported references.
            parsed_retry = (
                self._ask(state, prompts.citation_repair_prompt(parsed, invalid, trace)) or {}
            )
            repaired, invalid_after = self._build_report(parsed_retry, state, trace)
            if len(invalid_after) <= len(invalid):
                report = repaired
                invalid = invalid_after
            state.notes = (
                f"引用校验重试：修正后仍未对齐 {len(invalid)} 条证据"
                if invalid
                else "引用校验重试：全部证据已对齐到工具调用"
            )
        return report

    def _build_report(
        self, parsed: dict[str, Any], state: DiagnosisState, trace: list[dict]
    ) -> tuple[DiagnosisReport, list[str]]:
        """Turn the model's JSON into a report, listing unsupported citations."""
        trace_by_id = {entry["call_id"]: entry for entry in trace}
        evidence: list[EvidenceRef] = []
        invalid: list[str] = []
        for item in parsed.get("evidence") or []:
            call_id = str(item.get("call_id", ""))
            signal = str(item.get("signal", ""))
            entry = trace_by_id.get(call_id)
            if entry is None or signal not in set(entry.get("signals", [])):
                # Keep the citation so the hallucination metric can see it, but
                # mark it clearly as unsupported.
                invalid.append(f"{call_id}:{signal or 'missing-signal'}")
                evidence.append(
                    EvidenceRef(
                        call_id=call_id,
                        tool=str(item.get("tool", "unknown")),
                        signal=signal or "unsupported",
                        quote=str(item.get("quote", ""))[:300],
                        source="UNSUPPORTED (no matching tool call)",
                    )
                )
                continue
            evidence.append(
                EvidenceRef(
                    call_id=call_id,
                    tool=entry["tool"],
                    signal=signal,
                    quote=str(item.get("quote", ""))[:300],
                    source=entry.get("summary", ""),
                )
            )
        ruled_out = [
            RuledOut(
                category=str(item.get("category", "")),
                reason=str(item.get("reason", "")),
                evidence_call_ids=[str(c) for c in item.get("evidence_call_ids", [])],
            )
            for item in parsed.get("ruled_out") or []
            if str(item.get("category", "")) in ROOT_CAUSES
        ]
        actions: list[SuggestedAction] = []
        for item in parsed.get("suggested_actions") or []:
            tier = item.get("tier", "read")
            risk = item.get("risk", "low")
            actions.append(
                SuggestedAction(
                    action=str(item.get("action", "")),
                    tier=tier if tier in {"read", "write_l1", "write_l2"} else "read",
                    target=str(item.get("target", "")),
                    risk=risk if risk in {"low", "medium", "high"} else "medium",
                    command=str(item.get("command", "")),
                    rationale=str(item.get("rationale", "")),
                )
            )
        return (
            DiagnosisReport(
                root_cause=str(parsed.get("root_cause", "unresolved")),
                summary=str(parsed.get("summary", "")),
                confidence=_confidence(parsed.get("confidence")),
                evidence=evidence,
                ruled_out=ruled_out,
                suggested_actions=actions,
                uncertainty=[str(u) for u in parsed.get("uncertainty") or []],
                iterations=state.rounds,
            ),
            invalid,
        )

    def refresh(self, state: DiagnosisState, hypotheses: list[Hypothesis]) -> list[Hypothesis]:
        return hypotheses

    def describe(self) -> str:
        return json.dumps({"reasoner": self.name}, ensure_ascii=False)

"""Tool registry, tiers, budgets and the audit trail.

Design notes
------------
* Tiers are the *only* place write capability is expressed. The diagnosis loop
  receives ``catalog(allow_write=False)``, so an LLM cannot even see the write
  tools, let alone call one.
* Every call — including refused ones — produces a :class:`ToolResult` and an
  audit entry. ``CallAudit.unauthorized_attempts`` must stay 0; it is one of the
  published evaluation metrics.
* Results are sanitised centrally (secret redaction, truncation with an explicit
  marker) so no individual tool can forget it.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

Tier = Literal["read", "write_l1", "write_l2"]


@dataclass
class ToolResult:
    """Outcome of one tool call."""

    tool: str
    args: dict[str, Any]
    ok: bool
    data: Any = None
    summary: str = ""
    raw: str = ""
    signals: set[str] = field(default_factory=set)
    truncated: bool = False
    redacted: bool = False
    duration_ms: int = 0
    error: str | None = None
    blocked: bool = False
    block_reason: str | None = None
    call_id: str = ""
    at: float = field(default_factory=time.time)

    def to_trace(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool": self.tool,
            "args": self.args,
            "ok": self.ok,
            "blocked": self.blocked,
            "block_reason": self.block_reason,
            "summary": self.summary,
            "signals": sorted(self.signals),
            "truncated": self.truncated,
            "redacted": self.redacted,
            "duration_ms": self.duration_ms,
            "error": self.error,
            # Kept in the trace on purpose: the trajectory view and the
            # evidence quotes must point at the exact bytes the model saw.
            "output": self.raw[:4000],
            "at": self.at,
        }

    def as_prompt_block(self) -> str:
        """The exact text handed to the model, wrapped as untrusted data."""
        body = self.raw or self.summary
        return (
            f'<untrusted_tool_output tool="{self.tool}" call_id="{self.call_id}">\n'
            f"{body}\n"
            "</untrusted_tool_output>"
        )


@dataclass
class ToolSpec:
    name: str
    tier: Tier
    description: str
    params: dict[str, str]
    fn: Callable[..., ToolResult]
    max_result_chars: int = 6000

    def signature(self) -> str:
        args = ", ".join(f"{k}: {v}" for k, v in self.params.items()) or ""
        return f"{self.name}({args})"

    def catalog_entry(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tier": self.tier,
            "description": self.description,
            "params": self.params,
        }


@dataclass
class CallAudit:
    total: int = 0
    blocked_unauthorized: int = 0
    blocked_unapproved_write: int = 0
    blocked_policy: int = 0
    blocked_budget: int = 0
    failed: int = 0
    by_tool: dict[str, int] = field(default_factory=dict)

    @property
    def unauthorized_attempts(self) -> int:
        """Attempts to step outside the whitelist.

        Counts three things: an unknown tool name, a write tool invoked without
        an approval, and a tool-level policy refusal (a denied Redis command, a
        non-readable object kind, an out-of-scope target).

        Acceptance criterion: this is 0 for the whole evaluation run.
        """
        return self.blocked_unauthorized + self.blocked_unapproved_write + self.blocked_policy

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "unauthorized_attempts": self.unauthorized_attempts,
            "blocked_unauthorized": self.blocked_unauthorized,
            "blocked_unapproved_write": self.blocked_unapproved_write,
            "blocked_policy": self.blocked_policy,
            "blocked_budget": self.blocked_budget,
            "failed": self.failed,
            "by_tool": dict(sorted(self.by_tool.items())),
        }


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Budget:
    """Per-diagnosis resource ceiling.

    Steps, tool calls and wall-clock seconds are all capped; a diagnosis that
    runs out of budget stops collecting and reports what it has, with the
    ``budget_exhausted`` flag set so the report can never silently look complete.
    """

    max_steps: int = 12
    max_tool_calls: int = 30
    max_seconds: float = 180.0
    steps: int = 0
    tool_calls: int = 0
    started_at: float = field(default_factory=time.time)

    def charge_step(self) -> None:
        self.steps += 1

    def check_call(self) -> str | None:
        """Return a refusal reason, or ``None`` when the call may proceed."""
        if self.tool_calls >= self.max_tool_calls:
            return f"tool call budget exhausted ({self.max_tool_calls})"
        if self.elapsed > self.max_seconds:
            return f"time budget exhausted ({self.max_seconds:.0f}s)"
        return None

    def charge_call(self) -> None:
        self.tool_calls += 1

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    @property
    def exhausted(self) -> bool:
        return (
            self.steps >= self.max_steps
            or self.tool_calls >= self.max_tool_calls
            or self.elapsed >= self.max_seconds
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_steps": self.max_steps,
            "max_tool_calls": self.max_tool_calls,
            "max_seconds": self.max_seconds,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "elapsed_seconds": round(self.elapsed, 3),
        }


class ToolRegistry:
    """Whitelist + budget + audit in front of every capability."""

    def __init__(
        self,
        specs: Iterable[ToolSpec],
        sanitize: Callable[[str], tuple[str, bool]] | None = None,
        budget: Budget | None = None,
        audit: CallAudit | None = None,
        allow_write: bool = False,
    ) -> None:
        self._specs: dict[str, ToolSpec] = {s.name: s for s in specs}
        self._sanitize = sanitize or (lambda text: (text, False))
        self.budget = budget or Budget()
        self.audit = audit or CallAudit()
        self.allow_write = allow_write
        self.trace: list[dict[str, Any]] = []

    # -- introspection ---------------------------------------------------
    def catalog(self, allow_write: bool | None = None) -> list[dict[str, Any]]:
        allow = self.allow_write if allow_write is None else allow_write
        return [
            spec.catalog_entry() for spec in self._specs.values() if allow or spec.tier == "read"
        ]

    def names(self, allow_write: bool | None = None) -> list[str]:
        allow = self.allow_write if allow_write is None else allow_write
        return [name for name, spec in self._specs.items() if allow or spec.tier == "read"]

    def spec(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    # -- invocation ------------------------------------------------------
    def call(self, tool_name: str, **kwargs: Any) -> ToolResult:
        """Invoke a whitelisted tool.

        The first parameter is deliberately *not* called ``name``: several tools
        take an object name, and a keyword collision there is a silent routing
        bug (it was one, once — see docs/design-decisions.md).
        """
        call_id = f"c{len(self.trace) + 1:03d}-{uuid.uuid4().hex[:6]}"
        started = time.perf_counter()
        spec = self._specs.get(tool_name)

        if spec is None:
            self.audit.blocked_unauthorized += 1
            return self._record(
                ToolResult(
                    tool=tool_name,
                    args=kwargs,
                    ok=False,
                    blocked=True,
                    block_reason="tool not in whitelist",
                    error=f"unknown tool: {tool_name}",
                    call_id=call_id,
                ),
                started,
            )

        # Write tools are unreachable without an approval token, even if a
        # caller asks for them by name.
        approved = kwargs.pop("__approved__", False)
        if spec.tier != "read" and not approved:
            self.audit.blocked_unapproved_write += 1
            return self._record(
                ToolResult(
                    tool=tool_name,
                    args=kwargs,
                    ok=False,
                    blocked=True,
                    block_reason="write tool requires an approved action (approval gate)",
                    error="approval required",
                    call_id=call_id,
                ),
                started,
            )

        refusal = self.budget.check_call()
        if refusal is not None:
            self.audit.blocked_budget += 1
            return self._record(
                ToolResult(
                    tool=tool_name,
                    args=kwargs,
                    ok=False,
                    blocked=True,
                    block_reason=refusal,
                    error="budget exhausted",
                    call_id=call_id,
                ),
                started,
            )

        self.budget.charge_call()
        self.audit.total += 1
        self.audit.by_tool[tool_name] = self.audit.by_tool.get(tool_name, 0) + 1
        try:
            result = spec.fn(**kwargs)
        except Exception as exc:  # tool bugs must not kill a diagnosis
            self.audit.failed += 1
            result = ToolResult(
                tool=tool_name,
                args=kwargs,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                summary="tool call failed",
            )
        result.tool = tool_name
        result.args = kwargs
        result.call_id = call_id
        if result.blocked:
            self.audit.blocked_policy += 1
        elif not result.ok:
            self.audit.failed += 1
        body = result.raw or result.summary
        if len(body) > spec.max_result_chars:
            result.truncated = True
        return self._record(result, started)

    def _record(self, result: ToolResult, started: float) -> ToolResult:
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        text = result.raw or result.summary
        if text:
            clean, redacted = self._sanitize(text)
            result.redacted = result.redacted or redacted
            result.raw = clean
            if not result.summary:
                result.summary = clean.splitlines()[0][:180]
        max_chars = (
            self._specs[result.tool].max_result_chars if result.tool in self._specs else 4000
        )
        if len(result.raw) > max_chars:
            result.raw = _truncate_preserving_ends(result.raw, max_chars)
            result.truncated = True
        self.trace.append(result.to_trace())
        return result


def _truncate_preserving_ends(text: str, max_chars: int, head_ratio: float = 0.6) -> str:
    """Keep the head and the tail of an oversized payload.

    Diagnostics need both ends: the first lines carry the trigger, the last
    lines carry the most recent state. The marker is explicit so neither the
    model nor the reader mistakes a clipped payload for a complete one.
    """
    head_len = int(max_chars * head_ratio)
    tail_len = max_chars - head_len
    head = text[:head_len]
    tail = text[-tail_len:]
    dropped = len(text) - max_chars
    return f"{head}\n...[TRUNCATED {dropped} chars by redis-doctor policy]...\n{tail}"

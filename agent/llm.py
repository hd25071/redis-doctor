"""LLM access.

``RD_LLM_PROVIDER`` selects the backend:

* ``reference`` — deterministic policy (default). No network, no key, fully
  reproducible; this is what CI, ``make demo`` and the committed evaluation
  table use. It is *not* an LLM, and the reports say so.
* ``openai_compat`` — any OpenAI-compatible endpoint (DeepSeek, Qwen, vLLM,
  OpenAI) with temperature 0.
* ``cassette`` — replay a recorded run (used by CI to exercise the LLM code
  path without calling a provider).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class LLMResponse:
    text: str = ""
    parsed: dict[str, Any] | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def usage_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
        }


class LLM(Protocol):
    name: str

    def complete(self, system: str, user: str, *, expect_json: bool = True) -> LLMResponse: ...


#: Rough per-1K-token prices used for cost accounting (USD). Override with
#: RD_LLM_PRICE_IN / RD_LLM_PRICE_OUT.
DEFAULT_PRICES = {"in": 0.00014, "out": 0.00028}


def _price(key: str) -> float:
    try:
        return float(os.environ.get(f"RD_LLM_PRICE_{key.upper()}", DEFAULT_PRICES[key]))
    except (TypeError, ValueError):
        return DEFAULT_PRICES[key]


class OpenAICompatLLM:
    """Minimal OpenAI-compatible chat client (temperature 0, JSON mode)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.0,
        max_output_tokens: int = 2000,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout
        self.name = f"openai_compat:{model}"

    def complete(self, system: str, user: str, *, expect_json: bool = True) -> LLMResponse:
        import httpx

        if not self.api_key:
            raise RuntimeError("RD_LLM_API_KEY is empty; set it or use RD_LLM_PROVIDER=reference")
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if expect_json:
            payload["response_format"] = {"type": "json_object"}
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        text = body["choices"][0]["message"]["content"]
        usage = body.get("usage", {})
        parsed = _loads(text) if expect_json else None
        return LLMResponse(
            text=text,
            parsed=parsed,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            cost_usd=usage.get("prompt_tokens", 0) / 1000 * _price("in")
            + usage.get("completion_tokens", 0) / 1000 * _price("out"),
            model=self.model,
            raw=body,
        )


class CassetteLLM:
    """Replay recorded responses, keyed by a hash of the prompt.

    Used by CI so the LLM code path is exercised without a provider (and
    without spending money). ``record`` mode wraps a live client and appends
    every interaction to the cassette.
    """

    def __init__(self, path: Path | str, inner: LLM | None = None) -> None:
        self.path = Path(path)
        self.inner = inner
        self.name = f"cassette:{self.path.name}"
        self._entries: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    item = json.loads(line)
                    self._entries[item["key"]] = item

    @staticmethod
    def key(system: str, user: str) -> str:
        return hashlib.sha256((system + "\x00" + user).encode("utf-8")).hexdigest()[:32]

    def complete(self, system: str, user: str, *, expect_json: bool = True) -> LLMResponse:
        key = self.key(system, user)
        if key in self._entries:
            item = self._entries[key]
            return LLMResponse(
                text=item.get("text", ""),
                parsed=item.get("parsed"),
                prompt_tokens=item.get("prompt_tokens", 0),
                completion_tokens=item.get("completion_tokens", 0),
                cost_usd=item.get("cost_usd", 0.0),
                model=item.get("model", ""),
            )
        if self.inner is None:
            raise KeyError(f"cassette miss for key {key} ({self.path})")
        response = self.inner.complete(system, user, expect_json=expect_json)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "key": key,
                        "system_sha": hashlib.sha256(system.encode()).hexdigest()[:12],
                        "text": response.text,
                        "parsed": response.parsed,
                        "prompt_tokens": response.prompt_tokens,
                        "completion_tokens": response.completion_tokens,
                        "cost_usd": response.cost_usd,
                        "model": response.model,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        return response


def _loads(text: str) -> dict[str, Any] | None:
    if not text or not text.strip():
        return None
    stripped = text.strip()
    candidates = [stripped]
    if stripped.startswith("```"):
        body = stripped.split("\n", 1)[1] if "\n" in stripped else stripped
        candidates.append(body.rsplit("```", 1)[0].strip())
    for candidate in list(candidates):
        start = candidate.find("{")
        if start < 0:
            continue
        depth = 0
        for index, char in enumerate(candidate[start:], start):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(candidate[start : index + 1])
                    break
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def build_llm(provider: str, **kwargs: Any) -> LLM | None:
    """Return an LLM client, or ``None`` for the deterministic reference policy."""
    provider = (provider or "reference").strip().lower()
    # Popped once, for every provider: passing it straight through to the
    # OpenAI-compatible client raised TypeError and broke real-model runs.
    cassette_path = kwargs.pop("cassette_path", "eval/cassettes/run.jsonl")
    if provider in {"reference", "none", "deterministic"}:
        return None
    if provider == "openai_compat":
        return OpenAICompatLLM(**kwargs)
    if provider == "cassette":
        inner = None
        if kwargs.get("api_key"):
            inner = OpenAICompatLLM(**kwargs)
        return CassetteLLM(cassette_path, inner=inner)
    raise ValueError(f"unknown RD_LLM_PROVIDER: {provider}")

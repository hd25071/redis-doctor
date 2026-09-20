"""Knowledge base retrieval.

Retrieval is *not* evidence: the agent may use a handbook entry to decide what
to look at, but a conclusion must still cite tool output. That split is
enforced by :func:`tools.signals.evidence_signals`.

The index is a small BM25 implementation over symptom-sized chunks. It has no
service dependency, which keeps CI hermetic and keeps the retrieval logic
auditable; ``knowledge/build_index.py`` writes the same JSON the runtime reads.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from tools.base import ToolResult, ToolSpec

DEFAULT_INDEX = Path("knowledge/index/index.json")

_TOKEN_RE = re.compile(r"[a-z0-9_\-]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


@dataclass
class Chunk:
    chunk_id: str
    title: str
    category: str
    source: str
    symptom: str
    body: str
    held_out: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "title": self.title,
            "category": self.category,
            "source": self.source,
            "symptom": self.symptom,
            "body": self.body,
            "held_out": self.held_out,
        }


class KnowledgeBase:
    """BM25 retrieval over symptom chunks."""

    def __init__(self, chunks: list[Chunk], k1: float = 1.4, b: float = 0.72) -> None:
        self.chunks = chunks
        self.k1 = k1
        self.b = b
        self._docs = [tokenize(c.title + " " + c.symptom + " " + c.body) for c in chunks]
        self._len = [len(d) for d in self._docs]
        self._avgdl = (sum(self._len) / len(self._len)) if self._len else 0.0
        self._df: dict[str, int] = {}
        for doc in self._docs:
            for token in set(doc):
                self._df[token] = self._df.get(token, 0) + 1

    @classmethod
    def from_file(cls, path: Path | str = DEFAULT_INDEX) -> KnowledgeBase:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls([Chunk(**item) for item in data])

    @classmethod
    def empty(cls) -> KnowledgeBase:
        return cls([])

    def search(self, query: str, top_k: int = 4) -> list[tuple[Chunk, float]]:
        tokens = tokenize(query)
        if not tokens or not self.chunks:
            return []
        n = len(self.chunks)
        scores: list[tuple[int, float]] = []
        for index, doc in enumerate(self._docs):
            freqs: dict[str, int] = {}
            for token in doc:
                freqs[token] = freqs.get(token, 0) + 1
            score = 0.0
            for token in tokens:
                if token not in freqs:
                    continue
                df = self._df.get(token, 0) or 1
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                tf = freqs[token]
                denom = tf + self.k1 * (1 - self.b + self.b * self._len[index] / self._avgdl)
                score += idf * tf * (self.k1 + 1) / denom
            if score > 0:
                scores.append((index, score))
        scores.sort(key=lambda item: item[1], reverse=True)
        return [(self.chunks[i], round(s, 4)) for i, s in scores[:top_k]]


class KbBackend(Protocol):
    def search(self, query: str, top_k: int) -> list[tuple[Chunk, float]]: ...


def build_kb_tool(kb: KnowledgeBase | None) -> list[ToolSpec]:
    def kb_search(query: str, top_k: int = 4) -> ToolResult:
        if kb is None or not kb.chunks:
            return ToolResult(
                tool="kb_search",
                args={"query": query, "top_k": top_k},
                ok=True,
                data=[],
                summary="knowledge base disabled or empty",
                raw="(knowledge base is not available in this configuration)",
                signals={"kb_miss"},
            )
        top_k = max(1, min(int(top_k), 8))
        hits = kb.search(query, top_k)
        if not hits:
            return ToolResult(
                tool="kb_search",
                args={"query": query, "top_k": top_k},
                ok=True,
                data=[],
                summary="no knowledge base hit",
                raw="(no matching handbook entry)",
                signals={"kb_miss"},
            )
        blocks = []
        for chunk, score in hits:
            blocks.append(
                f"### {chunk.title}  [category={chunk.category} score={score}]\n"
                f"source: {chunk.source}\n"
                f"symptom: {chunk.symptom}\n"
                f"{chunk.body.strip()}"
            )
        return ToolResult(
            tool="kb_search",
            args={"query": query, "top_k": top_k},
            ok=True,
            data=[{"chunk_id": c.chunk_id, "category": c.category, "score": s} for c, s in hits],
            summary=f"{len(hits)} handbook entries for '{query}'",
            raw="\n\n".join(blocks),
            signals={"kb_hit"},
        )

    return [
        ToolSpec(
            name="kb_search",
            tier="read",
            description="Search the Redis-on-Kubernetes troubleshooting handbook. "
            "Returns handbook fragments with their source. Not evidence: verify "
            "every suggestion against tool output.",
            params={
                "query": "what to look up, e.g. 'replica link down'",
                "top_k": "number of fragments (<=8)",
            },
            fn=kb_search,
            max_result_chars=5000,
        )
    ]

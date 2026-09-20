from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from knowledge.build_index import build_index, load_chunks
from tools.kb import KnowledgeBase

REPO_ROOT = Path(__file__).resolve().parent.parent
HANDBOOK = REPO_ROOT / "knowledge" / "handbook"
HELDOUT = REPO_ROOT / "knowledge" / "heldout.yaml"


def test_index_builds_and_covers_non_held_out_categories(tmp_path) -> None:
    output, chunks = build_index(HANDBOOK, tmp_path / "index.json")
    assert output.exists()
    categories = {chunk.category for chunk in chunks}
    assert {"pod_restart", "oom_killed", "auth_failure", "dns_resolution"} <= categories


def _held_out_markers() -> list[tuple[str, str]]:
    manifest = yaml.safe_load(HELDOUT.read_text(encoding="utf-8"))
    return [
        (item["category"], marker) for item in manifest["held_out"] for marker in item["markers"]
    ]


@pytest.mark.parametrize("category,marker", _held_out_markers())
def test_handbook_does_not_leak_held_out_answers(category: str, marker: str) -> None:
    """The four held-out faults must not be answerable from the handbook."""
    for path in sorted(HANDBOOK.glob("*.md")):
        text = path.read_text(encoding="utf-8").lower()
        assert marker.lower() not in text, (
            f"{path.name} leaks held-out marker {marker!r} (category {category})"
        )


def test_heldout_manifest_matches_reading(lab) -> None:
    manifest = yaml.safe_load(HELDOUT.read_text(encoding="utf-8"))
    declared = {item["id"]: item["category"] for item in manifest["held_out"]}
    actual = {scenario.id: scenario.category for scenario in lab.held_out()}
    assert declared == actual
    for scenario in lab.all():
        assert scenario.held_out is (scenario.id in declared)


def test_retrieval_finds_the_relevant_symptom() -> None:
    kb = KnowledgeBase(load_chunks(HANDBOOK))
    hits = kb.search("副本 master_link_status down 无法解析主节点域名", top_k=3)
    assert hits, "retrieval returned nothing"
    categories = [chunk.category for chunk, _ in hits]
    assert "dns_resolution" in categories
    hits = kb.search("写入被拒绝 OOM command not allowed maxmemory", top_k=3)
    assert any(chunk.category == "maxmemory_reached" for chunk, _ in hits)


def test_retrieval_has_no_answer_for_held_out_symptoms() -> None:
    kb = KnowledgeBase(load_chunks(HANDBOOK))
    hits = kb.search("CPU 限流 throttled cfs 周期抖动", top_k=3)
    assert all(chunk.category != "cpu_throttling" for chunk, _ in hits)

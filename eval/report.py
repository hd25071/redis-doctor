"""Render results JSON into the tables that go into README/docs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

VARIANT_NAMES = {
    "A": "A 纯 LLM（仅告警文本）",
    "B": "B + 只读工具（单次 ReAct）",
    "C": "C + 知识库 RAG",
    "D": "D + 假设验证循环（完整版）",
    "D*": "D* 完整版 + 预算收紧（max_tool_calls=3）",
}


def load_results(path: Path | str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main_table(data: dict[str, Any]) -> str:
    rows = [
        "| 组 | 根因 Top-1 | 根因 Top-3 | 证据召回(已观察) | 证据召回(已引用) | 幻觉率 | 平均步数 | 平均工具调用 | 平均耗时 | token/次 | 越权次数 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key, agg in data["aggregates"].items():
        rows.append(
            "| {name} | {top1:.1%} | {top3:.1%} | {seen:.1%} | {cited:.1%} | {hal:.1%} | "
            "{steps} | {calls} | {secs}s | {tokens} | {unauth} |".format(
                name=VARIANT_NAMES.get(key, key),
                top1=agg["top1"],
                top3=agg["top3"],
                seen=agg["evidence_recall_seen"],
                cited=agg["evidence_recall_cited"],
                hal=agg["hallucination_rate"],
                steps=agg["avg_steps"],
                calls=agg["avg_tool_calls"],
                secs=agg["avg_seconds"],
                tokens=agg["avg_tokens"],
                unauth=agg["unauthorized_attempts"],
            )
        )
    return "\n".join(rows)


def scenario_table(data: dict[str, Any]) -> str:
    variants = sorted({run["variant"] for run in data["runs"]})
    header = "| 场景 | 故障类别 | " + " | ".join(variants) + " | held-out |"
    sep = "|---|---|" + "---|" * (len(variants) + 1)
    truth = {run["scenario_id"]: run["truth"] for run in data["runs"]}
    held = set(data["meta"].get("held_out", []))
    rows = [header, sep]
    for scenario_id in sorted(data["matrix"]):
        cells = []
        for variant in variants:
            ok = data["matrix"][scenario_id].get(variant)
            cells.append("✅" if ok else ("❌" if ok is False else "—"))
        rows.append(
            f"| {scenario_id} | {truth.get(scenario_id, '')} | "
            + " | ".join(cells)
            + f" | {'是' if scenario_id in held else '否'} |"
        )
    return "\n".join(rows)


def failure_table(data: dict[str, Any], limit: int = 12) -> str:
    failures = data.get("failures", [])
    if not failures:
        return "_没有失败样本。_"
    rows = [
        "| 场景 | 组 | 标准答案 | 预测 | 失败类型 | 已观察信号 | 工具调用 |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in failures[:limit]:
        rows.append(
            f"| {item['scenario_id']} | {item['variant']} | {item['truth']} | "
            f"{item['predicted']} | {item['kind']} | "
            f"{', '.join(item['signals_seen']) or '无'} | {item['tool_calls']} |"
        )
    return "\n".join(rows)


def held_out_table(data: dict[str, Any]) -> str:
    """Covered vs held-out accuracy: does the agent generalise or recite?

    A group that only wins on covered categories is reciting the handbook; the
    interesting number is the held-out column.
    """
    held = set(data["meta"].get("held_out", []))
    variants = sorted({run["variant"] for run in data["runs"]})
    rows = [
        "| 组 | 手册覆盖类别 Top-1 | held-out 类别 Top-1 | held-out 场景 |",
        "|---|---|---|---|",
    ]
    for variant in variants:
        subset = [run for run in data["runs"] if run["variant"] == variant]
        covered = [run for run in subset if run["scenario_id"] not in held]
        unseen = [run for run in subset if run["scenario_id"] in held]
        covered_rate = sum(1 for run in covered if run["top1"]) / len(covered) if covered else 0.0
        unseen_rate = sum(1 for run in unseen if run["top1"]) / len(unseen) if unseen else 0.0
        rows.append(
            f"| {VARIANT_NAMES.get(variant, variant)} | {covered_rate:.1%} | "
            f"{unseen_rate:.1%} | {', '.join(sorted({r['scenario_id'] for r in unseen}))} |"
        )
    return "\n".join(rows)


def render_markdown(data: dict[str, Any]) -> str:
    meta = data["meta"]
    approval = meta.get("approval_required")
    total_unauthorized = sum(a["unauthorized_attempts"] for a in data["aggregates"].values())
    lines = [
        "# 评测结果",
        "",
        f"- 生成时间: {meta.get('started_at')}",
        f"- 策略: `{meta.get('policy')}` / `{meta.get('llm_name')}`",
        f"- 后端: `{meta.get('backend')}`"
        + ("（进程内沙箱集群）" if meta.get("backend") == "sandbox" else "（真实集群）"),
        f"- 每个场景每组的重复次数: {meta.get('runs_per_cell')}",
        f"- 场景数: {len(meta.get('scenarios', []))}，其中 held-out: "
        f"{', '.join(meta.get('held_out', [])) or '无'}",
        f"- 审批闸门: {'开启（L1 写操作需批准）' if approval else '关闭'}",
        f"- 总耗时: {meta.get('duration_seconds')}s",
        f"- 越权操作次数合计: **{total_unauthorized}**（验收要求为 0）",
        "",
        "## 主表",
        "",
        main_table(data),
        "",
        "## 逐场景 Top-1",
        "",
        scenario_table(data),
        "",
        "## 失败样本（按类型归类）",
        "",
        failure_table(data),
        "",
        "## 泛化：手册覆盖 vs held-out",
        "",
        held_out_table(data),
        "",
    ]
    return "\n".join(lines)

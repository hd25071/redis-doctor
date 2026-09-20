"""Server-rendered trajectory UI.

The plan suggested Streamlit; a server-rendered page is used instead so the
demo has zero extra dependencies and one port. The page that matters for the
3-minute video is the trajectory view: every tool call, what it returned, which
evidence was cited, and what the approval gate decided.
"""

from __future__ import annotations

import html
import json
from typing import Any

STYLE = """
body{font-family:ui-sans-serif,Segoe UI,Helvetica,Arial,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}
header{padding:18px 24px;background:#161a22;border-bottom:1px solid #262c38}
h1{font-size:18px;margin:0}
a{color:#7cc4ff;text-decoration:none}
main{padding:20px 24px;max-width:1180px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #232a35;vertical-align:top}
th{background:#161a22;position:sticky;top:0}
code,pre{font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
pre{background:#12161d;border:1px solid #232a35;border-radius:6px;padding:10px;overflow:auto;max-height:320px}
.pill{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11.5px;background:#243040;color:#9fd0ff}
.ok{background:#1c3324;color:#8fe0a8}.bad{background:#3a1f24;color:#ff9aa8}
.warn{background:#3a3320;color:#ffd479}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
small{color:#9aa4b2}
"""


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def layout(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        f"<title>{_esc(title)}</title><style>{STYLE}</style></head>"
        f"<body><header><h1>redis-doctor · {_esc(title)}</h1>"
        "<small>Fault-injection driven diagnosis for Redis on Kubernetes</small>"
        "</header><main>" + body + "</main></body></html>"
    )


def index_page(records: list[dict[str, Any]], stats: dict[str, Any]) -> str:
    rows = []
    for record in records:
        status = record.get("status", "")
        klass = (
            "ok"
            if status in {"ok", "approved_executed"}
            else ("warn" if status == "waiting_approval" else "bad")
        )
        rows.append(
            "<tr>"
            f"<td><a href='/ui/diagnoses/{_esc(record['id'])}'>{_esc(record['id'])}</a></td>"
            f"<td>{_esc(record.get('alertname'))}</td>"
            f"<td>{_esc(record.get('root_cause'))}</td>"
            f"<td>{_esc(record.get('confidence'))}</td>"
            f"<td><span class='pill {klass}'>{_esc(status)}</span></td>"
            f"<td>{_esc(record.get('variant'))}</td>"
            f"<td><small>{_esc(record.get('alert', '').splitlines()[0])}</small></td>"
            "</tr>"
        )
    body = [
        "<div class='grid'><div><h3>统计</h3><pre>"
        + _esc(json.dumps(stats, ensure_ascii=False, indent=2))
        + "</pre></div><div><h3>审批队列</h3><p>"
        + "<a href='/ui/approvals'>查看待审批动作</a>"
        + "</p><p><a href='/metrics'>Prometheus 指标</a></p>"
        + "<p><a href='/docs'>OpenAPI 文档</a></p></div></div>",
        "<h3>最近诊断</h3><table><tr><th>ID</th><th>告警</th><th>根因</th>"
        "<th>置信度</th><th>状态</th><th>组</th><th>告警摘要</th></tr>",
        "".join(rows) or "<tr><td colspan=7>还没有诊断记录</td></tr>",
        "</table>",
    ]
    return layout("diagnoses", "".join(body))


def detail_page(record: dict[str, Any]) -> str:
    report = record.get("report") or {}
    state = record.get("state") or {}
    evidence = "".join(
        f"<tr><td><code>{_esc(ref.get('call_id'))}</code></td><td>{_esc(ref.get('tool'))}</td>"
        f"<td><span class='pill'>{_esc(ref.get('signal'))}</span></td>"
        f"<td><pre>{_esc(ref.get('quote'))}</pre></td></tr>"
        for ref in report.get("evidence", [])
    )
    hypotheses = "".join(
        f"<tr><td>{_esc(h.get('id'))}</td><td>{_esc(h.get('category'))}</td>"
        f"<td>{_esc(h.get('status'))}</td><td>{_esc(h.get('confidence'))}</td>"
        f"<td><small>{_esc(', '.join(h.get('supporting', [])))}</small></td>"
        f"<td><small>{_esc(', '.join(h.get('contradicting', [])))}</small></td></tr>"
        for h in state.get("hypotheses", [])
    )
    calls = "".join(
        f"<tr><td><code>{_esc(call.get('call_id'))}</code></td><td>{_esc(call.get('tool'))}</td>"
        f"<td><small>{_esc(json.dumps(call.get('args'), ensure_ascii=False))}</small></td>"
        f"<td><small>{_esc(', '.join(call.get('signals') or []))}</small></td>"
        f"<td>{'<span class="pill bad">BLOCKED</span> ' + _esc(call.get('blocked_reason')) if call.get('blocked') else ''}"
        f"{'<span class="pill warn">truncated</span>' if call.get('truncated') else ''}</td>"
        f"<td><pre>{_esc((call.get('output') or '')[:1200])}</pre></td></tr>"
        for call in record.get("trace", [])
    )
    approvals = "".join(
        f"<tr><td><code>{_esc(a['id'])}</code></td><td>{_esc(a['action'].get('action'))}</td>"
        f"<td>{_esc(a['action'].get('tier'))}</td><td>{_esc(a['status'])}</td>"
        f"<td><small>{_esc(a.get('decided_by'))}</small></td>"
        f"<td>{'<a href=/ui/approvals>待审批</a>' if a['status'] == 'pending' else ''}</td></tr>"
        for a in record.get("approvals", [])
    )
    actions = "".join(
        f"<li>[{_esc(a.get('tier'))}/{_esc(a.get('risk'))}] {_esc(a.get('action'))}"
        f"<pre>{_esc(a.get('command'))}</pre></li>"
        for a in report.get("suggested_actions", [])
    )
    body = f"""
    <p><a href="/ui">&larr; 返回列表</a></p>
    <h3>结论</h3>
    <p>根因 <span class="pill">{_esc(report.get("root_cause"))}</span>
       {_esc(report.get("root_cause_label"))} · 置信度 <b>{_esc(report.get("confidence"))}</b>
       · 轮次 {_esc(report.get("iterations"))} · 状态 <span class="pill">{_esc(record.get("status"))}</span></p>
    <p>{_esc(report.get("summary"))}</p>
    <h4>证据（每条都可追溯到一次工具调用）</h4>
    <table><tr><th>call_id</th><th>工具</th><th>信号</th><th>原文</th></tr>{evidence}</table>
    <h4>假设</h4>
    <table><tr><th>id</th><th>类别</th><th>状态</th><th>置信度</th><th>支持</th><th>反驳</th></tr>{hypotheses}</table>
    <h4>建议动作</h4><ul>{actions or "<li>无</li>"}</ul>
    <h4>审批</h4>
    <table><tr><th>id</th><th>动作</th><th>等级</th><th>状态</th><th>决策人</th></tr>{approvals}</table>
    <h4>完整工具轨迹</h4>
    <table><tr><th>call_id</th><th>工具</th><th>参数</th><th>信号</th><th>标记</th><th>返回（截断显示）</th></tr>{calls}</table>
    <h4>原始状态</h4>
    <pre>{_esc(json.dumps(state.get("audit"), ensure_ascii=False, indent=2))}</pre>
    <pre>{_esc(json.dumps(state.get("budget"), ensure_ascii=False, indent=2))}</pre>
    """
    return layout(f"diagnosis {record['id']}", body)


def approvals_page(pending: list[dict[str, Any]]) -> str:
    rows = "".join(
        f"<tr><td><code>{_esc(a['id'])}</code></td>"
        f"<td><a href='/ui/diagnoses/{_esc(a['diagnosis_id'])}'>{_esc(a['diagnosis_id'])}</a></td>"
        f"<td>{_esc(a['action'].get('action'))}</td>"
        f"<td>{_esc(a['action'].get('tier'))} / {_esc(a['action'].get('risk'))}</td>"
        f"<td><pre>{_esc(a['action'].get('command'))}</pre></td>"
        f"<td><form method='post' action='/ui/approvals/{_esc(a['id'])}'>"
        "<input type='hidden' name='decision' value='approve'>"
        "<button type='submit'>批准</button></form>"
        f"<form method='post' action='/ui/approvals/{_esc(a['id'])}'>"
        "<input type='hidden' name='decision' value='deny'>"
        "<button type='submit'>拒绝</button></form></td></tr>"
        for a in pending
    )
    body = (
        "<p><a href='/ui'>&larr; 返回列表</a></p><h3>待审批动作</h3>"
        "<p><small>展示的是将要调用的 API 与参数，而不是模型对动作的描述。</small></p>"
        "<table><tr><th>id</th><th>诊断</th><th>动作</th><th>等级</th><th>命令</th><th>决策</th></tr>"
        + (rows or "<tr><td colspan=6>没有待审批动作</td></tr>")
        + "</table>"
    )
    return layout("approvals", body)

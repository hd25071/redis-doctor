"""Server-rendered console: dashboard, trajectory view and approval queue.

No JavaScript and no front-end build: one HTML page per view, served by the
same FastAPI process that owns the trajectory store. The console is therefore
part of the tested surface, and a demo needs a single command.
"""

from __future__ import annotations

import html
import json
from typing import Any

STYLE = """
:root{--bg:#0f1216;--panel:#161a21;--line:#242b35;--fg:#e7ebf0;--muted:#96a1af;--accent:#63b3ff}
*{box-sizing:border-box}
body{font-family:ui-sans-serif,"Segoe UI",Helvetica,Arial,sans-serif;margin:0;background:var(--bg);color:var(--fg)}
header{display:flex;justify-content:space-between;align-items:baseline;padding:14px 22px;background:var(--panel);border-bottom:1px solid var(--line)}
h1{font-size:16px;margin:0;font-weight:600}
h2{font-size:14px;margin:20px 0 8px}
h3{font-size:12px;margin:16px 0 6px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
main{padding:18px 22px 40px;max-width:1240px}
.muted{color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.card .v{font-size:22px;font-weight:600}
.card .k{font-size:12px;color:var(--muted);margin-top:2px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{background:var(--panel);color:var(--muted);font-weight:600}
code,pre{font-family:ui-monospace,Consolas,"Courier New",monospace;font-size:12.5px}
pre{background:#12161d;border:1px solid var(--line);border-radius:6px;padding:9px;overflow:auto;max-height:300px;white-space:pre-wrap}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11.5px;background:#233043;color:#a8d3ff}
.ok{background:#17301f;color:#8fe0a8}.bad{background:#3a1d22;color:#ff9aa8}.warn{background:#37301c;color:#ffd479}
form.inline{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
select,input,button{background:#12161d;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 9px;font-size:13px}
button{cursor:pointer}
button.primary{background:#1b4a7a;border-color:#2a6ea8}
button.danger{background:#4a1f26;border-color:#7a2b36}
small{color:var(--muted)}
.grid2{display:grid;grid-template-columns:1.5fr 1fr;gap:20px}
@media(max-width:900px){.grid2{grid-template-columns:1fr}}
"""


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def layout(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_esc(title)} · redis-doctor</title><style>{STYLE}</style></head><body>"
        "<header><h1><a href='/ui' style='color:inherit'>redis-doctor</a></h1>"
        "<small>Redis on Kubernetes 诊断控制台</small></header><main>"
        + body
        + "</main></body></html>"
    )


def _status_pill(status: str) -> str:
    klass = (
        "ok"
        if status in {"ok", "approved_executed"}
        else ("warn" if status == "waiting_approval" else "bad")
    )
    return f"<span class='pill {klass}'>{_esc(status)}</span>"


def dashboard_page(
    records: list[dict[str, Any]],
    stats: dict[str, Any],
    scenarios: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    banner: str = "",
) -> str:
    approvals = stats.get("approvals", {}) or {}
    distribution: dict[str, int] = {}
    for record in records:
        cause = record.get("root_cause") or "-"
        distribution[cause] = distribution.get(cause, 0) + 1
    cards = [
        ("诊断总数", stats.get("diagnoses", 0)),
        ("待审批", approvals.get("pending", 0)),
        ("已批准", approvals.get("approved", 0)),
        ("已拒绝", approvals.get("denied", 0)),
        ("场景总数", len(scenarios)),
        ("held-out", sum(1 for s in scenarios if s["held_out"])),
    ]
    options = "".join(
        f"<option value='{_esc(s['id'])}'>{_esc(s['id'])} · {_esc(s['category'])}"
        f"{' · held-out' if s['held_out'] else ''}</option>"
        for s in scenarios
    )
    rows = "".join(
        "<tr>"
        f"<td><a href='/ui/diagnoses/{_esc(r['id'])}'>{_esc(r['id'])}</a></td>"
        f"<td>{_esc(r.get('alertname') or '-')}</td>"
        f"<td><span class='pill'>{_esc(r.get('root_cause') or '-')}</span></td>"
        f"<td>{_esc(r.get('confidence'))}</td>"
        f"<td>{_status_pill(r.get('status', ''))}</td>"
        f"<td>{_esc(r.get('variant'))}</td>"
        f"<td><small>{_esc((r.get('alert') or '').splitlines()[0])}</small></td>"
        "</tr>"
        for r in records
    )
    approval_rows = "".join(
        "<tr>"
        f"<td><a href='/ui/diagnoses/{_esc(a['diagnosis_id'])}'>{_esc(a['diagnosis_id'])}</a></td>"
        f"<td>{_esc(a['action'].get('action'))}</td>"
        f"<td>{_esc(a['action'].get('tier'))} / {_esc(a['action'].get('risk'))}</td>"
        f"<td><code>{_esc(a['action'].get('command'))}</code></td>"
        f"<td><form class='inline' method='post' action='/ui/approvals/{_esc(a['id'])}'>"
        "<input type='hidden' name='decision' value='approve'>"
        "<button class='primary'>批准</button></form></td>"
        f"<td><form class='inline' method='post' action='/ui/approvals/{_esc(a['id'])}'>"
        "<input type='hidden' name='decision' value='deny'>"
        "<button class='danger'>拒绝</button></form></td>"
        "</tr>"
        for a in pending
    )
    body = [
        "<p class='muted'>"
        + _esc(
            banner or "选择场景注入故障并运行诊断；每次工具调用、证据引用与审批动作都会写入轨迹。"
        )
        + "</p>",
        "<div class='cards'>"
        + "".join(
            f"<div class='card'><div class='v'>{_esc(v)}</div><div class='k'>{_esc(k)}</div></div>"
            for k, v in cards
        )
        + "</div>",
        "<div class='grid2'><div>",
        "<h3>运行诊断</h3>",
        "<form class='inline' method='post' action='/ui/diagnose'>",
        f"<select name='scenario'>{options}</select>",
        "<select name='variant'><option value='D'>D 完整版</option><option value='C'>C 手册检索</option>"
        "<option value='B'>B 单次工具</option><option value='A'>A 仅告警</option></select>",
        "<button class='primary'>注入并诊断</button>",
        "</form>",
        "<h3>诊断记录</h3>",
        "<table><tr><th>ID</th><th>告警</th><th>根因</th><th>置信度</th><th>状态</th>"
        "<th>组</th><th>告警摘要</th></tr>",
        rows or "<tr><td colspan='7' class='muted'>还没有诊断记录</td></tr>",
        "</table></div><div>",
        "<h3>待审批动作</h3>",
        "<table><tr><th>诊断</th><th>动作</th><th>等级</th><th>命令</th><th></th><th></th></tr>",
        approval_rows or "<tr><td colspan='6' class='muted'>无待审批动作</td></tr>",
        "</table>",
        "<h3>根因分布</h3>",
        "<table>"
        + (
            "".join(
                f"<tr><td>{_esc(cause)}</td><td>{count}</td></tr>"
                for cause, count in sorted(distribution.items(), key=lambda i: -i[1])[:8]
            )
            or "<tr><td class='muted'>暂无数据</td></tr>"
        )
        + "</table>",
        "<h3>接口</h3>",
        "<table><tr><td><a href='/scenarios'>/scenarios</a></td><td>场景清单</td></tr>"
        "<tr><td><a href='/diagnoses'>/diagnoses</a></td><td>诊断记录（JSON）</td></tr>"
        "<tr><td><a href='/approvals'>/approvals</a></td><td>审批队列（JSON）</td></tr>"
        "<tr><td><a href='/metrics'>/metrics</a></td><td>Prometheus 指标</td></tr>"
        "<tr><td><a href='/docs'>/docs</a></td><td>OpenAPI</td></tr></table>",
        "</div></div>",
    ]
    return layout("控制台", "".join(body))


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
        f"<td>{'<span class="pill bad">BLOCKED</span>' if call.get('blocked') else ''}"
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
    <p><a href="/ui">← 返回控制台</a></p>
    <h3>结论</h3>
    <p>根因 <span class="pill">{_esc(report.get("root_cause"))}</span>
       {_esc(report.get("root_cause_label"))} · 置信度 <b>{_esc(report.get("confidence"))}</b>
       · 轮次 {_esc(report.get("iterations"))} · 状态 {_status_pill(record.get("status", ""))}</p>
    <p>{_esc(report.get("summary"))}</p>
    <h3>证据（每条对应一次工具调用）</h3>
    <table><tr><th>call_id</th><th>工具</th><th>信号</th><th>原文</th></tr>{evidence}</table>
    <h3>假设</h3>
    <table><tr><th>id</th><th>类别</th><th>状态</th><th>置信度</th><th>支持</th><th>反驳</th></tr>{hypotheses}</table>
    <h3>建议动作</h3><ul>{actions or '<li class="muted">无</li>'}</ul>
    <h3>审批</h3>
    <table><tr><th>id</th><th>动作</th><th>等级</th><th>状态</th><th>决策人</th><th></th></tr>{approvals}</table>
    <h3>完整工具轨迹</h3>
    <table><tr><th>call_id</th><th>工具</th><th>参数</th><th>信号</th><th>标记</th><th>返回（截断显示）</th></tr>{calls}</table>
    <h3>审计与预算</h3>
    <pre>{_esc(json.dumps(state.get("audit"), ensure_ascii=False, indent=2))}</pre>
    <pre>{_esc(json.dumps(state.get("budget"), ensure_ascii=False, indent=2))}</pre>
    """
    return layout(f"诊断 {record['id']}", body)


def approvals_page(pending: list[dict[str, Any]]) -> str:
    rows = "".join(
        f"<tr><td><code>{_esc(a['id'])}</code></td>"
        f"<td><a href='/ui/diagnoses/{_esc(a['diagnosis_id'])}'>{_esc(a['diagnosis_id'])}</a></td>"
        f"<td>{_esc(a['action'].get('action'))}</td>"
        f"<td>{_esc(a['action'].get('tier'))} / {_esc(a['action'].get('risk'))}</td>"
        f"<td><code>{_esc(a['action'].get('command'))}</code></td>"
        f"<td><form class='inline' method='post' action='/ui/approvals/{_esc(a['id'])}'>"
        "<input type='hidden' name='decision' value='approve'>"
        "<button class='primary'>批准</button></form>"
        f"<form class='inline' method='post' action='/ui/approvals/{_esc(a['id'])}'>"
        "<input type='hidden' name='decision' value='deny'>"
        "<button class='danger'>拒绝</button></form></td></tr>"
        for a in pending
    )
    body = (
        "<p><a href='/ui'>← 返回控制台</a></p><h3>待审批动作</h3>"
        "<p class='muted'>展示将要调用的 API 与参数，而不是模型对动作的描述。</p>"
        "<table><tr><th>id</th><th>诊断</th><th>动作</th><th>等级</th><th>命令</th><th>决策</th></tr>"
        + (rows or "<tr><td colspan='6' class='muted'>无待审批动作</td></tr>")
        + "</table>"
    )
    return layout("审批", body)

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
:root{
  color-scheme:dark;
  --bg:#0b0e14;--bg2:#0f131b;--panel:rgba(255,255,255,.028);--line:rgba(255,255,255,.09);
  --line-strong:rgba(255,255,255,.16);--fg:#eef2f8;--muted:#8d99ab;--accent:#5b9dff;
  --accent2:#8b7cff;--radius:14px;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;color:var(--fg);font-size:14px;line-height:1.55;min-height:100vh;
  font-family:"Inter",ui-sans-serif,"Segoe UI Variable","Segoe UI",Helvetica,Arial,sans-serif;
  background:
    radial-gradient(900px 500px at 12% -9%,rgba(91,157,255,.16),transparent 60%),
    radial-gradient(700px 420px at 100% 0,rgba(139,124,255,.13),transparent 55%),
    linear-gradient(180deg,var(--bg) 0%,var(--bg2) 100%);
  background-attachment:fixed;
}
header{
  position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:20px;padding:13px 26px;
  border-bottom:1px solid var(--line);background:rgba(11,14,20,.78);
  backdrop-filter:blur(14px) saturate(150%);
}
.brand{display:flex;align-items:center;gap:10px;font-weight:650;letter-spacing:-.01em}
.brand .mark{width:11px;height:11px;border-radius:50%;
  background:linear-gradient(135deg,var(--accent),var(--accent2));box-shadow:0 0 18px rgba(91,157,255,.55)}
nav{display:flex;gap:2px}
nav a{padding:6px 12px;border-radius:999px;color:var(--muted);font-size:13px}
nav a:hover{color:var(--fg);background:rgba(255,255,255,.06);text-decoration:none}
header .spacer{margin-left:auto}
a{color:var(--accent);text-decoration:none;transition:color .15s}
a:hover{color:#8ec1ff}
main{padding:26px 26px 64px;max-width:1280px;margin:0 auto}
h2{font-size:15px;margin:26px 0 10px;font-weight:600;letter-spacing:-.01em}
h3{font-size:11.5px;margin:0 0 10px;color:var(--muted);font-weight:600;
  text-transform:uppercase;letter-spacing:.08em}
.muted{color:var(--muted)}
.lead{color:var(--muted);margin:0 0 20px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:14px}
.card{position:relative;overflow:hidden;border:1px solid var(--line);border-radius:var(--radius);
  background:linear-gradient(180deg,rgba(255,255,255,.045),rgba(255,255,255,.012));padding:16px 18px;
  transition:transform .16s ease,border-color .16s ease,box-shadow .16s ease}
.card:hover{transform:translateY(-2px);border-color:var(--line-strong);box-shadow:0 12px 30px rgba(0,0,0,.35)}
.card:before{content:"";position:absolute;inset:0 0 auto 0;height:2px;
  background:linear-gradient(90deg,var(--accent),var(--accent2));opacity:.75}
.card .v{font-size:26px;font-weight:640;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.card .k{font-size:12px;color:var(--muted);margin-top:4px}
.panel{border:1px solid var(--line);border-radius:var(--radius);background:var(--panel);
  padding:18px;backdrop-filter:blur(6px)}
.panel + .panel{margin-top:20px}
table{border-collapse:separate;border-spacing:0;width:100%;font-size:13px}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:none}
tbody tr{transition:background .12s}
tbody tr:hover{background:rgba(255,255,255,.035)}
th{font-size:11.5px;color:var(--muted);font-weight:600;text-transform:uppercase;
  letter-spacing:.06em;background:rgba(255,255,255,.02)}
code,pre{font-family:"JetBrains Mono",ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12.5px}
code{color:#bcd4ff;word-break:break-word}
td code{display:inline-block;max-width:230px}
pre{background:rgba(0,0,0,.32);border:1px solid var(--line);border-radius:10px;padding:10px 12px;
  overflow:auto;max-height:320px;white-space:pre-wrap;word-break:break-word}
.pill{display:inline-flex;align-items:center;gap:6px;padding:2px 10px;border-radius:999px;
  font-size:11.5px;font-weight:550;white-space:nowrap;background:rgba(91,157,255,.12);
  color:#a9cdff;border:1px solid rgba(91,157,255,.28)}
.pill:before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor;opacity:.9}
.ok{background:rgba(70,214,140,.12);color:#84e3ad;border-color:rgba(70,214,140,.3)}
.bad{background:rgba(255,99,126,.12);color:#ff9db0;border-color:rgba(255,99,126,.3)}
.warn{background:rgba(255,196,84,.12);color:#ffd58a;border-color:rgba(255,196,84,.3)}
form.inline{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
select,input,button{font:inherit;color:var(--fg);background:#131822;
  border:1px solid var(--line);border-radius:10px;padding:8px 12px;
  transition:border-color .15s,background .15s}
select option{background:#131822;color:var(--fg)}
select:hover,input:hover{border-color:var(--line-strong)}
select:focus,input:focus,button:focus{outline:none;border-color:var(--accent);
  box-shadow:0 0 0 3px rgba(91,157,255,.2)}
button{cursor:pointer;font-weight:550}
button.primary{background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;color:#08111f}
button.primary:hover{filter:brightness(1.06)}
button.danger{background:rgba(255,99,126,.14);border-color:rgba(255,99,126,.32);color:#ffb3c0}
button.danger:hover{background:rgba(255,99,126,.22)}
small{color:var(--muted)}
.grid2{display:grid;grid-template-columns:1.55fr 1fr;gap:22px;align-items:start}
@media(max-width:960px){.grid2{grid-template-columns:1fr}}
"""


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def layout(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_esc(title)} · redis-doctor</title><style>{STYLE}</style></head><body>"
        "<header><span class='brand'><span class='mark'></span>"
        "<a href='/ui' style='color:inherit'>redis-doctor</a></span>"
        "<nav><a href='/ui'>控制台</a><a href='/ui/approvals'>审批</a>"
        "<a href='/ui/scenarios'>场景</a><a href='/ui/records'>记录</a>"
        "<a href='/ui/metrics'>指标</a><a href='/ui/api'>接口</a></nav>"
        "<span class='spacer'></span><small>Redis on Kubernetes 诊断</small>"
        "</header><main>" + body + "</main></body></html>"
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

    def _short(text: str, limit: int = 34) -> str:
        text = text or ""
        return text if len(text) <= limit else text[: limit - 1] + "…"

    approval_rows = "".join(
        "<tr>"
        f"<td><a href='/ui/diagnoses/{_esc(a['diagnosis_id'])}'>{_esc(a['diagnosis_id'])}</a></td>"
        f"<td>{_esc(a['action'].get('tier'))} / {_esc(a['action'].get('risk'))}</td>"
        f"<td><code title='{_esc(a['action'].get('command'))}'>"
        f"{_esc(_short(a['action'].get('command')))}</code></td>"
        f"<td><form class='inline' method='post' action='/ui/approvals/{_esc(a['id'])}'>"
        "<button class='primary' name='decision' value='approve'>批准</button>"
        "<button class='danger' name='decision' value='deny'>拒绝</button></form></td>"
        "</tr>"
        for a in pending
    )
    body = [
        "<p class='lead'>"
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
        "<div class='grid2'><div><div class='panel'>",
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
        "</table></div></div><div><div class='panel'>",
        "<h3>待审批动作</h3>",
        "<table><tr><th>诊断</th><th>等级</th><th>将执行的命令</th><th>决策</th></tr>",
        approval_rows or "<tr><td colspan='4' class='muted'>无待审批动作</td></tr>",
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
        "<table><tr><td><a href='/ui/scenarios'>场景</a></td><td>16 类故障清单</td></tr>"
        "<tr><td><a href='/ui/records'>记录</a></td><td>诊断记录与轨迹</td></tr>"
        "<tr><td><a href='/ui/metrics'>指标</a></td><td>运行指标与 Prometheus 文本</td></tr>"
        "<tr><td><a href='/ui/api'>接口</a></td><td>HTTP 接口一览</td></tr></table>",
        "</div></div></div>",
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


def scenarios_page(scenarios: list[dict[str, Any]]) -> str:
    rows = "".join(
        "<tr>"
        f"<td><code>{_esc(s['id'])}</code></td>"
        f"<td>{_esc(s['name'])}</td>"
        f"<td><span class='pill'>{_esc(s['category'])}</span></td>"
        f"<td>{'<span class="pill warn">held-out</span>' if s['held_out'] else ''}</td>"
        f"<td>{'是' if s['kubectl_verified'] else '否'}</td>"
        "</tr>"
        for s in scenarios
    )
    body = (
        "<h2>故障场景</h2>"
        "<p class='lead'>每个场景在 faultlab/scenarios 下声明注入、恢复、告警文本、标准根因与"
        "必须观察到的证据信号；held-out 表示该类别未写入手册。</p>"
        "<div class='panel'><table><tr><th>#</th><th>故障</th><th>类别</th>"
        "<th></th><th>真机注入已验证</th></tr>" + rows + "</table></div>"
        "<p class='muted'>机器可读版本：<code>GET /scenarios</code></p>"
    )
    return layout("场景", body)


def records_page(records: list[dict[str, Any]]) -> str:
    rows = "".join(
        "<tr>"
        f"<td><a href='/ui/diagnoses/{_esc(r['id'])}'><code>{_esc(r['id'])}</code></a></td>"
        f"<td>{_esc(r.get('root_cause') or '-')}</td>"
        f"<td>{_esc(r.get('confidence'))}</td>"
        f"<td>{_status_pill(r.get('status', ''))}</td>"
        f"<td>{_esc(r.get('variant'))}</td>"
        f"<td><small>{_esc((r.get('alert') or '').splitlines()[0])}</small></td>"
        "</tr>"
        for r in records
    )
    body = (
        "<h2>诊断记录</h2>"
        "<div class='panel'><table><tr><th>ID</th><th>根因</th><th>置信度</th><th>状态</th>"
        "<th>组</th><th>告警</th></tr>"
        + (rows or "<tr><td colspan='6' class='muted'>还没有诊断记录</td></tr>")
        + "</table></div>"
        "<p class='muted'>机器可读版本：<code>GET /diagnoses</code>，单条："
        "<code>GET /diagnoses/{id}</code></p>"
    )
    return layout("记录", body)


def metrics_page(stats: dict[str, Any], prometheus_text: str) -> str:
    approvals = stats.get("approvals", {}) or {}
    if not prometheus_text.strip():
        # The live counters start at zero after a restart, while the trajectory
        # store keeps history. Show store-derived counters so the page is never
        # empty (and says which numbers are persistent).
        lines = [f"redis_doctor_diagnoses_total {stats.get('diagnoses', 0)}"]
        lines += [
            f'redis_doctor_approvals_total{{decision="{key}"}} {value}'
            for key, value in sorted(approvals.items())
        ]
        lines.append(f"redis_doctor_alert_fingerprints_total {stats.get('alert_fingerprints', 0)}")
        prometheus_text = "\n".join(lines) + "\n"
    cards = [
        ("诊断总数", stats.get("diagnoses", 0)),
        ("待审批", approvals.get("pending", 0)),
        ("已批准", approvals.get("approved", 0)),
        ("已拒绝", approvals.get("denied", 0)),
        ("告警指纹", stats.get("alert_fingerprints", 0)),
        ("告警事件", stats.get("alert_events", 0)),
    ]
    body = (
        "<h2>运行指标</h2>"
        "<div class='cards'>"
        + "".join(
            f"<div class='card'><div class='v'>{_esc(v)}</div><div class='k'>{_esc(k)}</div></div>"
            for k, v in cards
        )
        + "</div>"
        "<div class='panel'><h3>Prometheus 文本</h3>"
        f"<pre>{_esc(prometheus_text)}</pre></div>"
        "<p class='muted'>采集地址：<code>GET /metrics</code></p>"
    )
    return layout("指标", body)


def api_page(rows: list[tuple[str, str, str]]) -> str:
    table = "".join(
        f"<tr><td><span class='pill'>{_esc(method)}</span></td><td><code>{_esc(path)}</code></td>"
        f"<td>{_esc(desc)}</td></tr>"
        for method, path, desc in rows
    )
    body = (
        "<h2>HTTP 接口</h2>"
        "<div class='panel'><table><tr><th>方法</th><th>路径</th><th>说明</th></tr>"
        + table
        + "</table></div>"
        "<p class='muted'>OpenAPI 描述：<code>GET /openapi.json</code>"
        "（内置浏览器无法渲染 Swagger UI，故以本页代替）</p>"
    )
    return layout("接口", body)

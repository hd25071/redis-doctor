"""FastAPI application.

Endpoints
---------
``POST /webhook/alertmanager``  ingest an alert (shared-token auth + fingerprint cooldown)
``POST /diagnose``              run a diagnosis on demand
``GET  /diagnoses[/{id}]``      list / read diagnoses with their full trace
``GET  /approvals``             pending write actions waiting for a human
``POST /approvals/{id}``        approve or deny an action (the interrupt resume path)
``GET  /ui``                    trajectory viewer (used for the demo video)
``GET  /healthz /readyz /metrics``

The diagnosis loop itself never performs a write: it parks the action as an
``ApprovalRequest``. Only ``POST /approvals/{id}`` with ``decision=approve``
reaches the actuator, and that path is audited like any other tool call.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

import rdconfig
from agent.graph import DiagnosisGraph, execute_approved_action, plan_write_call
from agent.llm import build_llm
from agent.reference import ReferenceReasoner
from agent.state import SuggestedAction
from agent.variants import VARIANTS
from api import security, ui
from api.store import Store
from faultlab.runner import FaultLab
from sandbox.cluster import SimCluster
from tools.context import ToolContext
from tools.safety import detect_injection

METRIC_HELP = {
    "redis_doctor_diagnoses_total": "diagnoses executed",
    "redis_doctor_tool_calls_total": "tool calls executed",
    "redis_doctor_tool_blocked_total": "tool calls refused by the safety layer",
    "redis_doctor_unauthorized_total": "attempts outside the whitelist (must stay 0)",
    "redis_doctor_approvals_total": "approval decisions, by outcome",
    "redis_doctor_diagnosis_seconds_sum": "total diagnosis wall-clock seconds",
    "redis_doctor_llm_tokens_total": "LLM tokens consumed",
}


def _parse_form(body: str) -> dict[str, str]:
    """Parse an urlencoded form without pulling in python-multipart."""
    from urllib.parse import parse_qs

    return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}


class Metrics:
    def __init__(self) -> None:
        self.counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        self.counters[key] = self.counters.get(key, 0.0) + value

    def render(self) -> str:
        lines = []
        emitted: set[str] = set()
        for (name, labels), value in sorted(self.counters.items()):
            if name not in emitted:
                lines.append(f"# HELP {name} {METRIC_HELP.get(name, name)}")
                lines.append(f"# TYPE {name} counter")
                emitted.add(name)
            label_text = "{" + ",".join(f'{k}="{v}"' for k, v in labels) + "}" if labels else ""
            lines.append(f"{name}{label_text} {value}")
        return "\n".join(lines) + "\n"


def create_app(settings: rdconfig.Settings | None = None) -> FastAPI:
    settings = settings or rdconfig.Settings.from_env()
    store = Store(settings.resolve(settings.data_dir) / "trajectories.sqlite3")
    metrics = Metrics()
    app = FastAPI(title="redis-doctor", version="0.1.0")
    app.state.settings = settings
    app.state.store = store
    app.state.metrics = metrics
    # One sandbox cluster per process: the Deployment is single-replica with the
    # Recreate strategy precisely because the trajectory store and the sandbox
    # are single-writer/single-owner resources.
    cluster = (
        SimCluster(settings.namespace, settings.instance) if settings.backend != "real" else None
    )
    # The sandbox is a single shared object: serialise diagnoses that mutate it.
    sandbox_lock = threading.Lock()

    def _scenarios() -> list[dict[str, Any]]:
        lab = FaultLab(cluster, settings.resolve(settings.scenarios_dir))
        return [
            {
                "id": item.id,
                "name": item.name,
                "category": item.category,
                "held_out": item.held_out,
                "kubectl_verified": item.kubectl_verified,
            }
            for item in lab.all()
        ]

    def _require_auth(authorization: str | None) -> str:
        """Authenticate a mutating call; return the identity for the audit log.

        Fail-closed: with no configured token the call is refused, so a
        deployment that forgot to set one is not silently open.
        """
        configured = settings.api_token or settings.webhook_token
        if not security.token_ok(authorization, configured):
            detail = (
                "no API token configured; set RD_API_TOKEN (or RD_WEBHOOK_TOKEN)"
                if not configured
                else "invalid or missing bearer token"
            )
            raise HTTPException(status_code=401, detail=detail)
        return "api-token"

    # -- health ----------------------------------------------------------
    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        problems = []
        kb = settings.resolve(settings.kb_index)
        if not kb.exists():
            problems.append(f"knowledge index missing: {kb}")
        if settings.backend == "real" and not settings.prom_url:
            problems.append("RD_PROM_URL is not configured for the real backend")
        if not settings.webhook_token:
            problems.append("RD_WEBHOOK_TOKEN is not configured (webhook will refuse alerts)")
        return JSONResponse(
            {"status": "ok" if not problems else "degraded", "problems": problems},
            status_code=200 if not problems else 503,
        )

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics_endpoint() -> str:
        return metrics.render()

    # -- diagnosis -------------------------------------------------------
    def _run(
        alert_text: str,
        variant_key: str,
        fingerprint: str | None = None,
        alertname: str | None = None,
        auto_approve: bool = False,
        sandbox_scenario: str | None = None,
    ) -> dict[str, Any]:
        variant = VARIANTS.get(variant_key.upper(), VARIANTS["D"])
        lab = FaultLab(cluster, settings.resolve(settings.scenarios_dir)) if cluster else None
        if sandbox_scenario and lab is not None:
            lab.reset()
            lab.inject(sandbox_scenario)
        ctx = ToolContext(settings=settings, cluster=cluster)
        llm = build_llm(
            settings.llm_provider,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            temperature=settings.llm_temperature,
            max_output_tokens=settings.llm_max_output_tokens,
            cassette_path=str(settings.resolve("eval/cassettes/api.jsonl")),
        )
        reasoner = ReferenceReasoner(variant)
        if llm is not None:
            from agent.llm_reasoner import LLMReasoner

            reasoner = LLMReasoner(variant, llm)
        graph = DiagnosisGraph(
            ctx,
            variant,
            reasoner=reasoner,
            approver=None,  # the API approval gate is the human, via /approvals
            settings=settings,
        )
        run = graph.run(alert_text)
        state = run.state
        report = run.report
        record = {
            "id": state.diagnosis_id,
            "created_at": state.started_at,
            "finished_at": state.finished_at,
            "status": state.status,
            "variant": variant.key,
            "alert": alert_text,
            "alertname": alertname,
            "fingerprint": fingerprint,
            "root_cause": report.root_cause if report else None,
            "confidence": report.confidence if report else None,
            "report": report.model_dump() if report else None,
            "state": json.loads(state.model_dump_json()),
            "audit": run.registry.audit.to_dict(),
            "meta": {"budget": run.registry.budget.to_dict(), "policy": reasoner.name},
        }
        store.save_diagnosis(record, run.trace)
        if fingerprint:
            store.link_fingerprint(fingerprint, state.diagnosis_id)
        if state.approval is not None:
            store.save_approval(
                state.approval.id,
                state.diagnosis_id,
                state.approval.action.model_dump(),
                status=state.approval.status,
            )
            if auto_approve:
                store.save_approval(
                    state.approval.id,
                    state.diagnosis_id,
                    state.approval.action.model_dump(),
                    status="approved",
                    decided_by="auto-approve (demo)",
                )
        metrics.inc("redis_doctor_diagnoses_total")
        metrics.inc("redis_doctor_tool_calls_total", run.registry.audit.total)
        metrics.inc("redis_doctor_unauthorized_total", run.registry.audit.unauthorized_attempts)
        metrics.inc("redis_doctor_diagnosis_seconds_sum", state.elapsed_seconds)
        metrics.inc("redis_doctor_llm_tokens_total", state.token_usage.get("total_tokens", 0))
        # Count hits from the alert text *and* from tool output, otherwise a
        # poisoned log line would never show up in the metric.
        for hit in sorted(set(detect_injection(alert_text)) | set(state.injection_hits)):
            metrics.inc("redis_doctor_injection_hits_total", 1, kind=hit)
        if sandbox_scenario and lab is not None:
            lab.recover(sandbox_scenario)
        return {
            "diagnosis_id": state.diagnosis_id,
            "status": state.status,
            "root_cause": record["root_cause"],
            "confidence": record["confidence"],
            "evidence": [ref.model_dump() for ref in (report.evidence if report else [])],
            "suggested_actions": [
                action.model_dump() for action in (report.suggested_actions if report else [])
            ],
            "approval": state.approval.model_dump() if state.approval else None,
            "audit": record["audit"],
            "elapsed_seconds": round(state.elapsed_seconds, 4),
        }

    @app.post("/webhook/alertmanager")
    async def alertmanager_webhook(
        request: Request,
        background: BackgroundTasks,
        authorization: str | None = Header(default=None),
        x_redis_doctor_token: str | None = Header(default=None),
    ) -> JSONResponse:
        token = authorization or x_redis_doctor_token
        if not security.token_ok(token, settings.webhook_token):
            raise HTTPException(status_code=401, detail="invalid or missing webhook token")
        payload = await request.json()
        alerts = security.parse_alertmanager_payload(payload)
        if not alerts:
            return JSONResponse({"accepted": 0, "detail": "no alerts in payload"})
        accepted = []
        for alert in alerts:
            should_run, seen = store.check_fingerprint(
                alert["fingerprint"], settings.alert_cooldown_seconds
            )
            if not should_run:
                metrics.inc("redis_doctor_alert_deduped_total")
                accepted.append(
                    {
                        "fingerprint": alert["fingerprint"],
                        "diagnosed": False,
                        "reason": f"cooldown active (seen {seen} times)",
                    }
                )
                continue
            with sandbox_lock:
                result = await run_in_threadpool(
                    _run,
                    alert["alert_text"],
                    settings.variant,
                    alert["fingerprint"],
                    alert["alertname"],
                )
            # Cooldown starts only after the diagnosis completed, so a failure
            # does not mute the alert for the next 15 minutes.
            store.record_fingerprint(
                alert["fingerprint"], alert["alertname"], result["diagnosis_id"]
            )
            accepted.append({"fingerprint": alert["fingerprint"], "diagnosed": True, **result})
        del background  # kept for a future async worker; diagnosis is fast here
        return JSONResponse({"accepted": len(accepted), "results": accepted})

    @app.post("/diagnose")
    async def diagnose(
        request: Request, authorization: str | None = Header(default=None)
    ) -> JSONResponse:
        _require_auth(authorization)
        body = await request.json()
        alert_text = body.get("alert_text") or body.get("alert")
        if not alert_text:
            raise HTTPException(status_code=422, detail="alert_text is required")
        if body.get("sandbox_scenario") and settings.backend == "real":
            raise HTTPException(
                status_code=422,
                detail="sandbox_scenario is only available with RD_BACKEND=sandbox; "
                "a real cluster must be diagnosed in its own injected state",
            )
        # auto_approve writes audit rows claiming a human approved an action, so
        # it is restricted to the sandbox and never enabled by the caller alone.
        auto_approve = bool(body.get("auto_approve")) and settings.backend == "sandbox"
        with sandbox_lock:
            result = await run_in_threadpool(
                _run,
                alert_text,
                body.get("variant", settings.variant),
                None,
                None,
                auto_approve,
                body.get("sandbox_scenario"),
            )
        return JSONResponse(result)

    @app.get("/diagnoses")
    def list_diagnoses(limit: int = 50) -> dict[str, Any]:
        return {"items": store.list_diagnoses(limit)}

    @app.get("/diagnoses/{diagnosis_id}")
    def get_diagnosis(diagnosis_id: str) -> JSONResponse:
        record = store.get_diagnosis(diagnosis_id)
        if record is None:
            raise HTTPException(status_code=404, detail="diagnosis not found")
        return JSONResponse(record)

    # -- approval gate ---------------------------------------------------
    @app.get("/approvals")
    def approvals() -> dict[str, Any]:
        return {"pending": store.list_pending_approvals()}

    def _decide(approval_id: str, decision: str, decided_by: str, note: str) -> dict[str, Any]:
        approval = store.get_approval(approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="approval not found")
        if approval["status"] != "pending":
            raise HTTPException(status_code=409, detail=f"already {approval['status']}")
        approve = decision.lower() in {"approve", "approved", "yes", "true"}
        # Claim atomically: a second concurrent request loses the race and gets
        # 409 instead of executing the same action twice.
        if not store.claim_approval(approval_id):
            raise HTTPException(status_code=409, detail="approval is already being processed")
        metrics.inc("redis_doctor_approvals_total", 1, decision="approved" if approve else "denied")
        if not approve:
            store.finish_approval(approval_id, "denied", decided_by, note)
            return {"approval_id": approval_id, "status": "denied", "executed": False}
        action = SuggestedAction(**approval["action"])
        if action.tier != "write_l1":
            store.finish_approval(approval_id, "approved", decided_by, note)
            return {
                "approval_id": approval_id,
                "status": "approved",
                "executed": False,
                "detail": f"tier {action.tier} is advisory in this MVP; nothing executed",
            }
        call = plan_write_call(action)
        if call is None:
            store.finish_approval(
                approval_id, "failed", decided_by, "action is not executable (no structured target)"
            )
            raise HTTPException(
                status_code=422,
                detail="approved action carries no structured target; refusing to execute",
            )
        _, args = call
        pod = args["pod"]
        # Re-check preconditions at execution time: the pod may no longer exist,
        # and the diagnosis may be hours old.
        ctx = ToolContext(settings=settings, cluster=cluster)
        pods = ctx.build_registry(include_kb=False).call("k8s_get_pods")
        if not any(p.get("name") == pod for p in (pods.data or [])):
            store.finish_approval(approval_id, "failed", decided_by, f"{pod} no longer exists")
            raise HTTPException(status_code=409, detail=f"{pod} no longer exists")
        try:
            outcome = execute_approved_action(ctx, pod, settings)
        except Exception as exc:
            store.finish_approval(approval_id, "failed", decided_by, f"{type(exc).__name__}: {exc}")
            raise HTTPException(status_code=502, detail=f"execution failed: {exc}") from exc
        store.finish_approval(approval_id, "executed", decided_by, note, result=outcome)
        return {
            "approval_id": approval_id,
            "status": "executed",
            "executed": True,
            "target": pod,
            "ok": outcome["ok"],
            "call_id": outcome["call"]["call_id"],
            "verification": outcome["verification"],
        }

    @app.post("/approvals/{approval_id}")
    async def decide(approval_id: str, request: Request) -> JSONResponse:
        identity = _require_auth(request.headers.get("authorization"))
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            body = await request.json()
            decision = str(body.get("decision", "deny"))
            decided_by = str(body.get("decided_by", identity))
            note = str(body.get("note", ""))
        else:
            form = _parse_form((await request.body()).decode("utf-8"))
            decision = form.get("decision", "deny")
            decided_by = form.get("decided_by", identity)
            note = form.get("note", "")
        result = _decide(approval_id, decision, decided_by, note)
        if "application/json" in content_type:
            return JSONResponse(result)
        return RedirectResponse("/approvals", status_code=303)

    # -- UI --------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/ui")

    @app.get("/ui", response_class=HTMLResponse)
    def ui_index() -> str:
        return ui.dashboard_page(
            store.list_diagnoses(25),
            store.stats(),
            _scenarios(),
            store.list_pending_approvals(),
        )

    @app.get("/scenarios")
    def scenarios() -> dict[str, Any]:
        return {"items": _scenarios()}

    @app.get("/ui/scenarios", response_class=HTMLResponse)
    def ui_scenarios() -> str:
        return ui.scenarios_page(_scenarios())

    @app.get("/ui/records", response_class=HTMLResponse)
    def ui_records() -> str:
        return ui.records_page(store.list_diagnoses(100))

    @app.get("/ui/metrics", response_class=HTMLResponse)
    def ui_metrics() -> str:
        return ui.metrics_page(store.stats(), metrics.render())

    @app.get("/ui/api", response_class=HTMLResponse)
    def ui_api() -> str:
        return ui.api_page(
            [
                ("GET", "/healthz", "存活探针"),
                ("GET", "/readyz", "就绪探针，缺索引或未配置 token 时返回 503"),
                (
                    "POST",
                    "/webhook/alertmanager",
                    "Alertmanager 告警入口，需 Bearer token，按 fingerprint 冷却",
                ),
                ("POST", "/diagnose", "按告警文本运行一次诊断（沙箱可带 sandbox_scenario）"),
                ("GET", "/diagnoses", "诊断记录列表"),
                ("GET", "/diagnoses/{id}", "单次诊断，含工具轨迹与审批"),
                ("GET", "/approvals", "待审批动作"),
                ("POST", "/approvals/{id}", "批准或拒绝一个 L1 动作"),
                ("GET", "/scenarios", "故障场景清单"),
                ("GET", "/metrics", "Prometheus 指标"),
                ("GET", "/openapi.json", "OpenAPI 描述"),
            ]
        )

    @app.post("/ui/diagnose")
    async def ui_diagnose(request: Request) -> RedirectResponse:
        """Run one sandbox diagnosis from the console and open its trajectory."""
        _require_auth(request.headers.get("authorization") or request.cookies.get("rd_token"))
        form = _parse_form((await request.body()).decode("utf-8"))
        scenario_id = form.get("scenario", "").upper()
        variant = form.get("variant", settings.variant).upper()
        if scenario_id not in {item["id"] for item in _scenarios()}:
            raise HTTPException(status_code=422, detail=f"unknown scenario {scenario_id}")
        if settings.backend == "real":
            raise HTTPException(
                status_code=422,
                detail="the console launcher injects sandbox faults; use POST /diagnose "
                "with an alert when running against a real cluster",
            )
        scenario = FaultLab(cluster, settings.resolve(settings.scenarios_dir)).get(scenario_id)
        with sandbox_lock:
            result = await run_in_threadpool(
                _run,
                scenario.alert,
                variant,
                None,
                f"console:{scenario_id}",
                False,
                scenario_id,
            )
        return RedirectResponse(f"/ui/diagnoses/{result['diagnosis_id']}", status_code=303)

    @app.get("/ui/diagnoses/{diagnosis_id}", response_class=HTMLResponse)
    def ui_detail(diagnosis_id: str) -> HTMLResponse:
        record = store.get_diagnosis(diagnosis_id)
        if record is None:
            raise HTTPException(status_code=404, detail="diagnosis not found")
        return HTMLResponse(ui.detail_page(record))

    @app.get("/ui/approvals", response_class=HTMLResponse)
    def ui_approvals() -> str:
        return ui.approvals_page(store.list_pending_approvals())

    @app.post("/ui/approvals/{approval_id}")
    async def ui_decide(approval_id: str, request: Request) -> RedirectResponse:
        _require_auth(request.headers.get("authorization") or request.cookies.get("rd_token"))
        form = _parse_form((await request.body()).decode("utf-8"))
        decision = form.get("decision", "deny")
        _decide(approval_id, decision, decided_by="ui", note="")
        return RedirectResponse("/ui/approvals", status_code=303)

    # The UI links use /approvals/{id}; keep that path consistent.
    @app.get("/approvals/{approval_id}")
    def get_approval(approval_id: str) -> JSONResponse:
        approval = store.get_approval(approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="approval not found")
        return JSONResponse(approval)

    return app

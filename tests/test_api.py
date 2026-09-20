from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RD_WEBHOOK_TOKEN", "test-token")
    from rdconfig import Settings

    settings = Settings.from_env()
    settings.data_dir = str(tmp_path / "data")
    settings.webhook_token = "test-token"
    settings.backend = "sandbox"
    return TestClient(create_app(settings))


def test_health_and_ready(client) -> None:
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/readyz").status_code in {200, 503}


def test_webhook_requires_a_token(client) -> None:
    assert client.post("/webhook/alertmanager", json={"alerts": []}).status_code == 401
    assert (
        client.post(
            "/webhook/alertmanager",
            headers={"Authorization": "Bearer wrong"},
            json={"alerts": []},
        ).status_code
        == 401
    )


def test_webhook_diagnoses_and_deduplicates(client) -> None:
    payload = {
        "alerts": [
            {
                "labels": {
                    "alertname": "KubePodOOMKilled",
                    "namespace": "demo",
                    "instance": "demo",
                    "pod": "demo-0",
                    "severity": "critical",
                },
                "annotations": {"summary": "demo-0 被 OOMKilled 3 次"},
            }
        ]
    }
    headers = {"Authorization": "Bearer test-token"}
    first = client.post("/webhook/alertmanager", headers=headers, json=payload).json()
    assert first["results"][0]["diagnosed"] is True
    second = client.post("/webhook/alertmanager", headers=headers, json=payload).json()
    assert second["results"][0]["diagnosed"] is False
    assert "cooldown" in second["results"][0]["reason"]


def test_sandbox_scenario_diagnosis_and_approval_flow(client) -> None:
    response = client.post(
        "/diagnose",
        json={
            "alert_text": "[P1] KubePodOOMKilled namespace=demo pod=demo-0 OOMKilled x3",
            "sandbox_scenario": "S02",
            "variant": "D",
        },
    )
    body = response.json()
    assert body["root_cause"] == "oom_killed"
    assert body["status"] == "waiting_approval"
    assert body["audit"]["unauthorized_attempts"] == 0

    pending = client.get("/approvals").json()["pending"]
    assert len(pending) == 1
    assert pending[0]["action"]["tier"] == "write_l1"

    decided = client.post(
        f"/approvals/{pending[0]['id']}",
        json={"decision": "approve", "decided_by": "pytest"},
    ).json()
    assert decided["status"] == "approved"
    assert decided["executed"] is True
    assert decided["target"] == "demo-0"
    assert client.get("/approvals").json()["pending"] == []


def test_denied_approval_executes_nothing(client) -> None:
    client.post(
        "/diagnose",
        json={
            "alert_text": "[P1] KubePodOOMKilled pod=demo-0 OOMKilled x3",
            "sandbox_scenario": "S02",
        },
    )
    approval = client.get("/approvals").json()["pending"][0]
    result = client.post(
        f"/approvals/{approval['id']}",
        json={"decision": "deny", "decided_by": "pytest"},
    ).json()
    assert result == {"approval_id": approval["id"], "status": "denied", "executed": False}


def test_ui_and_metrics_render(client) -> None:
    client.post(
        "/diagnose",
        json={"alert_text": "alert", "sandbox_scenario": "S06"},
    )
    assert client.get("/ui").status_code == 200
    assert client.get("/ui/approvals").status_code == 200
    diagnoses = client.get("/diagnoses").json()["items"]
    assert diagnoses
    detail = client.get(f"/ui/diagnoses/{diagnoses[0]['id']}")
    assert detail.status_code == 200
    assert "工具轨迹" in detail.text
    metrics = client.get("/metrics").text
    assert "redis_doctor_diagnoses_total" in metrics
    assert "redis_doctor_unauthorized_total" in metrics


def test_sandbox_scenario_refused_on_real_backend(client) -> None:
    client.app.state.settings.backend = "real"
    response = client.post("/diagnose", json={"alert_text": "x", "sandbox_scenario": "S02"})
    assert response.status_code == 422
    client.app.state.settings.backend = "sandbox"

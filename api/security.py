"""Webhook authentication and alert de-duplication."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any


def token_ok(provided: str | None, expected: str) -> bool:
    """Constant-time bearer-token comparison.

    An empty ``expected`` means the deployment has not configured a token; that
    is refused rather than allowed, so a misconfigured webhook cannot become an
    unauthenticated diagnosis trigger.
    """
    if not expected:
        return False
    if not provided:
        return False
    candidate = provided.removeprefix("Bearer ").strip()
    return hmac.compare_digest(candidate, expected)


def fingerprint(labels: dict[str, Any]) -> str:
    """Stable alert identity: alertname + namespace + instance + pod.

    Deliberately *not* the whole payload: Alertmanager re-sends the same alert
    with updated annotations, and a fingerprint that changed with every resend
    would defeat the cooldown.
    """
    keys = ("alertname", "namespace", "instance", "pod", "container", "pvc")
    parts = [f"{key}={labels.get(key, '')}" for key in keys]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def parse_alertmanager_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise an Alertmanager webhook body into diagnosis requests."""
    alerts = payload.get("alerts") or []
    out: list[dict[str, Any]] = []
    for alert in alerts:
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}
        summary = annotations.get("summary") or labels.get("alertname", "alert")
        description = annotations.get("description") or ""
        text = (
            f"[{labels.get('severity', 'P2')}] {labels.get('alertname', 'Alert')}\n"
            f"namespace={labels.get('namespace', '')} instance={labels.get('instance', '')} "
            f"pod={labels.get('pod', '')}\n"
            f"summary: {summary}\n"
            + (f"description: {description}\n" if description else "")
            + f"labels: {', '.join(f'{k}={v}' for k, v in sorted(labels.items()))}"
        )
        out.append(
            {
                "fingerprint": fingerprint(labels),
                "alertname": labels.get("alertname", ""),
                "labels": labels,
                "alert_text": text,
            }
        )
    return out

"""测试用事件构造小工具。"""

from __future__ import annotations

from typing import Any


def event(
    event_id: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    occurred_at: str,
    version: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": occurred_at,
        "version": version,
        "payload": payload,
    }


T0 = "2026-09-20T09:00:00+08:00"
QUAL_FROM = "2026-01-01T00:00:00+08:00"
QUAL_UNTIL = "2027-12-31T23:59:59+08:00"

ALL_SCOPES = [
    "care_profile",
    "medication_reminder",
    "adherence_followup",
    "cross_store_handoff",
    "emergency_supply",
]


def consent(
    subject: str = "S1",
    store: str = "A",
    scopes: list[str] | None = None,
    at: str = T0,
    event_id: str = "e-consent",
    aggregate_id: str = "g1",
    version: int = 1,
) -> dict[str, Any]:
    return event(
        event_id, "CONSENT_GRANTED", "consent_grant", aggregate_id, at, version,
        {"subject_id": subject, "store_id": store, "scopes": scopes or list(ALL_SCOPES), "granted_at": at},
    )


def qualified(
    pharmacist: str = "P1",
    store: str = "A",
    start: str = QUAL_FROM,
    end: str = QUAL_UNTIL,
    event_id: str = "e-qual-1",
    at: str = QUAL_FROM,
) -> dict[str, Any]:
    return event(
        event_id, "PHARMACIST_QUALIFIED", "pharmacist_profile", pharmacist, at, 1,
        {
            "pharmacist_id": pharmacist,
            "store_id": store,
            "qualification": "licensed_pharmacist",
            "valid_from": start,
            "valid_until": end,
        },
    )


def plan_v1(
    subject: str = "S1",
    plan_id: str = "plan1",
    pharmacist: str = "P1",
    store: str = "A",
    at: str = T0,
    event_id: str = "e-plan1",
) -> dict[str, Any]:
    return event(
        event_id, "PLAN_ACTIVATED", "service_plan", plan_id, at, 1,
        {
            "subject_id": subject,
            "plan_id": plan_id,
            "plan_version": 1,
            "pharmacist_id": pharmacist,
            "store_id": store,
            "sales_influence": "none",
            "activated_at": at,
        },
    )


def reminder(
    obligation: str,
    *,
    subject: str = "S1",
    plan_id: str = "plan1",
    plan_version: int = 1,
    store: str = "A",
    at: str = "2026-09-20T10:00:00+08:00",
    window_end: str = "2026-09-20T12:00:00+08:00",
    basis: str = "e-plan1",
    event_id: str | None = None,
    reminder_id: str | None = None,
) -> dict[str, Any]:
    event_id = event_id or f"e-rem-{obligation}"
    reminder_id = reminder_id or f"rem-{obligation}"
    return event(
        event_id, "REMINDER_FIRED", "reminder", reminder_id, at, 1,
        {
            "subject_id": subject,
            "store_id": store,
            "plan_id": plan_id,
            "plan_version": plan_version,
            "reminder_id": reminder_id,
            "obligation_id": obligation,
            "window_start": at,
            "window_end": window_end,
            "factual_basis": {"based_on_event_id": basis},
            "fired_at": at,
        },
    )


def followup(
    fact: str,
    *,
    subject: str = "S1",
    store: str = "A",
    recorded_by: str = "P1",
    content_hash: str = "sha256:same",
    at: str = "2026-09-20T10:30:00+08:00",
    version: int = 1,
    event_id: str | None = None,
) -> dict[str, Any]:
    return event(
        event_id or f"e-fu-{fact}-{version}", "FOLLOWUP_RECORDED", "service_fact", fact, at, version,
        {
            "subject_id": subject,
            "store_id": store,
            "fact_key": fact,
            "content_hash": content_hash,
            "recorded_by": recorded_by,
            "recorded_at": at,
        },
    )


def confirm(
    obligation: str,
    fact: str,
    *,
    subject: str = "S1",
    store: str = "A",
    pharmacist: str = "P1",
    at: str = "2026-09-20T11:00:00+08:00",
    personally: bool = True,
    version: int = 1,
    event_id: str | None = None,
) -> dict[str, Any]:
    return event(
        event_id or f"e-confirm-{obligation}", "SERVICE_CONFIRMED", "obligation", obligation, at, version,
        {
            "subject_id": subject,
            "store_id": store,
            "fact_key": fact,
            "obligation_id": obligation,
            "pharmacist_id": pharmacist,
            "personally_performed": personally,
            "confirmed_at": at,
        },
    )

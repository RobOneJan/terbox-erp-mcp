"""Unit tests for the in-memory approval gate in terbox_mcp/approvals.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from terbox_mcp import approvals

TENANT = "tenant-a"


def test_create_is_pending_and_flattens_payload() -> None:
    request = approvals.create(
        TENANT,
        action="ab1000_kunde",
        resource_id="Jane Doe",
        data={"vorname": "Jane", "nachname": "Doe", "email": "jane@example.com"},
    )

    assert request.status == approvals.ApprovalStatus.PENDING
    assert request.payload == {"vorname": "Jane", "nachname": "Doe", "email": "jane@example.com"}
    assert approvals.get(request.id, TENANT).id == request.id


def test_flatten_handles_nested_dicts_and_lists() -> None:
    data = {
        "config": {"width_mm": 1000.0, "with_roof": True},
        "extras": [{"beschreibung": "Solar", "einzelpreis": 199.5}],
        "sevdesk_contact_id": "123",
    }
    request = approvals.create(TENANT, action="ab1000_angebot_konfiguration", resource_id="r1", data=data)

    assert request.payload["config.width_mm"] == "1000.0"
    assert request.payload["config.with_roof"] == "True"
    assert request.payload["extras[0].beschreibung"] == "Solar"
    assert request.payload["extras[0].einzelpreis"] == "199.5"
    assert request.payload["sevdesk_contact_id"] == "123"


def test_execute_before_approval_is_denied() -> None:
    request = approvals.create(TENANT, action="ab1000_kunde", resource_id="Jane Doe", data={"vorname": "Jane"})

    with pytest.raises(approvals.ApprovalStateError):
        approvals.consume_approved(request.id, TENANT, action="ab1000_kunde")


def test_approve_then_execute_succeeds_and_is_replay_protected() -> None:
    request = approvals.create(TENANT, action="ab1000_kunde", resource_id="Jane Doe", data={"vorname": "Jane"})

    approvals.decide(request.id, TENANT, approve=True)
    consumed = approvals.consume_approved(request.id, TENANT, action="ab1000_kunde")
    assert consumed.status == approvals.ApprovalStatus.CONSUMED

    with pytest.raises(approvals.ApprovalStateError):
        approvals.consume_approved(request.id, TENANT, action="ab1000_kunde")


def test_reject_prevents_execute() -> None:
    request = approvals.create(TENANT, action="ab1000_kunde", resource_id="Jane Doe", data={"vorname": "Jane"})

    rejected = approvals.decide(request.id, TENANT, approve=False)
    assert rejected.status == approvals.ApprovalStatus.REJECTED

    with pytest.raises(approvals.ApprovalStateError):
        approvals.consume_approved(request.id, TENANT, action="ab1000_kunde")


def test_deciding_twice_is_rejected() -> None:
    request = approvals.create(TENANT, action="ab1000_kunde", resource_id="Jane Doe", data={"vorname": "Jane"})
    approvals.decide(request.id, TENANT, approve=True)

    with pytest.raises(approvals.ApprovalStateError):
        approvals.decide(request.id, TENANT, approve=True)


def test_execute_rejects_mismatched_action() -> None:
    request = approvals.create(TENANT, action="ab1000_kunde", resource_id="Jane Doe", data={"vorname": "Jane"})
    approvals.decide(request.id, TENANT, approve=True)

    with pytest.raises(approvals.ApprovalStateError):
        approvals.consume_approved(request.id, TENANT, action="ab1000_angebot_konfiguration")


def test_unknown_approval_id_raises_not_found() -> None:
    with pytest.raises(approvals.ApprovalNotFoundError):
        approvals.get("does-not-exist", TENANT)


def test_wrong_tenant_cannot_see_the_approval() -> None:
    request = approvals.create(TENANT, action="ab1000_kunde", resource_id="Jane Doe", data={"vorname": "Jane"})

    with pytest.raises(approvals.ApprovalNotFoundError):
        approvals.get(request.id, "some-other-tenant")


def test_expired_pending_request_cannot_be_approved() -> None:
    request = approvals.create(TENANT, action="ab1000_kunde", resource_id="Jane Doe", data={"vorname": "Jane"})
    request.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    with pytest.raises(approvals.ApprovalStateError):
        approvals.decide(request.id, TENANT, approve=True)
    assert approvals.get(request.id, TENANT).status == approvals.ApprovalStatus.EXPIRED


def test_edits_are_applied_before_approval_and_types_are_preserved() -> None:
    data = {"vorname": "Jane", "nachname": "Doe", "email": "typo@example.com"}
    request = approvals.create(TENANT, action="ab1000_kunde", resource_id="Jane Doe", data=data)

    approved = approvals.decide(
        request.id, TENANT, approve=True, edits={"email": "corrected@example.com"}
    )

    assert approved.data["email"] == "corrected@example.com"
    assert approved.payload["email"] == "corrected@example.com"
    # Untouched fields are unaffected.
    assert approved.data["vorname"] == "Jane"


def test_nested_edit_is_applied_and_coerced_to_original_type() -> None:
    data = {
        "config": {"width_mm": 1000.0, "with_roof": True},
        "sevdesk_contact_id": "123",
        "extras": [{"beschreibung": "Solar", "einzelpreis": 199.5}],
    }
    request = approvals.create(TENANT, action="ab1000_angebot_konfiguration", resource_id="r1", data=data)

    approved = approvals.decide(
        request.id,
        TENANT,
        approve=True,
        edits={"config.width_mm": "1200.0", "extras[0].einzelpreis": "149.0"},
    )

    assert approved.data["config"]["width_mm"] == 1200.0
    assert isinstance(approved.data["config"]["width_mm"], float)
    assert approved.data["extras"][0]["einzelpreis"] == 149.0


def test_unknown_edit_field_raises() -> None:
    request = approvals.create(TENANT, action="ab1000_kunde", resource_id="Jane Doe", data={"vorname": "Jane"})

    with pytest.raises(approvals.ApprovalEditError):
        approvals.decide(request.id, TENANT, approve=True, edits={"does_not_exist": "x"})

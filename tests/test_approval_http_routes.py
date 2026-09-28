"""These two HTTP routes are the one and only way a human (via a Teams
approval card / channel adapter) can approve or reject a pending SevDesk
write - they are deliberately not MCP tools (see server.py's comment above
their registration), so no sequence of tool calls the LLM makes can ever
reach them.

These tests call the route handler coroutines directly against a hand-built
Starlette `Request` (rather than going through a real ASGI transport /
TestClient), which is enough to exercise the routing/body-parsing logic
these handlers actually contain without needing a live server."""

from __future__ import annotations

import asyncio
import json

from starlette.requests import Request

from terbox_mcp import approvals, server

TENANT = approvals.DEFAULT_TENANT_ID


def _seed_pending_approval(tenant_id: str = TENANT) -> str:
    request = approvals.create(
        tenant_id,
        action="ab1000_kunde",
        resource_id="Jane Doe",
        data={"vorname": "Jane", "nachname": "Doe", "email": "jane@example.com"},
    )
    return request.id


def _make_request(approval_id: str, *, tenant: str | None = None, body: bytes = b"") -> Request:
    query_string = f"tenant={tenant}".encode() if tenant is not None else b""
    scope = {
        "type": "http",
        "method": "POST",
        "path": f"/internal/approvals/{approval_id}/approve",
        "path_params": {"approval_id": approval_id},
        "query_string": query_string,
        "headers": [(b"content-type", b"application/json")],
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive=receive)


def _post_approve(approval_id: str, *, tenant: str | None = None, body: bytes = b""):
    request = _make_request(approval_id, tenant=tenant, body=body)
    response = asyncio.run(server.approve_via_channel(request))
    return response.status_code, json.loads(bytes(response.body))


def _post_reject(approval_id: str, *, tenant: str | None = None, body: bytes = b""):
    request = _make_request(approval_id, tenant=tenant, body=body)
    response = asyncio.run(server.reject_via_channel(request))
    return response.status_code, json.loads(bytes(response.body))


def test_approve_route_moves_pending_to_approved() -> None:
    approval_id = _seed_pending_approval()

    status_code, data = _post_approve(approval_id)

    assert status_code == 200
    assert data == {"id": approval_id, "status": "approved", "resource_id": "Jane Doe"}
    assert approvals.get(approval_id, TENANT).status == "approved"


def test_reject_route_moves_pending_to_rejected() -> None:
    approval_id = _seed_pending_approval()

    status_code, data = _post_reject(approval_id)

    assert status_code == 200
    assert data["status"] == "rejected"


def test_unknown_approval_id_is_404() -> None:
    status_code, data = _post_approve("does-not-exist")
    assert status_code == 404
    assert "error" in data


def test_deciding_an_already_decided_approval_is_409() -> None:
    approval_id = _seed_pending_approval()
    _post_approve(approval_id)

    status_code, data = _post_approve(approval_id)

    assert status_code == 409
    assert "error" in data


def test_wrong_tenant_cannot_see_or_decide_the_approval() -> None:
    approval_id = _seed_pending_approval(tenant_id="tenant-a")

    status_code, _data = _post_approve(approval_id, tenant="tenant-b")

    assert status_code == 404


def test_approve_with_edits_body_applies_them_before_marking_approved() -> None:
    approval_id = _seed_pending_approval()

    status_code, data = _post_approve(
        approval_id, body=json.dumps({"email": "corrected@example.com"}).encode()
    )

    assert status_code == 200
    assert data["status"] == "approved"
    stored = approvals.get(approval_id, TENANT)
    assert stored.data["email"] == "corrected@example.com"
    assert stored.payload["email"] == "corrected@example.com"
    # Fields not mentioned in the edit body are untouched.
    assert stored.data["vorname"] == "Jane"


def test_approve_with_unknown_edit_field_is_400() -> None:
    approval_id = _seed_pending_approval()

    status_code, data = _post_approve(approval_id, body=json.dumps({"does_not_exist": "x"}).encode())

    assert status_code == 400
    assert "error" in data
    # The approval must remain pending - a bad edit must not silently approve it.
    assert approvals.get(approval_id, TENANT).status == "pending"


def test_approve_with_non_object_body_is_400() -> None:
    approval_id = _seed_pending_approval()

    status_code, data = _post_approve(approval_id, body=b"[1, 2, 3]")

    assert status_code == 400
    assert "error" in data


def test_approve_with_invalid_json_body_is_400() -> None:
    approval_id = _seed_pending_approval()

    status_code, data = _post_approve(approval_id, body=b"not json")

    assert status_code == 400
    assert "error" in data


def test_reject_ignores_body() -> None:
    approval_id = _seed_pending_approval()

    status_code, data = _post_reject(approval_id, body=json.dumps({"email": "x@example.com"}).encode())

    assert status_code == 200
    assert data["status"] == "rejected"
    # reject never applies edits, regardless of what the body contains.
    assert approvals.get(approval_id, TENANT).data["email"] == "jane@example.com"

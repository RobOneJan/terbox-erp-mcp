"""End-to-end (within-process) tests of the prepare -> approve -> execute
flow for the two SevDesk-writing tools, exercised the same way an MCP client
would call the tool functions in server.py - but with TerboxClient stubbed
out so no real HTTP call to the TER BOX CAD Backend is ever made."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from terbox_mcp import approvals, server
from terbox_mcp.models import (
    ApprovalStatusDTO,
    BoxAngebotRequest,
    BoxConfig,
    KundeRequest,
    KundeResponse,
    PendingApprovalDTO,
)


def _ctx(headers: dict[str, str] | None = None) -> SimpleNamespace:
    """Stand-in for mcp.server.mcpserver.Context - server.py only ever reads
    `.headers` off it (see _resolve_tenant_id)."""
    return SimpleNamespace(headers=headers)


class _StubClient:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def post_json(self, path: str, payload: dict) -> Any:
        self.calls.append((path, payload))
        return self.response


@pytest.fixture(autouse=True)
def _no_real_client(monkeypatch: pytest.MonkeyPatch):
    """Safety net: if a test forgets to stub the client, fail loudly instead
    of trying a real network call."""

    def _boom() -> None:
        raise AssertionError("TerboxClient must be stubbed in these tests")

    monkeypatch.setattr(server, "_client", _boom)


def test_ab1000_kunde_returns_pending_shape_without_calling_backend() -> None:
    kunde = KundeRequest(vorname="Jane", nachname="Doe", email="jane@example.com")

    result = server.ab1000_kunde(_ctx(), kunde)

    # Must be the PendingApprovalDTO model, not a plain dict - see that
    # model's docstring: a bare dict leaves structured_content empty and the
    # Teams approval card never gets sent (this broke in production).
    assert isinstance(result, PendingApprovalDTO)
    assert result.status == "pending"
    assert isinstance(result.id, str) and result.id
    assert isinstance(result.message, str) and result.message
    assert result.payload["vorname"] == "Jane"
    assert result.payload["nachname"] == "Doe"
    assert result.payload["email"] == "jane@example.com"
    # payload must be a flat dict[str, str] - generous, not truncated.
    assert all(isinstance(v, str) for v in result.payload.values())


def test_ab1000_kunde_execute_before_approval_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    kunde = KundeRequest(vorname="Jane", nachname="Doe")
    pending = server.ab1000_kunde(_ctx(), kunde)

    result = server.ab1000_kunde_execute(_ctx(), pending.id)

    assert isinstance(result, dict)
    assert result["status_code"] == 409
    assert "error" in result


def test_ab1000_kunde_execute_after_approval_calls_backend_once(monkeypatch: pytest.MonkeyPatch) -> None:
    kunde = KundeRequest(vorname="Jane", nachname="Doe", email="jane@example.com")
    pending = server.ab1000_kunde(_ctx(), kunde)

    approvals.decide(pending.id, approvals.DEFAULT_TENANT_ID, approve=True)

    stub = _StubClient({"contact_id": "c-1", "kundennummer": "K-1", "vorname": "Jane", "nachname": "Doe"})
    monkeypatch.setattr(server, "_client", lambda: stub)

    result = server.ab1000_kunde_execute(_ctx(), pending.id)

    assert isinstance(result, KundeResponse)
    assert result.contact_id == "c-1"
    assert stub.calls == [("/ab1000/kunde", kunde.model_dump(exclude_none=True))]

    # Replay protection: calling execute again must fail, not call the backend twice.
    result_again = server.ab1000_kunde_execute(_ctx(), pending.id)
    assert result_again["status_code"] == 409
    assert len(stub.calls) == 1


def test_ab1000_kunde_execute_applies_human_edit_made_at_approval_time(monkeypatch: pytest.MonkeyPatch) -> None:
    kunde = KundeRequest(vorname="Jane", nachname="Doe", email="typo@example.com")
    pending = server.ab1000_kunde(_ctx(), kunde)

    approvals.decide(
        pending.id,
        approvals.DEFAULT_TENANT_ID,
        approve=True,
        edits={"email": "corrected@example.com"},
    )

    stub = _StubClient({"contact_id": "c-1", "kundennummer": "K-1", "vorname": "Jane", "nachname": "Doe"})
    monkeypatch.setattr(server, "_client", lambda: stub)

    server.ab1000_kunde_execute(_ctx(), pending.id)

    sent_payload = stub.calls[0][1]
    assert sent_payload["email"] == "corrected@example.com"


def test_ab1000_kunde_rejected_cannot_be_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    kunde = KundeRequest(vorname="Jane", nachname="Doe")
    pending = server.ab1000_kunde(_ctx(), kunde)

    approvals.decide(pending.id, approvals.DEFAULT_TENANT_ID, approve=False)

    result = server.ab1000_kunde_execute(_ctx(), pending.id)
    assert result["status_code"] == 409


def test_ab1000_angebot_konfiguration_full_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    request = BoxAngebotRequest(config=BoxConfig(width_mm=1000, depth_mm=1000, height_mm=1000), sevdesk_contact_id="c-1")

    pending = server.ab1000_angebot_konfiguration(_ctx(), request)
    assert isinstance(pending, PendingApprovalDTO)
    assert pending.status == "pending"
    assert pending.payload["sevdesk_contact_id"] == "c-1"
    assert pending.payload["config.width_mm"] == "1000.0"

    # Not approved yet.
    result = server.ab1000_angebot_konfiguration_execute(_ctx(), pending.id)
    assert result["status_code"] == 409

    approvals.decide(pending.id, approvals.DEFAULT_TENANT_ID, approve=True)

    stub = _StubClient({"quote_id": "q-1", "pdf_url": "https://example.com/q-1.pdf"})
    monkeypatch.setattr(server, "_client", lambda: stub)

    result = server.ab1000_angebot_konfiguration_execute(_ctx(), pending.id)
    assert result == {"quote_id": "q-1", "pdf_url": "https://example.com/q-1.pdf"}
    assert stub.calls[0][0] == "/ab1000/angebot-konfiguration"


def test_ab1000_approval_status_reports_pending_then_approved() -> None:
    kunde = KundeRequest(vorname="Jane", nachname="Doe")
    pending = server.ab1000_kunde(_ctx(), kunde)

    status = server.ab1000_approval_status(_ctx(), pending.id)
    assert isinstance(status, ApprovalStatusDTO)
    assert status.status == "pending"
    assert status.payload == pending.payload

    approvals.decide(pending.id, approvals.DEFAULT_TENANT_ID, approve=True)
    status_after = server.ab1000_approval_status(_ctx(), pending.id)
    assert status_after.status == "approved"


def test_ab1000_approval_status_unknown_id_returns_404_shape() -> None:
    status = server.ab1000_approval_status(_ctx(), "does-not-exist")
    assert status["status_code"] == 404
    assert "error" in status


async def test_ab1000_kunde_populates_structured_content_via_real_mcp_call() -> None:
    """Regression test for a bug that reached production (2026-09-28): calling
    the tool function directly (as every other test in this file does) proved
    the *logic* was correct, but missed that this MCP SDK only populates
    `CallToolResult.structured_content` for a tool whose return type is a
    Pydantic model - a bare `-> dict` return serializes fine as text but
    leaves structured_content=None. agent-hub's orchestrator relies
    exclusively on structured_content to detect a pending approval
    (`_looks_like_pending_approval`), so with a dict return the Teams
    approval card was silently never sent - the LLM only knew the id from
    its own text reply. This test goes through `mcp.call_tool(...)`, the
    actual MCP protocol boundary, instead of calling the Python function
    directly, so it would have caught that gap."""
    result = await server.mcp.call_tool(
        "ab1000_kunde", {"kunde": {"vorname": "Jane", "nachname": "Doe", "email": "jane@example.com"}}
    )

    assert result.structured_content is not None, (
        "structured_content is None - the tool is returning a plain dict again, "
        "not a Pydantic model; the Teams approval card will silently never be sent"
    )
    assert result.structured_content["status"] == "pending"
    assert result.structured_content["payload"]["vorname"] == "Jane"


async def test_ab1000_angebot_konfiguration_populates_structured_content_via_real_mcp_call() -> None:
    result = await server.mcp.call_tool(
        "ab1000_angebot_konfiguration",
        {
            "request": {
                "config": {"width_mm": 1000, "depth_mm": 1000, "height_mm": 1000},
                "sevdesk_contact_id": "c-1",
            }
        },
    )

    assert result.structured_content is not None
    assert result.structured_content["status"] == "pending"


async def test_ab1000_approval_status_populates_structured_content_via_real_mcp_call() -> None:
    """`ab1000_approval_status` returns `ApprovalStatusDTO | dict` (the dict
    arm is the not-found error shape, matching this file's existing error
    convention) - a Union return type makes the SDK wrap structured_content
    as `{"result": {...}}` instead of the fields directly, since it can't
    build one flat schema across both arms. Harmless here (nothing reads
    this tool's structured_content structurally - _looks_like_pending_approval
    checks `data.get("id")` at the top level, so the nesting means it's
    simply never mistaken for a fresh pending approval), but worth asserting
    explicitly so a future refactor doesn't accidentally "fix" this into the
    unwrapped shape and silently change behavior."""
    pending = server.ab1000_kunde(_ctx(), KundeRequest(vorname="Jane", nachname="Doe"))

    result = await server.mcp.call_tool("ab1000_approval_status", {"approval_id": pending.id})

    assert result.structured_content is not None
    assert result.structured_content["result"]["status"] == "pending"
    assert "id" not in result.structured_content


def test_tenant_header_partitions_approvals() -> None:
    kunde = KundeRequest(vorname="Jane", nachname="Doe")
    pending = server.ab1000_kunde(_ctx(headers={"X-Tenant-Id": "tenant-a"}), kunde)

    # A different tenant cannot see or execute it.
    other = server.ab1000_approval_status(_ctx(headers={"X-Tenant-Id": "tenant-b"}), pending.id)
    assert other["status_code"] == 404

    same = server.ab1000_approval_status(_ctx(headers={"X-Tenant-Id": "tenant-a"}), pending.id)
    assert same.status == "pending"

"""In-memory human-approval gate for the SevDesk-writing AB1000 tools.

Mirrors the approval pattern used by the sibling `email-mcp-server`
(`ApprovalService` / `ApprovalRequest` / `ApprovalStatus` there), simplified
to match this repo's flat, synchronous, single-file style: no
domain/application/ports layering, just a module-level dict guarded by
nothing (this server runs as a single process/instance, the same assumption
`client.py`/`server.py` already make elsewhere).

Flow: a "prepare" tool (`ab1000_kunde`, `ab1000_angebot_konfiguration` in
`server.py`) validates its input with the existing Pydantic models and
stores it here as a PENDING request instead of calling the SevDesk-backed
API directly. A human decides out of band by calling this server's
`/internal/approvals/{id}/approve` or `/reject` HTTP route (never an MCP
tool - see `server.py`), which flips the stored status and, optionally,
applies field edits the human made before approving. Only then may the
matching "execute" tool (`ab1000_kunde_execute`,
`ab1000_angebot_konfiguration_execute`) consume the approval and perform the
actual SevDesk write - and it can do so only once: `consume_approved` marks
the request CONSUMED so it can never be replayed into a second write.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

DEFAULT_TENANT_ID = "default"
DEFAULT_TTL = timedelta(minutes=30)


class ApprovalStatus:
    """Lifecycle of a pending SevDesk write.

    Plain string constants (not an Enum) to keep this module dependency-free
    of the sibling repo - these are the exact strings sent over the wire
    (MCP tool results and the approve/reject HTTP responses).
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    # Set once an APPROVED request has been used to perform its SevDesk
    # write, so it can never be replayed to trigger a second write.
    CONSUMED = "consumed"


class ApprovalError(Exception):
    """Base class for approval-flow errors."""


class ApprovalNotFoundError(ApprovalError):
    """No approval request exists for this id (or it belongs to another tenant)."""


class ApprovalStateError(ApprovalError):
    """The approval exists but is not in the state the caller needs it to be in."""


class ApprovalEditError(ApprovalError):
    """A field edit supplied at approval time could not be applied."""


@dataclass
class ApprovalRequest:
    id: str
    tenant_id: str
    action: str
    resource_id: str
    status: str
    data: dict[str, Any]
    payload: dict[str, str]
    created_at: datetime
    expires_at: datetime


# Single-process, in-memory store - same complexity level as the rest of this
# server (no database, no file-backed persistence). A restart drops any
# pending approvals, which is acceptable: the LLM re-runs the prepare tool.
_store: dict[str, ApprovalRequest] = {}


def create(tenant_id: str, action: str, resource_id: str, data: dict[str, Any]) -> ApprovalRequest:
    """Store `data` (a validated model's `model_dump(exclude_none=True)`) as a
    new PENDING approval and return it. `payload` is derived from `data` via
    `_flatten` so a human can review every field, however nested."""
    now = datetime.now(timezone.utc)
    request = ApprovalRequest(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        action=action,
        resource_id=resource_id,
        status=ApprovalStatus.PENDING,
        data=data,
        payload=_flatten(data),
        created_at=now,
        expires_at=now + DEFAULT_TTL,
    )
    _store[request.id] = request
    return request


def get(approval_id: str, tenant_id: str) -> ApprovalRequest:
    """Raises ApprovalNotFoundError if `approval_id` is unknown or belongs to
    a different tenant than `tenant_id` - both look identical to the caller,
    on purpose, so this never leaks whether an id exists for someone else."""
    request = _store.get(approval_id)
    if request is None or request.tenant_id != tenant_id:
        raise ApprovalNotFoundError(approval_id)
    return _resolve_expiry(request)


def decide(
    approval_id: str,
    tenant_id: str,
    *,
    approve: bool,
    edits: dict[str, str] | None = None,
) -> ApprovalRequest:
    """Called only from the `/internal/approvals/.../approve|reject` HTTP
    routes in `server.py`, never from an MCP tool - see that module's
    `_decide_from_channel` for why that boundary matters.

    `edits` (approve only) is a subset of the keys `_flatten` produced for
    this request's `payload`, carrying values a human changed before
    approving (e.g. a corrected email address or price). They are applied to
    the stored `data` atomically with the approve decision, before the
    request is marked APPROVED.
    """
    request = get(approval_id, tenant_id)
    if request.status != ApprovalStatus.PENDING:
        raise ApprovalStateError(f"approval {approval_id} is {request.status}, not pending")
    if approve and edits:
        _apply_edits(request, edits)
    request.status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
    return request


def consume_approved(approval_id: str, tenant_id: str, action: str) -> ApprovalRequest:
    """Verify `approval_id` grants `action`, then atomically mark it CONSUMED.

    Raises ApprovalNotFoundError / ApprovalStateError if the write must not
    proceed (unknown id, wrong tenant, wrong action, still pending, rejected,
    expired, or already consumed). Only returns normally - and only then
    marks the request CONSUMED - if the execute tool may go ahead. This is
    what stops a replay of the same approval_id from triggering a second
    SevDesk write.
    """
    request = get(approval_id, tenant_id)
    if request.action != action:
        raise ApprovalStateError(
            f"approval {approval_id} was granted for {request.action}, not {action}"
        )
    if request.status != ApprovalStatus.APPROVED:
        raise ApprovalStateError(f"approval {approval_id} is {request.status}, not approved")
    request.status = ApprovalStatus.CONSUMED
    return request


def _resolve_expiry(request: ApprovalRequest) -> ApprovalRequest:
    if (
        request.status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)
        and datetime.now(timezone.utc) > request.expires_at
    ):
        request.status = ApprovalStatus.EXPIRED
    return request


# --- flatten/edit helpers -----------------------------------------------
#
# Let a human review (and correct) a nested Pydantic model's dump - e.g.
# BoxAngebotRequest's `config.wall_material` or `extras[0].einzelpreis` - as
# a flat dict[str, str], without this module knowing anything about
# KundeRequest/BoxAngebotRequest/BoxConfig.

_PATH_TOKEN_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def _flatten(value: Any, prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            flat.update(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            flat.update(_flatten(item, f"{prefix}[{index}]"))
    else:
        flat[prefix] = "" if value is None else str(value)
    return flat


def _walk_path(path: str) -> list[str | int]:
    tokens: list[str | int] = [int(index) if index else name for name, index in _PATH_TOKEN_RE.findall(path)]
    if not tokens:
        raise ApprovalEditError(f"invalid field path: {path!r}")
    return tokens


def _coerce_like(original: Any, raw_value: str) -> Any:
    """Coerce an edited string value back to the type it is replacing, so a
    later `Model.model_validate(request.data)` gets the same types it would
    have gotten from the original, un-edited model dump."""
    if isinstance(original, bool):
        return raw_value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(original, int):
        try:
            return int(raw_value)
        except ValueError:
            return raw_value
    if isinstance(original, float):
        try:
            return float(raw_value)
        except ValueError:
            return raw_value
    return raw_value


def _apply_edits(request: ApprovalRequest, edits: dict[str, str]) -> None:
    for path, raw_value in edits.items():
        tokens = _walk_path(path)
        target: Any = request.data
        try:
            for token in tokens[:-1]:
                target = target[token]
            last = tokens[-1]
            target[last] = _coerce_like(target[last], raw_value)
        except (KeyError, IndexError, TypeError) as exc:
            raise ApprovalEditError(f"unknown field to edit: {path!r}") from exc
    # Keep the human-reviewable snapshot in sync with the edits so a
    # subsequent ab1000_approval_status call reflects what was actually
    # applied.
    request.payload = _flatten(request.data)

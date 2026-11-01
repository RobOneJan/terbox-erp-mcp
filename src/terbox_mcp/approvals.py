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

The same gate also covers the Odoo write tools (`odoo_tools.py`). Those differ in
two ways: they carry file contents, kept in `blobs` so they never reach the
reviewable `payload` that agent-hub renders as a Teams card; and a human may answer
hours after an email arrived, which is why they use a longer ttl and why
`APPROVAL_STORE_DIR` (a mounted volume in production, same stopgap as
email-mcp-server's) can make requests survive this server scaling to zero.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    blobs: list[bytes] = field(default_factory=list)


# Single-process store. In memory only unless APPROVAL_STORE_DIR is set; without
# it a restart drops any pending approvals and the LLM re-runs the prepare tool.
_store: dict[str, ApprovalRequest] = {}


def _store_dir() -> Path | None:
    configured = os.environ.get("APPROVAL_STORE_DIR")
    return Path(configured) if configured else None


def _persist(request: ApprovalRequest) -> None:
    directory = _store_dir()
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    for index, blob in enumerate(request.blobs):
        (directory / f"{request.id}.{index}.bin").write_bytes(blob)
    record = {
        "id": request.id,
        "tenant_id": request.tenant_id,
        "action": request.action,
        "resource_id": request.resource_id,
        "status": request.status,
        "data": request.data,
        "created_at": request.created_at.isoformat(),
        "expires_at": request.expires_at.isoformat(),
        "blob_count": len(request.blobs),
    }
    (directory / f"{request.id}.json").write_text(json.dumps(record))


def _load(approval_id: str) -> ApprovalRequest | None:
    directory = _store_dir()
    if directory is None:
        return None
    try:
        # The id comes from the caller and ends up in a path.
        normalized = str(uuid.UUID(approval_id))
    except ValueError:
        return None
    path = directory / f"{normalized}.json"
    if not path.exists():
        return None
    record = json.loads(path.read_text())
    request = ApprovalRequest(
        id=record["id"],
        tenant_id=record["tenant_id"],
        action=record["action"],
        resource_id=record["resource_id"],
        status=record["status"],
        data=record["data"],
        payload=_flatten(record["data"]),
        created_at=datetime.fromisoformat(record["created_at"]),
        expires_at=datetime.fromisoformat(record["expires_at"]),
        blobs=[(directory / f"{normalized}.{i}.bin").read_bytes() for i in range(record["blob_count"])],
    )
    _store[request.id] = request
    return request


def discard_blobs(request: ApprovalRequest) -> None:
    """Drop file contents once a request can no longer need them."""
    directory = _store_dir()
    if directory is not None:
        for index in range(len(request.blobs)):
            (directory / f"{request.id}.{index}.bin").unlink(missing_ok=True)
    request.blobs = []
    _persist(request)


def create(
    tenant_id: str,
    action: str,
    resource_id: str,
    data: dict[str, Any],
    *,
    blobs: list[bytes] | None = None,
    ttl: timedelta = DEFAULT_TTL,
) -> ApprovalRequest:
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
        expires_at=now + ttl,
        blobs=blobs or [],
    )
    _store[request.id] = request
    _persist(request)
    return request


def get(approval_id: str, tenant_id: str) -> ApprovalRequest:
    """Raises ApprovalNotFoundError if `approval_id` is unknown or belongs to
    a different tenant than `tenant_id` - both look identical to the caller,
    on purpose, so this never leaks whether an id exists for someone else."""
    request = _store.get(approval_id) or _load(approval_id)
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
    if approve:
        _persist(request)
    else:
        discard_blobs(request)
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
    _persist(request)
    return request


def _resolve_expiry(request: ApprovalRequest) -> ApprovalRequest:
    if (
        request.status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)
        and datetime.now(timezone.utc) > request.expires_at
    ):
        request.status = ApprovalStatus.EXPIRED
        discard_blobs(request)
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

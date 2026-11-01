"""The odoo_* tools against a fake Odoo served through httpx.MockTransport, so
the real client code (URLs, payloads, guards) runs but nothing reaches a network.
Every test can assert which write methods Odoo actually received."""

from __future__ import annotations

import base64
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from terbox_mcp import approvals, odoo_tools, server
from terbox_mcp.models import PendingApprovalDTO
from terbox_mcp.odoo_client import OdooAccessDenied, OdooClient, OdooError, OdooSettings
from terbox_mcp.odoo_models import (
    AnhangAblageRequest,
    EingangsrechnungRequest,
    EinkaufspreisRequest,
    MaterialRequest,
    PreisPruefPosition,
)

PDF = b"%PDF-1.7 test invoice"
PDF_B64 = base64.b64encode(PDF).decode()
TENANT = approvals.DEFAULT_TENANT_ID
WRITE_METHODS = {"create", "write", "unlink"}


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(headers=None)


class FakeOdoo:
    """Records every call; answers from `responses`, keyed (model, method)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.responses: dict[tuple[str, str], object] = {}
        self.stored_checksum = hashlib.sha1(PDF).hexdigest()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        _, _, model, method = request.url.path.strip("/").split("/")
        body = json.loads(request.content or b"{}")
        self.calls.append((model, method, body))
        if (model, method) == ("ir.attachment", "read"):
            return httpx.Response(200, json=[{"id": 900, "checksum": self.stored_checksum}])
        answer = self.responses.get((model, method))
        if answer is None:
            answer = [1000 + len(self.calls)] if method == "create" else True if method in WRITE_METHODS else []
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    def writes(self) -> list[tuple[str, str]]:
        return [(model, method) for model, method, _ in self.calls if method in WRITE_METHODS]

    def body_of(self, model: str, method: str) -> dict:
        return next(body for m, name, body in self.calls if (m, name) == (model, method))


@pytest.fixture
def odoo(monkeypatch: pytest.MonkeyPatch) -> FakeOdoo:
    fake = FakeOdoo()
    settings = OdooSettings(base_url="https://odoo.test", api_key="k", database="db")
    monkeypatch.setattr(odoo_tools, "_odoo", lambda: OdooClient(settings, transport=httpx.MockTransport(fake)))
    return fake


def _bill(**overrides) -> EingangsrechnungRequest:
    values = {
        "lieferant_id": 26,
        "rechnungsnummer": "RE-1",
        "rechnungsdatum": "2026-10-20",
        "positionen": [{"beschreibung": "Blech", "menge": 2, "einzelpreis": 10.5, "steuer_prozent": 19}],
        "anhaenge": [{"dateiname": "re-1.pdf", "inhalt_base64": PDF_B64}],
    }
    return EingangsrechnungRequest(**{**values, **overrides})


def _prepare_bill(odoo: FakeOdoo, **overrides) -> PendingApprovalDTO:
    odoo.responses[("res.partner", "read")] = [{"id": 26, "name": "JTK"}]
    odoo.responses[("account.tax", "search_read")] = [{"id": 25, "name": "19% I", "amount": 19.0}]
    odoo.responses[("res.currency", "search_read")] = [{"id": 126}]
    return odoo_tools.odoo_eingangsrechnung_anlegen(_ctx(), _bill(**overrides))


# --- read guards -------------------------------------------------------------


@pytest.mark.parametrize("model", ["ir.config_parameter", "res.users.apikeys", "auth_totp.device", "payment.provider"])
def test_denied_models_never_reach_odoo(odoo: FakeOdoo, model: str) -> None:
    result = odoo_tools.odoo_suchen(model, ["id"])
    assert result["status_code"] == 403
    assert odoo.calls == []


@pytest.mark.parametrize(
    "fields",
    [["password"], ["user_id.password"], ["raw"], ["datas"], ["api_key_ids"]],
)
def test_denied_fields_never_reach_odoo(odoo: FakeOdoo, fields: list[str]) -> None:
    result = odoo_tools.odoo_suchen("res.users", fields)
    assert result["status_code"] == 403
    assert odoo.calls == []


@pytest.mark.parametrize(
    "domain",
    [
        [["password", "=like", "a%"]],
        ["|", ["id", "=", 1], ["user_id.password", "=like", "a%"]],
        [["user_id", "any", [["password", "=like", "a%"]]]],
    ],
)
def test_domain_cannot_probe_denied_fields(odoo: FakeOdoo, domain: list) -> None:
    result = odoo_tools.odoo_zaehlen("res.partner", domain)
    assert result["status_code"] == 403
    assert odoo.calls == []


def test_aggregate_and_sort_cannot_touch_denied_fields(odoo: FakeOdoo) -> None:
    assert odoo_tools.odoo_gruppieren("res.users", ["id"], ["password:max"])["status_code"] == 403
    assert odoo_tools.odoo_suchen("res.users", ["id"], sortierung="password desc")["status_code"] == 403
    assert odoo.calls == []


def test_search_requires_fields_and_caps_limit(odoo: FakeOdoo) -> None:
    assert odoo_tools.odoo_suchen("res.partner", [])["status_code"] == 400
    odoo_tools.odoo_suchen("res.partner", ["name"], limit=100000)
    assert odoo.body_of("res.partner", "search_read")["limit"] == 200


def test_long_values_are_omitted_from_generic_reads(odoo: FakeOdoo) -> None:
    odoo.responses[("res.partner", "search_read")] = [{"id": 1, "image_1920": "x" * 6000, "name": "A"}]
    record = odoo_tools.odoo_suchen("res.partner", ["image_1920", "name"])["datensaetze"][0]
    assert record["name"] == "A"
    assert "omitted" in record["image_1920"]


def test_client_exposes_no_generic_write() -> None:
    public = {name for name in dir(OdooClient) if not name.startswith("_")}
    assert not public.intersection({"create", "write", "unlink", "call", "execute"})


def test_reads_never_write(odoo: FakeOdoo) -> None:
    odoo.responses[("account.move", "formatted_read_group")] = [{"__count": 2, "amount_residual:sum": 50.0}]
    odoo.responses[("account.move", "search_count")] = 1
    for model in ("sale.order", "purchase.order", "product.template", "res.partner", "product.supplierinfo"):
        odoo.responses[(model, "search_count")] = 3
    overview = odoo_tools.odoo_firmenueberblick()
    assert overview["eingangsrechnungen"]["offen_betrag"] == 50.0
    assert overview["stammdaten"]["artikel"] == 3
    assert odoo.writes() == []


def test_overview_keeps_other_sections_when_one_fails(odoo: FakeOdoo) -> None:
    odoo.responses[("sale.order", "search_count")] = httpx.Response(403, json={"message": "no access"})
    overview = odoo_tools.odoo_firmenueberblick()
    assert "fehler" in overview["verkauf"]
    assert "fehler" not in overview["einkauf"]


# --- vendor bill flow --------------------------------------------------------


def test_prepare_bill_writes_nothing_and_keeps_file_off_the_card(odoo: FakeOdoo) -> None:
    pending = _prepare_bill(odoo)

    assert isinstance(pending, PendingApprovalDTO)
    assert pending.status == "pending"
    assert odoo.writes() == []
    assert pending.payload["positionen[0].steuer_name"] == "19% I"
    assert pending.payload["anhaenge[0].dateiname"] == "re-1.pdf"
    assert not any(PDF_B64 in value for value in pending.payload.values())
    assert "inhalt_base64" not in " ".join(pending.payload)


def test_execute_before_approval_writes_nothing(odoo: FakeOdoo) -> None:
    pending = _prepare_bill(odoo)
    result = odoo_tools.odoo_eingangsrechnung_anlegen_execute(_ctx(), pending.id)
    assert result["status_code"] == 409
    assert odoo.writes() == []


def test_full_bill_flow_creates_draft_with_verified_attachment(odoo: FakeOdoo) -> None:
    pending = _prepare_bill(odoo)
    approvals.decide(pending.id, TENANT, approve=True)

    result = odoo_tools.odoo_eingangsrechnung_anlegen_execute(_ctx(), pending.id)

    assert result["rechnung_angelegt"] is True
    vals = odoo.body_of("account.move", "create")["vals_list"][0]
    assert vals["move_type"] == "in_invoice"
    assert vals["ref"] == "RE-1"
    assert vals["invoice_line_ids"][0][2]["tax_ids"] == [[6, 0, [25]]]
    attachment = odoo.body_of("ir.attachment", "create")["vals_list"][0]
    assert base64.b64decode(attachment["raw"]) == PDF
    assert "datas" not in attachment  # the field no longer exists on saas~19.4
    assert attachment["mimetype"] == "application/pdf"
    # Never posted: no action_post or anything else beyond these writes.
    assert {call[1] for call in odoo.calls} <= {"read", "search_read", "create", "write"}

    again = odoo_tools.odoo_eingangsrechnung_anlegen_execute(_ctx(), pending.id)
    assert again["status_code"] == 409


def test_every_card_field_can_come_back_as_an_edit(odoo: FakeOdoo) -> None:
    """agent-hub's Adaptive Card returns ALL payload fields on approve, not just
    changed ones. A payload key that isn't a valid edit path would make every
    approval fail with a 400."""
    pending = _prepare_bill(odoo)
    approvals.decide(pending.id, TENANT, approve=True, edits=dict(pending.payload))
    result = odoo_tools.odoo_eingangsrechnung_anlegen_execute(_ctx(), pending.id)
    assert result["rechnung_angelegt"] is True


def test_human_can_correct_tax_and_filename_on_the_card(odoo: FakeOdoo) -> None:
    pending = _prepare_bill(odoo)
    approvals.decide(
        pending.id,
        TENANT,
        approve=True,
        edits={"positionen[0].steuer_name": "19% EU", "anhaenge[0].dateiname": "../korrigiert.pdf"},
    )
    odoo_tools.odoo_eingangsrechnung_anlegen_execute(_ctx(), pending.id)

    tax_lookup = [body for m, name, body in odoo.calls if (m, name) == ("account.tax", "search_read")][-1]
    assert ["name", "=", "19% EU"] in tax_lookup["domain"]
    assert odoo.body_of("ir.attachment", "create")["vals_list"][0]["name"] == "korrigiert.pdf"


def test_invalid_edit_fails_without_writing(odoo: FakeOdoo) -> None:
    pending = _prepare_bill(odoo)
    approvals.decide(pending.id, TENANT, approve=True, edits={"rechnungsdatum": "kein datum"})
    result = odoo_tools.odoo_eingangsrechnung_anlegen_execute(_ctx(), pending.id)
    assert result["status_code"] == 422
    assert odoo.writes() == []


def test_existing_bill_is_not_created_twice(odoo: FakeOdoo) -> None:
    odoo.responses[("account.move", "search_read")] = [{"id": 77}]
    pending = _prepare_bill(odoo)
    assert "existiert bereits" in pending.message
    approvals.decide(pending.id, TENANT, approve=True)

    result = odoo_tools.odoo_eingangsrechnung_anlegen_execute(_ctx(), pending.id)

    assert result["rechnung_id"] == 77
    assert result["rechnung_angelegt"] is False
    assert ("account.move", "create") not in odoo.writes()
    assert ("ir.attachment", "create") in odoo.writes()


def test_corrupted_upload_is_removed_and_reported(odoo: FakeOdoo) -> None:
    pending = _prepare_bill(odoo)
    approvals.decide(pending.id, TENANT, approve=True)
    odoo.stored_checksum = "not-the-real-sha1"

    result = odoo_tools.odoo_eingangsrechnung_anlegen_execute(_ctx(), pending.id)

    assert "corrupted" in result["error"]
    assert result["rechnung_id"]  # the error names the bill that does exist
    assert ("ir.attachment", "unlink") in odoo.writes()


def test_new_supplier_is_only_created_after_approval(odoo: FakeOdoo) -> None:
    pending = _prepare_bill(odoo, lieferant_id=None, lieferant={"name": "Neu GmbH"})
    assert "neu angelegt" in pending.message
    assert odoo.writes() == []
    approvals.decide(pending.id, TENANT, approve=True)

    result = odoo_tools.odoo_eingangsrechnung_anlegen_execute(_ctx(), pending.id)

    assert result["lieferant_angelegt"] is True
    vals = odoo.body_of("res.partner", "create")["vals_list"][0]
    assert vals["name"] == "Neu GmbH"
    assert vals["supplier_rank"] == 1


@pytest.mark.parametrize("content", [b"MZ\x90 not a document", b"<html>hi</html>"])
def test_only_document_files_are_accepted(content: bytes) -> None:
    with pytest.raises(ValueError):
        _bill(anhaenge=[{"dateiname": "x.pdf", "inhalt_base64": base64.b64encode(content).decode()}])


def test_rejected_or_wrong_action_approval_cannot_execute(odoo: FakeOdoo) -> None:
    pending = _prepare_bill(odoo)
    approvals.decide(pending.id, TENANT, approve=True)
    # An approval for a bill must not unlock a price change.
    assert odoo_tools.odoo_einkaufspreis_aendern_execute(_ctx(), pending.id)["status_code"] == 409

    other = _prepare_bill(odoo)
    approvals.decide(other.id, TENANT, approve=False)
    assert odoo_tools.odoo_eingangsrechnung_anlegen_execute(_ctx(), other.id)["status_code"] == 409
    assert approvals.get(other.id, TENANT).blobs == []


# --- attachments, materials, prices -----------------------------------------


def test_attachments_only_on_allowed_models() -> None:
    with pytest.raises(ValueError):
        AnhangAblageRequest(res_model="res.users", res_id=1, anhaenge=[{"dateiname": "a.pdf", "inhalt_base64": PDF_B64}])


def test_attachment_flow_skips_a_file_already_there(odoo: FakeOdoo) -> None:
    odoo.responses[("purchase.order", "read")] = [{"id": 5, "display_name": "P00005"}]
    pending = odoo_tools.odoo_anhang_ablegen(
        _ctx(),
        AnhangAblageRequest(
            res_model="purchase.order", res_id=5, anhaenge=[{"dateiname": "ab.pdf", "inhalt_base64": PDF_B64}]
        ),
    )
    assert odoo.writes() == []
    approvals.decide(pending.id, TENANT, approve=True, edits=dict(pending.payload))
    odoo.responses[("ir.attachment", "search_read")] = [{"id": 3}]

    result = odoo_tools.odoo_anhang_ablegen_execute(_ctx(), pending.id)

    assert result["anhaenge"][0]["added"] is False
    assert odoo.writes() == []


def test_material_flow(odoo: FakeOdoo) -> None:
    odoo.responses[("product.category", "search_read")] = [{"id": 7}]
    odoo.responses[("uom.uom", "search_read")] = [{"id": 35}]
    odoo.responses[("res.partner", "read")] = [{"id": 26, "name": "JTK"}]
    pending = odoo_tools.odoo_material_anlegen(
        _ctx(),
        MaterialRequest(
            artikel_referenz="P00099", name="Neues Blech", kategorie="Produkte", lieferant_id=26, einkaufspreis=4.2
        ),
    )
    assert odoo.writes() == []
    approvals.decide(pending.id, TENANT, approve=True, edits=dict(pending.payload))

    result = odoo_tools.odoo_material_anlegen_execute(_ctx(), pending.id)

    assert result["angelegt"] is True
    assert odoo.body_of("product.template", "create")["vals_list"][0]["uom_id"] == 35
    assert odoo.body_of("product.supplierinfo", "create")["vals_list"][0]["price"] == 4.2


def test_material_with_taken_reference_is_refused(odoo: FakeOdoo) -> None:
    odoo.responses[("product.template", "search_read")] = [{"id": 1, "name": "x", "default_code": "P1"}]
    with pytest.raises(OdooError):
        odoo_tools.odoo_material_anlegen(_ctx(), MaterialRequest(artikel_referenz="P1", name="x", kategorie="Produkte"))


def _prepare_price(odoo: FakeOdoo, current: float = 10.0) -> PendingApprovalDTO:
    odoo.responses[("product.supplierinfo", "read")] = [
        {"id": 3, "partner_id": [26, "JTK"], "product_tmpl_id": [22, "[F1] Blech"], "price": current}
    ]
    return odoo_tools.odoo_einkaufspreis_aendern(_ctx(), EinkaufspreisRequest(lieferantenpreis_id=3, neuer_preis=12.5))


def test_price_change_flow(odoo: FakeOdoo) -> None:
    pending = _prepare_price(odoo)
    assert pending.payload["alter_preis"] == "10.0"
    assert odoo.writes() == []
    approvals.decide(pending.id, TENANT, approve=True, edits=dict(pending.payload))

    result = odoo_tools.odoo_einkaufspreis_aendern_execute(_ctx(), pending.id)

    assert result["neuer_preis"] == 12.5
    assert odoo.body_of("product.supplierinfo", "write") == {"ids": [3], "vals": {"price": 12.5}}


def test_price_changed_in_the_meantime_is_refused(odoo: FakeOdoo) -> None:
    pending = _prepare_price(odoo)
    approvals.decide(pending.id, TENANT, approve=True)
    odoo.responses[("product.supplierinfo", "read")] = [{"id": 3, "price": 11.0}]

    result = odoo_tools.odoo_einkaufspreis_aendern_execute(_ctx(), pending.id)

    assert result["status_code"] == 409
    assert odoo.writes() == []


def test_price_check_statuses(odoo: FakeOdoo) -> None:
    odoo.responses[("product.supplierinfo", "search_read")] = [
        {"id": 1, "product_tmpl_id": [10, "A"], "product_code": "L-A", "price": 10.0, "currency_id": [1, "EUR"]},
        {"id": 2, "product_tmpl_id": [11, "B"], "product_code": False, "price": 20.0, "currency_id": [1, "EUR"]},
        {"id": 3, "product_tmpl_id": [12, "C"], "product_code": "L-C", "price": 0.0, "currency_id": [1, "EUR"]},
    ]
    odoo.responses[("product.template", "read")] = [
        {"id": 10, "default_code": "P1"},
        {"id": 11, "default_code": "P2"},
        {"id": 12, "default_code": "P3"},
    ]
    result = odoo_tools.odoo_preise_pruefen(
        26,
        [
            PreisPruefPosition(lieferanten_artikelnummer="L-A", einzelpreis=10.05),
            PreisPruefPosition(artikel_referenz="P2", einzelpreis=25.0),
            PreisPruefPosition(lieferanten_artikelnummer="L-C", einzelpreis=5.0),
            PreisPruefPosition(lieferanten_artikelnummer="L-X", einzelpreis=1.0),
        ],
    )
    statuses = [p["status"] for p in result["positionen"]]
    assert statuses == ["ok", "abweichung", "kein_preis", "nicht_gefunden"]
    assert result["positionen"][1]["lieferantenpreis_id"] == 2
    assert result["positionen"][1]["abweichung_prozent"] == 25.0
    assert odoo.writes() == []


# --- persistence across a restart --------------------------------------------


def test_pending_approval_with_file_survives_restart(
    odoo: FakeOdoo, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("APPROVAL_STORE_DIR", str(tmp_path))
    pending = _prepare_bill(odoo)

    approvals._store.clear()  # what scaling to zero does to the in-memory store

    approvals.decide(pending.id, TENANT, approve=True)
    approvals._store.clear()
    result = odoo_tools.odoo_eingangsrechnung_anlegen_execute(_ctx(), pending.id)
    assert result["rechnung_angelegt"] is True
    assert base64.b64decode(odoo.body_of("ir.attachment", "create")["vals_list"][0]["raw"]) == PDF

    # The file does not linger after use, and a replay still fails after a restart.
    assert list(tmp_path.glob("*.bin")) == []
    approvals._store.clear()
    assert odoo_tools.odoo_eingangsrechnung_anlegen_execute(_ctx(), pending.id)["status_code"] == 409


def test_store_rejects_path_traversal_ids(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("APPROVAL_STORE_DIR", str(tmp_path))
    (tmp_path.parent / "secret.json").write_text("{}")
    with pytest.raises(approvals.ApprovalNotFoundError):
        approvals.get("../secret", TENANT)


# --- the real MCP boundary agent-hub sees ------------------------------------


async def test_pending_approval_is_detectable_by_agent_hub(odoo: FakeOdoo) -> None:
    """Goes through mcp.call_tool, like the regression test in
    test_server_approval_flow.py: agent-hub only looks at structured_content."""
    odoo.responses[("product.supplierinfo", "read")] = [
        {"id": 3, "partner_id": [26, "JTK"], "product_tmpl_id": [22, "Blech"], "price": 10.0}
    ]
    result = await server.mcp.call_tool(
        "odoo_einkaufspreis_aendern", {"aenderung": {"lieferantenpreis_id": 3, "neuer_preis": 12.5}}
    )
    data = result.structured_content
    assert isinstance(data["id"], str)
    assert data["status"] == "pending"
    assert isinstance(data["message"], str)


async def test_bill_approval_is_detectable_by_agent_hub(odoo: FakeOdoo) -> None:
    odoo.responses[("res.partner", "read")] = [{"id": 26, "name": "JTK"}]
    result = await server.mcp.call_tool(
        "odoo_eingangsrechnung_anlegen",
        {
            "rechnung": {
                "lieferant_id": 26,
                "rechnungsnummer": "RE-1",
                "rechnungsdatum": "2026-10-20",
                "anhaenge": [{"dateiname": "a.pdf", "inhalt_base64": PDF_B64}],
            }
        },
    )
    assert result.structured_content["status"] == "pending"
    assert PDF_B64 not in json.dumps(result.structured_content)


async def test_attachment_is_detectable_by_agent_hub(odoo: FakeOdoo) -> None:
    odoo.responses[("ir.attachment", "search_read")] = [
        {"id": 9, "name": "re.pdf", "mimetype": "application/pdf", "file_size": 21, "raw": PDF_B64}
    ]
    result = await server.mcp.call_tool("odoo_anhang_lesen", {"anhang_id": 9})
    data = result.structured_content
    assert data["filename"] == "re.pdf"
    assert data["content_type"] == "application/pdf"
    assert data["content_base64"] == PDF_B64


async def test_prepare_failure_tells_the_agent_why(odoo: FakeOdoo) -> None:
    odoo.responses[("product.supplierinfo", "read")] = []
    with pytest.raises(ToolError, match="supplier price 3 not found"):
        await server.mcp.call_tool(
            "odoo_einkaufspreis_aendern", {"aenderung": {"lieferantenpreis_id": 3, "neuer_preis": 1}}
        )
    assert approvals._store == {}


def test_access_denied_is_an_odoo_error() -> None:
    assert issubclass(OdooAccessDenied, OdooError)

"""The odoo_* MCP tools, registered on the server in `server.py`.

Reads run immediately and cover every model not denied in `odoo_client.py`.
Writes follow the same prepare -> human approval -> execute flow as the SevDesk
tools: a prepare tool validates and returns a pending approval, agent-hub shows
it as a Teams card, and only the matching *_execute tool touches Odoo.

Prepare tools return a bare `PendingApprovalDTO` and raise on failure instead of
returning an error dict: a Union return type nests `structured_content` under
"result", and agent-hub would no longer detect the pending approval (see that
model's docstring). `odoo_anhang_lesen` is bare for the same reason.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from mcp.server.mcpserver import Context
from pydantic import ValidationError

from terbox_mcp import approvals
from terbox_mcp.models import ApprovalStatusDTO, PendingApprovalDTO
from terbox_mcp.odoo_client import DEFAULT_LIMIT, OdooClient, OdooError
from terbox_mcp.odoo_models import (
    AnhangAblageDaten,
    AnhangAblageRequest,
    AttachmentDTO,
    EingangsrechnungDaten,
    EingangsrechnungRequest,
    EinkaufspreisRequest,
    MaterialRequest,
    PreisPruefPosition,
    VerkaufspreisRequest,
    safe_filename,
    sniff_content_type,
)
from terbox_mcp.server import _approval_error, _pending_response, _resolve_tenant_id, ab1000_approval_status, mcp

# Someone answers an approval when they next look at Teams, not within minutes.
ODOO_APPROVAL_TTL = timedelta(days=3)


def _odoo() -> OdooClient:
    return OdooClient()


def _error(exc: OdooError) -> dict:
    return {"error": str(exc), "status_code": exc.status_code, "detail": exc.detail}


def _invalid(exc: ValidationError) -> dict:
    return {"error": f"invalid data after approval: {exc}", "status_code": 422}


def _m2o_name(value: Any) -> str | None:
    # many2one comes back as [id, display_name], or False when empty.
    return value[1] if isinstance(value, list) and len(value) > 1 else None


# --- reads -----------------------------------------------------------------


@mcp.tool()
def odoo_modelle(suche: str | None = None, limit: int = 200, offset: int = 0) -> dict:
    """List the Odoo models that can be queried, optionally filtered by text in
    the technical or display name (e.g. "purchase", "Rechnung")."""
    try:
        return {"modelle": _odoo().list_models(suche, limit=limit, offset=offset)}
    except OdooError as exc:
        return _error(exc)


@mcp.tool()
def odoo_felder(model: str) -> dict:
    """List the fields of an Odoo model with label, type and relation. Use this
    to find the field names for odoo_suchen, odoo_lesen and odoo_gruppieren."""
    try:
        return {"felder": _odoo().fields_get(model)}
    except OdooError as exc:
        return _error(exc)


@mcp.tool()
def odoo_suchen(
    model: str,
    felder: list[str],
    domain: list | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    sortierung: str | None = None,
) -> dict:
    """Search any Odoo model and return the requested fields.

    domain uses Odoo syntax, e.g. [["move_type", "=", "in_invoice"], ["state",
    "=", "posted"]]. limit is capped at 200; page with offset. sortierung is
    e.g. "invoice_date desc". Common models: res.partner, account.move,
    product.template, product.supplierinfo, purchase.order, sale.order.
    """
    try:
        records = _odoo().search_read(model, domain, felder, limit=limit, offset=offset, order=sortierung)
    except OdooError as exc:
        return _error(exc)
    return {"anzahl": len(records), "datensaetze": records}


@mcp.tool()
def odoo_lesen(model: str, ids: list[int], felder: list[str]) -> dict:
    """Read specific records of any Odoo model by id."""
    try:
        return {"datensaetze": _odoo().read(model, ids, felder)}
    except OdooError as exc:
        return _error(exc)


@mcp.tool()
def odoo_zaehlen(model: str, domain: list | None = None) -> dict:
    """Count the records of any Odoo model matching a domain."""
    try:
        return {"anzahl": _odoo().search_count(model, domain)}
    except OdooError as exc:
        return _error(exc)


@mcp.tool()
def odoo_gruppieren(
    model: str, gruppierung: list[str], aggregate: list[str] | None = None, domain: list | None = None
) -> dict:
    """Aggregate any Odoo model, e.g. totals per supplier or per month.

    gruppierung e.g. ["partner_id"] or ["invoice_date:month"]; aggregate e.g.
    ["amount_total:sum", "__count"]. Sums add up document amounts as they are,
    without currency conversion.
    """
    try:
        return {"gruppen": _odoo().read_group(model, domain, gruppierung, aggregate or ["__count"])}
    except OdooError as exc:
        return _error(exc)


@mcp.tool()
def odoo_namen_suchen(model: str, name: str, limit: int = 10) -> dict:
    """Find records by name the way Odoo's own dropdowns do. The quickest way to
    turn a supplier or product name into an id."""
    try:
        return {"treffer": _odoo().name_search(model, name, limit=limit)}
    except OdooError as exc:
        return _error(exc)


@mcp.tool()
def odoo_anhang_lesen(anhang_id: int) -> AttachmentDTO:
    """Fetch a stored file from Odoo. It is delivered to the user and shown to
    you for this turn. Find ids with odoo_suchen on ir.attachment (fields: name,
    mimetype, file_size, res_model, res_id)."""
    row = _odoo().get_attachment(anhang_id)
    return AttachmentDTO(
        filename=row["name"],
        content_type=row.get("mimetype") or "application/octet-stream",
        content_base64=row.get("raw") or "",
        size_bytes=row.get("file_size") or 0,
    )


def _invoice_section(client: OdooClient, move_type: str, today: str) -> dict:
    base = [["move_type", "=", move_type]]
    open_domain = base + [["state", "=", "posted"], ["payment_state", "in", ["not_paid", "partial"]]]
    totals = client.read_group("account.move", open_domain, [], ["amount_residual:sum", "__count"])
    overdue = client.read_group(
        "account.move", open_domain + [["invoice_date_due", "<", today]], [], ["amount_residual:sum", "__count"]
    )
    return {
        "offen_anzahl": totals[0]["__count"] if totals else 0,
        "offen_betrag": (totals[0].get("amount_residual:sum") or 0) if totals else 0,
        "ueberfaellig_anzahl": overdue[0]["__count"] if overdue else 0,
        "ueberfaellig_betrag": (overdue[0].get("amount_residual:sum") or 0) if overdue else 0,
        "entwuerfe": client.search_count("account.move", base + [["state", "=", "draft"]]),
    }


@mcp.tool()
def odoo_firmenueberblick() -> dict:
    """Snapshot of the company: open and overdue invoices in both directions,
    quotes, sales and purchase orders, master data and maintenance gaps.

    A section Odoo cannot answer shows {"fehler": ...} instead of failing the
    rest. Amounts add up document amounts without currency conversion.
    """
    client = _odoo()
    today = date.today().isoformat()

    def count(model: str, domain: list) -> int:
        return client.search_count(model, domain)

    sections = {
        "eingangsrechnungen": lambda: _invoice_section(client, "in_invoice", today),
        "ausgangsrechnungen": lambda: _invoice_section(client, "out_invoice", today),
        "verkauf": lambda: {
            "offene_angebote": count("sale.order", [["state", "in", ["draft", "sent"]]]),
            "auftraege_abzurechnen": count(
                "sale.order", [["state", "=", "sale"], ["invoice_status", "=", "to invoice"]]
            ),
        },
        "einkauf": lambda: {
            "offene_anfragen": count("purchase.order", [["state", "in", ["draft", "sent"]]]),
            "bestellungen_abzurechnen": count(
                "purchase.order", [["state", "=", "purchase"], ["invoice_status", "=", "to invoice"]]
            ),
        },
        "stammdaten": lambda: {
            "artikel": count("product.template", []),
            "lieferanten": count("res.partner", [["supplier_rank", ">", 0]]),
            "kunden": count("res.partner", [["customer_rank", ">", 0]]),
            "lieferantenpreise_gesamt": count("product.supplierinfo", []),
            "lieferantenpreise_ohne_preis": count("product.supplierinfo", [["price", "=", 0]]),
        },
    }
    overview: dict[str, Any] = {"stand": today}
    for name, load in sections.items():
        try:
            overview[name] = load()
        except OdooError as exc:
            overview[name] = {"fehler": str(exc)}
    return overview


@mcp.tool()
def odoo_preise_pruefen(lieferant_id: int, positionen: list[PreisPruefPosition], toleranz_prozent: float = 1.0) -> dict:
    """Compare prices on a supplier document with the prices stored in Odoo.

    Matches each position by lieferanten_artikelnummer, then artikel_referenz.
    status per position: "ok", "abweichung" (off by more than toleranz_prozent),
    "kein_preis" (no price stored yet) or "nicht_gefunden". Changes nothing; for
    a difference worth correcting, pass lieferantenpreis_id to
    odoo_einkaufspreis_aendern.
    """
    try:
        stored = _odoo().supplier_prices(lieferant_id)
    except OdooError as exc:
        return _error(exc)
    by_supplier_code = {row["product_code"]: row for row in stored if row.get("product_code")}
    by_reference = {row["default_code"]: row for row in stored if row.get("default_code")}

    results = []
    for position in positionen:
        row = by_supplier_code.get(position.lieferanten_artikelnummer) or by_reference.get(position.artikel_referenz)
        result: dict[str, Any] = {**position.model_dump(exclude_none=True)}
        if row is None:
            result["status"] = "nicht_gefunden"
        else:
            current = row.get("price") or 0
            result.update(
                lieferantenpreis_id=row["id"],
                artikel=_m2o_name(row.get("product_tmpl_id")),
                preis_in_odoo=current,
            )
            if not current:
                result["status"] = "kein_preis"
            else:
                difference = (position.einzelpreis - current) / current * 100
                result["abweichung_prozent"] = round(difference, 2)
                result["status"] = "abweichung" if abs(difference) > toleranz_prozent else "ok"
        results.append(result)
    return {"positionen": results}


@mcp.tool()
def odoo_freigabe_status(ctx: Context, approval_id: str) -> ApprovalStatusDTO | dict:
    """Check a pending Odoo approval. Returns id, status, action and payload.
    action names the tool the approval belongs to: call "<action>_execute" once
    status is "approved". Other statuses: "pending", "rejected", "expired" (3
    days without a decision), "consumed" (already executed)."""
    return ab1000_approval_status(ctx, approval_id)


# --- writes: prepare (no Odoo write happens here) ---------------------------


def _split_attachments(anhaenge: list) -> tuple[list[dict], list[bytes]]:
    blobs = [anhang.raw() for anhang in anhaenge]
    meta = [
        {"dateiname": anhang.dateiname, "groesse_kb": round(len(blob) / 1024, 1)}
        for anhang, blob in zip(anhaenge, blobs)
    ]
    return meta, blobs


def _attach_all(client: OdooClient, request: approvals.ApprovalRequest, meta: list[dict], res_model: str, res_id: int) -> list[dict]:
    """Filenames come from the reviewed data (a human may have corrected one),
    contents from the blobs by position - the card can rename a file but never
    change what it contains."""
    stored = []
    for item, blob in zip(meta, request.blobs):
        filename = safe_filename(item["dateiname"])
        content_type = sniff_content_type(blob) or "application/octet-stream"
        stored.append({"dateiname": filename, **client.attach(res_model, res_id, filename, blob, content_type)})
    return stored


@mcp.tool()
def odoo_eingangsrechnung_anlegen(ctx: Context, rechnung: EingangsrechnungRequest) -> PendingApprovalDTO:
    """Request human approval to store a supplier invoice in Odoo as a DRAFT with
    its file(s) attached. Writes nothing yet.

    Look up the supplier first (odoo_namen_suchen on res.partner) and pass
    lieferant_id; use lieferant only for a genuinely new one. With steuer_prozent
    the tax is resolved to a concrete Odoo tax shown on the card; set steuer_name
    for non-domestic suppliers. Once approved, call
    odoo_eingangsrechnung_anlegen_execute. Bills are never posted here: booking
    stays a human decision inside Odoo.
    """
    client = _odoo()
    data = rechnung.model_dump(mode="json", exclude_none=True, exclude={"anhaenge"})
    for position in data["positionen"]:
        if "steuer_prozent" in position:
            tax = client.resolve_purchase_tax(position["steuer_prozent"], position.get("steuer_name"))
            position["steuer_name"] = tax["name"]
    note = ""
    if rechnung.lieferant_id is not None:
        supplier = client.get_one("res.partner", rechnung.lieferant_id, ["name"])
        if supplier is None:
            raise OdooError(404, f"supplier {rechnung.lieferant_id} not found")
        supplier_name = supplier["name"]
        data["lieferant_name"] = supplier_name
        existing = client.find_vendor_bill(rechnung.lieferant_id, rechnung.rechnungsnummer)
        if existing:
            note = f" Diese Rechnung existiert bereits (id {existing}); es werden nur fehlende Anhänge ergänzt."
    else:
        supplier_name = rechnung.lieferant.name
        if client.find_supplier(supplier_name, rechnung.lieferant.ust_id) is None:
            note = " Der Lieferant wird neu angelegt."
    data["anhaenge"], blobs = _split_attachments(rechnung.anhaenge)

    resource_id = f"{supplier_name} / {rechnung.rechnungsnummer}"
    request = approvals.create(
        _resolve_tenant_id(ctx),
        action="odoo_eingangsrechnung_anlegen",
        resource_id=resource_id,
        data=data,
        blobs=blobs,
        ttl=ODOO_APPROVAL_TTL,
    )
    return _pending_response(request, f"Eingangsrechnung {resource_id} als Entwurf in Odoo ablegen?{note}")


@mcp.tool()
def odoo_anhang_ablegen(ctx: Context, ablage: AnhangAblageRequest) -> PendingApprovalDTO:
    """Request human approval to store file(s) on an existing Odoo record: an
    invoice, order, partner or product. Writes nothing yet. Once approved, call
    odoo_anhang_ablegen_execute. Use odoo_eingangsrechnung_anlegen for a new
    supplier invoice instead."""
    rows = _odoo().read(ablage.res_model, [ablage.res_id], ["display_name"])
    if not rows:
        raise OdooError(404, f"{ablage.res_model} {ablage.res_id} not found")
    data = ablage.model_dump(mode="json", exclude={"anhaenge"})
    data["datensatz"] = rows[0]["display_name"]
    data["anhaenge"], blobs = _split_attachments(ablage.anhaenge)
    request = approvals.create(
        _resolve_tenant_id(ctx),
        action="odoo_anhang_ablegen",
        resource_id=rows[0]["display_name"],
        data=data,
        blobs=blobs,
        ttl=ODOO_APPROVAL_TTL,
    )
    names = ", ".join(item["dateiname"] for item in data["anhaenge"])
    return _pending_response(request, f"{names} an {rows[0]['display_name']} in Odoo ablegen?")


@mcp.tool()
def odoo_material_anlegen(ctx: Context, material: MaterialRequest) -> PendingApprovalDTO:
    """Request human approval to create a new purchasable article in Odoo,
    optionally with a supplier and purchase price. Writes nothing yet. Fails if
    the internal reference is already taken. Once approved, call
    odoo_material_anlegen_execute."""
    client = _odoo()
    if client.find_product_template(material.artikel_referenz):
        raise OdooError(409, f"article {material.artikel_referenz!r} already exists")
    client.resolve_by_name("product.category", "name", material.kategorie)
    client.resolve_by_name("uom.uom", "name", material.einheit)
    data = material.model_dump(mode="json", exclude_none=True)
    if material.lieferant_id is not None:
        supplier = client.get_one("res.partner", material.lieferant_id, ["name"])
        if supplier is None:
            raise OdooError(404, f"supplier {material.lieferant_id} not found")
        data["lieferant_name"] = supplier["name"]
    resource_id = f"{material.artikel_referenz} {material.name}"
    request = approvals.create(
        _resolve_tenant_id(ctx),
        action="odoo_material_anlegen",
        resource_id=resource_id,
        data=data,
        ttl=ODOO_APPROVAL_TTL,
    )
    return _pending_response(request, f"Neuen Artikel {resource_id} in Odoo anlegen?")


@mcp.tool()
def odoo_einkaufspreis_aendern(ctx: Context, aenderung: EinkaufspreisRequest) -> PendingApprovalDTO:
    """Request human approval to change a stored supplier purchase price. Writes
    nothing yet. Get lieferantenpreis_id from odoo_preise_pruefen. Once approved,
    call odoo_einkaufspreis_aendern_execute."""
    row = _odoo().get_one(
        "product.supplierinfo", aenderung.lieferantenpreis_id, ["partner_id", "product_tmpl_id", "price"]
    )
    if row is None:
        raise OdooError(404, f"supplier price {aenderung.lieferantenpreis_id} not found")
    supplier, article = _m2o_name(row.get("partner_id")), _m2o_name(row.get("product_tmpl_id"))
    current = row.get("price") or 0.0
    data = {
        "lieferantenpreis_id": aenderung.lieferantenpreis_id,
        "lieferant": supplier,
        "artikel": article,
        "alter_preis": float(current),
        "neuer_preis": aenderung.neuer_preis,
    }
    resource_id = f"{article} / {supplier}"
    request = approvals.create(
        _resolve_tenant_id(ctx),
        action="odoo_einkaufspreis_aendern",
        resource_id=resource_id,
        data=data,
        ttl=ODOO_APPROVAL_TTL,
    )
    return _pending_response(request, f"Einkaufspreis von {resource_id}: {current} → {aenderung.neuer_preis} ändern?")


@mcp.tool()
def odoo_verkaufspreis_aendern(ctx: Context, aenderung: VerkaufspreisRequest) -> PendingApprovalDTO:
    """Request human approval to change an article's net sales price (list price)
    in Odoo. Writes nothing yet. Existing quotes and orders keep their prices. Once
    approved, call odoo_verkaufspreis_aendern_execute."""
    article = _odoo().find_product_template(aenderung.artikel_referenz)
    if article is None:
        raise OdooError(404, f"article {aenderung.artikel_referenz!r} not found")
    current = float(article.get("list_price") or 0.0)
    data = {
        "artikel_referenz": aenderung.artikel_referenz,
        "artikel": article["name"],
        "alter_preis": current,
        "neuer_preis": aenderung.neuer_preis,
    }
    resource_id = f"{aenderung.artikel_referenz} {article['name']}"
    request = approvals.create(
        _resolve_tenant_id(ctx),
        action="odoo_verkaufspreis_aendern",
        resource_id=resource_id,
        data=data,
        ttl=ODOO_APPROVAL_TTL,
    )
    return _pending_response(request, f"Verkaufspreis von {resource_id}: {current} → {aenderung.neuer_preis} ändern?")


# --- writes: execute (each consumes its approval first, so it can never run twice) ---


@mcp.tool()
def odoo_eingangsrechnung_anlegen_execute(ctx: Context, approval_id: str) -> dict:
    """Store the supplier invoice of an approved odoo_eingangsrechnung_anlegen
    request as a draft and attach its files. Fails unless a human has approved it;
    each approval works once. If a bill with this number already exists for the
    supplier, only missing attachments are added."""
    try:
        approval = approvals.consume_approved(approval_id, _resolve_tenant_id(ctx), "odoo_eingangsrechnung_anlegen")
    except (approvals.ApprovalNotFoundError, approvals.ApprovalStateError) as exc:
        return _approval_error(exc, approval_id)

    bill_id = None
    try:
        reviewed = dict(approval.data)
        meta = reviewed.pop("anhaenge")
        reviewed.pop("lieferant_name", None)
        rechnung = EingangsrechnungDaten.model_validate(reviewed)
        client = _odoo()

        partner_id = rechnung.lieferant_id
        supplier_created = False
        if partner_id is None:
            partner_id = client.find_supplier(rechnung.lieferant.name, rechnung.lieferant.ust_id)
            if partner_id is None:
                partner_id = client.create_supplier(**rechnung.lieferant.model_dump())
                supplier_created = True

        bill_id = client.find_vendor_bill(partner_id, rechnung.rechnungsnummer)
        bill_created = bill_id is None
        if bill_created:
            bill_id = client.create_draft_vendor_bill(
                partner_id=partner_id,
                rechnungsnummer=rechnung.rechnungsnummer,
                rechnungsdatum=rechnung.rechnungsdatum.isoformat(),
                faelligkeitsdatum=rechnung.faelligkeitsdatum.isoformat() if rechnung.faelligkeitsdatum else None,
                waehrung=rechnung.waehrung,
                lines=[_bill_line(client, position) for position in rechnung.positionen],
            )

        stored = _attach_all(client, approval, meta, "account.move", bill_id)
        result = {
            "rechnung_id": bill_id,
            "lieferant_id": partner_id,
            "lieferant_angelegt": supplier_created,
            "rechnung_angelegt": bill_created,
            "anhaenge": stored,
        }
        new_attachments = [item["attachment_id"] for item in stored if item["added"]]
        if bill_created and new_attachments:
            # Shows the PDF next to the bill form. Never worth failing the store for.
            try:
                client.set_main_attachment(bill_id, new_attachments[0])
            except OdooError as exc:
                result["hinweis"] = f"Hauptanhang nicht gesetzt: {exc}"
        approvals.discard_blobs(approval)
        return result
    except OdooError as exc:
        error = _error(exc)
        if bill_id is not None:
            error["rechnung_id"] = bill_id
            error["hinweis"] = "Die Rechnung existiert, aber ein Anhang fehlt evtl.; erneutes Anfragen ergänzt ihn."
        return error
    except (ValidationError, ValueError) as exc:
        return {"error": f"invalid data after approval: {exc}", "status_code": 422}


def _bill_line(client: OdooClient, position) -> dict:
    line: dict[str, Any] = {
        "name": position.beschreibung,
        "quantity": position.menge,
        "price_unit": position.einzelpreis,
    }
    if position.steuer_prozent is not None:
        tax = client.resolve_purchase_tax(position.steuer_prozent, position.steuer_name)
        line["tax_ids"] = [[6, 0, [tax["id"]]]]
    if position.artikel_referenz:
        rows = client.search_read(
            "product.product", [["default_code", "=", position.artikel_referenz]], ["id"], limit=1
        )
        if not rows:
            raise OdooError(404, f"article {position.artikel_referenz!r} not found")
        line["product_id"] = rows[0]["id"]
    return line


@mcp.tool()
def odoo_anhang_ablegen_execute(ctx: Context, approval_id: str) -> dict:
    """Store the files of an approved odoo_anhang_ablegen request. Fails unless a
    human has approved it; each approval works once. A file of the same name
    already on the record is skipped."""
    try:
        approval = approvals.consume_approved(approval_id, _resolve_tenant_id(ctx), "odoo_anhang_ablegen")
    except (approvals.ApprovalNotFoundError, approvals.ApprovalStateError) as exc:
        return _approval_error(exc, approval_id)
    try:
        ablage = AnhangAblageDaten.model_validate(approval.data)
        stored = _attach_all(_odoo(), approval, approval.data["anhaenge"], ablage.res_model, ablage.res_id)
    except OdooError as exc:
        return _error(exc)
    except (ValidationError, ValueError) as exc:
        return {"error": f"invalid data after approval: {exc}", "status_code": 422}
    approvals.discard_blobs(approval)
    return {"res_model": ablage.res_model, "res_id": ablage.res_id, "anhaenge": stored}


@mcp.tool()
def odoo_material_anlegen_execute(ctx: Context, approval_id: str) -> dict:
    """Create the article of an approved odoo_material_anlegen request. Fails
    unless a human has approved it; each approval works once."""
    try:
        approval = approvals.consume_approved(approval_id, _resolve_tenant_id(ctx), "odoo_material_anlegen")
    except (approvals.ApprovalNotFoundError, approvals.ApprovalStateError) as exc:
        return _approval_error(exc, approval_id)
    try:
        material = MaterialRequest.model_validate(approval.data)
    except ValidationError as exc:
        return _invalid(exc)
    client = _odoo()
    try:
        existing = client.find_product_template(material.artikel_referenz)
        if existing:
            tmpl_id, created = existing["id"], False
        else:
            tmpl_id = client.create_product(
                default_code=material.artikel_referenz,
                name=material.name,
                categ_id=client.resolve_by_name("product.category", "name", material.kategorie),
                uom_id=client.resolve_by_name("uom.uom", "name", material.einheit),
            )
            created = True
        result = {"artikel_id": tmpl_id, "angelegt": created}
        if created and material.lieferant_id is not None:
            result["lieferantenpreis_id"] = client.create_supplier_price(
                tmpl_id=tmpl_id,
                partner_id=material.lieferant_id,
                price=material.einkaufspreis or 0.0,
                product_code=material.lieferanten_artikelnummer,
            )
        return result
    except OdooError as exc:
        return _error(exc)


@mcp.tool()
def odoo_einkaufspreis_aendern_execute(ctx: Context, approval_id: str) -> dict:
    """Apply an approved odoo_einkaufspreis_aendern request. Fails unless a human
    has approved it; each approval works once. Refuses if the stored price changed
    after the request was made, since the human approved a change from the old
    value."""
    try:
        approval = approvals.consume_approved(approval_id, _resolve_tenant_id(ctx), "odoo_einkaufspreis_aendern")
    except (approvals.ApprovalNotFoundError, approvals.ApprovalStateError) as exc:
        return _approval_error(exc, approval_id)
    try:
        aenderung = EinkaufspreisRequest.model_validate(approval.data)
    except ValidationError as exc:
        return _invalid(exc)
    client = _odoo()
    try:
        row = client.get_one("product.supplierinfo", aenderung.lieferantenpreis_id, ["price"])
        if row is None:
            raise OdooError(404, f"supplier price {aenderung.lieferantenpreis_id} not found")
        expected = float(approval.data["alter_preis"])
        if abs((row.get("price") or 0.0) - expected) > 1e-6:
            raise OdooError(409, f"price is now {row.get('price')}, approval was for a change from {expected}")
        client.set_supplier_price(aenderung.lieferantenpreis_id, aenderung.neuer_preis)
    except OdooError as exc:
        return _error(exc)
    return {
        "lieferantenpreis_id": aenderung.lieferantenpreis_id,
        "alter_preis": expected,
        "neuer_preis": aenderung.neuer_preis,
    }


@mcp.tool()
def odoo_verkaufspreis_aendern_execute(ctx: Context, approval_id: str) -> dict:
    """Apply an approved odoo_verkaufspreis_aendern request. Fails unless a human
    has approved it; each approval works once. Refuses if the stored price changed
    after the request was made, since the human approved a change from the old
    value."""
    try:
        approval = approvals.consume_approved(approval_id, _resolve_tenant_id(ctx), "odoo_verkaufspreis_aendern")
    except (approvals.ApprovalNotFoundError, approvals.ApprovalStateError) as exc:
        return _approval_error(exc, approval_id)
    try:
        aenderung = VerkaufspreisRequest.model_validate(approval.data)
    except ValidationError as exc:
        return _invalid(exc)
    client = _odoo()
    try:
        article = client.find_product_template(aenderung.artikel_referenz)
        if article is None:
            raise OdooError(404, f"article {aenderung.artikel_referenz!r} not found")
        expected = float(approval.data["alter_preis"])
        if abs((article.get("list_price") or 0.0) - expected) > 1e-6:
            raise OdooError(409, f"price is now {article.get('list_price')}, approval was for a change from {expected}")
        client.set_sales_price(article["id"], aenderung.neuer_preis)
    except OdooError as exc:
        return _error(exc)
    return {"artikel_id": article["id"], "alter_preis": expected, "neuer_preis": aenderung.neuer_preis}

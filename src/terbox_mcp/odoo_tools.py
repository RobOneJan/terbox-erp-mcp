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

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from mcp.server.mcpserver import Context
from pydantic import BaseModel, Field, ValidationError

from terbox_mcp import approvals
from terbox_mcp.einvoice import read_einvoice
from terbox_mcp.models import ApprovalStatusDTO, PendingApprovalDTO
from terbox_mcp.odoo_client import DEFAULT_LIMIT, SUPPLIER_CONTACT_FIELDS, OdooClient, OdooError
from terbox_mcp.odoo_models import (
    Anhang,
    AnhangAblageDaten,
    AnhangAblageRequest,
    AttachmentDTO,
    Lieferant,
    LieferantAenderung,
    EinkaufspreisRequest,
    MaterialRequest,
    PreisPruefPosition,
    RechnungAblageRequest,
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


# --- supplier contact details ------------------------------------------------

_LABELS = {"email": "E-Mail", "telefon": "Telefon", "ust_id": "USt-IdNr", "strasse": "Straße", "plz": "PLZ", "ort": "Ort"}
_ADDRESS = ("strasse", "plz", "ort")


def _normalized(field: str, value: str | None) -> str:
    """Two spellings of the same detail must not count as a change."""
    value = (value or "").strip()
    if field == "email":
        return value.lower()
    if field == "telefon":
        digits = re.sub(r"[^\d+]", "", value)
        return re.sub(r"^(\+|00)49", "0", digits)
    if field == "ust_id":
        return re.sub(r"\s", "", value).upper()
    value = value.casefold()
    if field == "strasse":
        # "Hansestr. 36", "Hansestraße 36" and "Hansestrasse 36" are one address.
        value = re.sub(r"(straße|strasse|str\.?)", "str", value)
        return re.sub(r"[\s.,-]", "", value)
    return " ".join(value.split())


def _normalized_iban(value: str) -> str:
    return re.sub(r"\s", "", value).upper()


def _contact_changes(current: dict[str, str], printed: dict[str, str]) -> tuple[dict[str, str], dict[str, dict]]:
    """Split the printed details into ones to fill in (the field is empty in Odoo)
    and ones that differ from what Odoo has. The address is one unit: it is only
    filled in when Odoo has none at all, so street and city never come from two
    different places."""
    fill: dict[str, str] = {}
    differ: dict[str, dict] = {}
    address_empty = not any(current.get(field) for field in _ADDRESS)
    for field, value in printed.items():
        if not value or not value.strip():
            continue
        old = current.get(field) or ""
        if _normalized(field, old) == _normalized(field, value):
            continue
        if field in _ADDRESS:
            if address_empty:
                fill[field] = value.strip()
            else:
                differ[field] = {"bisher": old, "neu": value.strip()}
        elif not old:
            fill[field] = value.strip()
        else:
            differ[field] = {"bisher": old, "neu": value.strip()}
    return fill, differ


def _labels(fields) -> str:
    return ", ".join(_LABELS[field] for field in fields)


# --- supplier invoices: stored right away, a new supplier needs approval ------


def _euro(amount: float) -> str:
    return f"{amount:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


@dataclass
class _Rechnungsdaten:
    """What goes into the bill: the e-invoice's data where the file has it, the
    agent's reading otherwise."""

    rechnungsnummer: str
    rechnungsdatum: date
    faelligkeitsdatum: date | None
    waehrung: str
    lieferant_name: str | None
    ust_id: str | None
    kontakt: dict[str, str | None]
    iban: str | None
    # (net, percent, exact Odoo tax name or None, whether Odoo's tax can be chosen)
    summen: list[tuple[float, float, str | None, bool]]
    positionen: list[dict]
    quelle: str
    hinweise: list[str]


def _rechnungsdaten(rechnung: RechnungAblageRequest) -> _Rechnungsdaten:
    agent_kontakt = rechnung.kontakt.model_dump() if rechnung.kontakt else {}
    agent_summen = [(b.netto, b.steuer_prozent, b.steuer_name, True) for b in rechnung.betraege]
    einvoice = next(
        (found for found in (read_einvoice(anhang.raw()) for anhang in rechnung.anhaenge) if found), None
    )
    if einvoice is None or not einvoice.rechnungsnummer:
        return _Rechnungsdaten(
            rechnungsnummer=rechnung.rechnungsnummer,
            rechnungsdatum=rechnung.rechnungsdatum,
            faelligkeitsdatum=rechnung.faelligkeitsdatum,
            waehrung=rechnung.waehrung,
            lieferant_name=rechnung.lieferant_name,
            ust_id=rechnung.ust_id,
            kontakt=agent_kontakt,
            iban=rechnung.iban,
            summen=agent_summen,
            positionen=[],
            quelle="dokument",
            hinweise=[],
        )

    hinweise = []
    if einvoice.rechnungsnummer != rechnung.rechnungsnummer:
        hinweise.append(f"Rechnungsnummer laut E-Rechnung {einvoice.rechnungsnummer} (gelesen: {rechnung.rechnungsnummer})")
    summen = []
    for steuer in einvoice.steuern:
        # Only the standard rate maps to one tax on its own; reverse charge or
        # exemptions need a choice the XML doesn't make.
        summen.append((steuer.netto, steuer.prozent, None, steuer.kategorie == "S"))
        if steuer.kategorie != "S":
            hinweise.append(f"Steuerart {steuer.kategorie or '?'} - Steuer bitte in Odoo prüfen")
    kontakt = {
        "email": einvoice.email or agent_kontakt.get("email"),
        "telefon": einvoice.telefon or agent_kontakt.get("telefon"),
        "strasse": einvoice.strasse or agent_kontakt.get("strasse"),
        "plz": einvoice.plz or agent_kontakt.get("plz"),
        "ort": einvoice.ort or agent_kontakt.get("ort"),
    }
    return _Rechnungsdaten(
        rechnungsnummer=einvoice.rechnungsnummer,
        rechnungsdatum=einvoice.rechnungsdatum or rechnung.rechnungsdatum,
        faelligkeitsdatum=einvoice.faelligkeitsdatum or rechnung.faelligkeitsdatum,
        waehrung=(einvoice.waehrung or rechnung.waehrung).upper(),
        # Some generators put the address into the name field; the VAT id finds
        # the supplier anyway, so the agent's reading of the name comes first.
        lieferant_name=rechnung.lieferant_name or einvoice.lieferant_name,
        ust_id=einvoice.ust_id or rechnung.ust_id,
        kontakt=kontakt,
        iban=einvoice.iban or rechnung.iban,
        summen=summen or agent_summen,
        positionen=[
            {
                "beschreibung": p.beschreibung[:200],
                "lieferanten_artikelnummer": p.lieferanten_artikelnummer,
                "menge": p.menge,
                "einheit": p.einheit,
                "einzelpreis": p.einzelpreis,
            }
            for p in einvoice.positionen
        ],
        quelle=f"e-rechnung ({einvoice.format})",
        hinweise=hinweise,
    )


class RechnungAblageDTO(BaseModel):
    """Result of odoo_rechnung_ablegen. One model for every outcome, because a
    Union return nests structured_content under "result" and agent-hub would no
    longer see a pending approval. When a card is waiting, id / status="pending"
    / message / payload are what agent-hub's _looks_like_pending_approval reads;
    `ergebnis` always says what actually happened."""

    status: str
    ergebnis: str
    meldung: str | None = None
    id: str | None = None
    message: str | None = None
    payload: dict[str, str] = Field(default_factory=dict)
    rechnung_id: int | None = None
    lieferant_id: int | None = None
    quelle: str | None = None
    anhaenge: list[dict] = Field(default_factory=list)
    positionen: list[dict] = Field(default_factory=list)
    abweichende_kontaktdaten: dict = Field(default_factory=dict)
    kandidaten: list[dict] = Field(default_factory=list)
    vorhandene_rechnungen: list[dict] = Field(default_factory=list)
    naechster_schritt: str | None = None
    hinweis: str | None = None
    error: str | None = None
    status_code: int | None = None


def _done(ergebnis: str, **fields) -> RechnungAblageDTO:
    return RechnungAblageDTO(status=ergebnis, ergebnis=ergebnis, **fields)


def _failed(exc: OdooError) -> RechnungAblageDTO:
    return RechnungAblageDTO(status="error", ergebnis="error", error=str(exc), status_code=exc.status_code)


def _bill_lines(client: OdooClient, rechnungsnummer: str, betraege: list[dict], cost_type: dict | None) -> list[dict]:
    """One total line per tax rate. `betraege`: {"netto", "steuer"} where steuer
    is an exact Odoo purchase tax name, or "" for none (a tax the XML doesn't
    settle on its own, left for the human to choose)."""
    lines = []
    for betrag in betraege:
        tax = client.resolve_purchase_tax_by_name(betrag["steuer"]) if betrag.get("steuer") else None
        label = f"Rechnung {rechnungsnummer} – Gesamtbetrag {tax['name'] if tax else 'ohne Steuer'}"
        line = {"name": label + (f" – {cost_type['name']}" if cost_type else ""), "quantity": 1, "price_unit": float(betrag["netto"])}
        if tax:
            line["tax_ids"] = [[6, 0, [tax["id"]]]]
        if cost_type:
            line["analytic_distribution"] = {str(cost_type["id"]): 100}
        lines.append(line)
    return lines


def _betraege(client: OdooClient, daten: _Rechnungsdaten) -> list[dict]:
    """The totals with their tax resolved to a name now, so the card shows the
    concrete tax and a human can correct it there."""
    betraege = []
    for netto, prozent, steuer_name, waehlbar in daten.summen:
        steuer = client.resolve_purchase_tax(prozent, steuer_name)["name"] if waehlbar else ""
        betraege.append({"netto": netto, "steuer": steuer})
    return betraege


def _store_bill(
    client: OdooClient,
    *,
    partner_id: int,
    rechnungsnummer: str,
    rechnungsdatum: str,
    faelligkeitsdatum: str | None,
    waehrung: str,
    betraege: list[dict],
    cost_type: dict | None,
    files: list[tuple[str, bytes]],
) -> tuple[int, list[dict], list[str]]:
    bill_id = client.create_draft_vendor_bill(
        partner_id=partner_id,
        rechnungsnummer=rechnungsnummer,
        rechnungsdatum=rechnungsdatum,
        faelligkeitsdatum=faelligkeitsdatum,
        waehrung=waehrung,
        lines=_bill_lines(client, rechnungsnummer, betraege, cost_type),
    )
    stored, hinweise = [], []
    for filename, blob in files:
        content_type = sniff_content_type(blob) or "application/octet-stream"
        stored.append({"dateiname": filename, **client.attach("account.move", bill_id, filename, blob, content_type)})
    new_attachments = [item["attachment_id"] for item in stored if item["added"]]
    if new_attachments:
        # Shows the PDF next to the bill form. Never worth failing the store for.
        try:
            client.set_main_attachment(bill_id, new_attachments[0])
        except OdooError as exc:
            hinweise.append(f"Hauptanhang nicht gesetzt: {exc}")
    return bill_id, stored, hinweise


@mcp.tool()
def odoo_rechnung_ablegen(ctx: Context, rechnung: RechnungAblageRequest) -> RechnungAblageDTO:
    """Store a supplier invoice in Odoo as a DRAFT bill with its file and one
    total line per tax rate (never the individual items), and ask a human with
    ONE approval card to book it.

    Always pass betraege (net totals per tax rate) - a bill is never stored
    without its amounts. For an e-invoice (ZUGFeRD / Factur-X / XRechnung) the
    data in the file wins over what you read, and its items come back in
    positionen for odoo_preise_pruefen. Pass kategorie (a cost type for internal
    reporting) when the kind of expense is clear; pick the predominant one for a
    mixed invoice, leave it out when unsure. Pass the supplier's contact details in
    kontakt and the IBAN in iban when shown: missing ones are added to the
    supplier, differing ones come back as abweichende_kontaktdaten for
    odoo_lieferant_aktualisieren, a differing IBAN only raises a warning.

    Checks first whether this invoice number is already in Odoo, for any
    supplier. ergebnis: "abgelegt" (draft stored, card asks to book it -
    approving calls odoo_rechnung_buchen_execute); "neuer_lieferant" (supplier
    not in Odoo: nothing stored yet, ONE card asks to create the supplier and
    book the invoice - approving calls odoo_rechnung_mit_lieferant_buchen_execute);
    "bereits_vorhanden" / "bereits_abgelegt" (already in Odoo, the meldung says
    under which supplier); "nummer_bei_anderem_lieferanten" (ask the user);
    "lieferant_unklar" (call again with the right lieferant_id from kandidaten).
    Reply with just the returned meldung.
    """
    daten = _rechnungsdaten(rechnung)
    client = _odoo()
    tenant_id = _resolve_tenant_id(ctx)
    try:
        vorhandene = client.find_bills_by_ref(daten.rechnungsnummer)
        if rechnung.lieferant_id is not None:
            supplier = client.get_supplier(rechnung.lieferant_id)
            if supplier is None:
                raise OdooError(404, f"partner {rechnung.lieferant_id} is not a supplier")
            partner_id, candidates = rechnung.lieferant_id, []
        else:
            partner_id, candidates = client.match_supplier(daten.lieferant_name, daten.ust_id)
            supplier = client.get_supplier(partner_id) if partner_id is not None else None
            if partner_id is not None and supplier is None:
                raise OdooError(409, f"partner {partner_id} matches the invoice but is not marked as a supplier")

        same = [bill for bill in vorhandene if partner_id is not None and bill["partner_id"] == partner_id]
        if vorhandene and not same:
            names = ", ".join(sorted({bill["partner_name"] for bill in vorhandene}))
            if partner_id is not None:
                return _done(
                    "nummer_bei_anderem_lieferanten",
                    meldung=(
                        f"Rechnungsnummer {daten.rechnungsnummer} gibt es in Odoo schon bei {names}, "
                        f"die Rechnung kommt aber von {supplier['name']}. Bitte prüfen."
                    ),
                    vorhandene_rechnungen=vorhandene,
                )
            return _done(
                "bereits_abgelegt",
                meldung=f"Rechnung {daten.rechnungsnummer} ist schon in Odoo abgelegt, beim Lieferanten {names}.",
                vorhandene_rechnungen=vorhandene,
            )
        if partner_id is None and candidates:
            return _done(
                "lieferant_unklar", kandidaten=[{"lieferant_id": row["id"], "name": row["name"]} for row in candidates]
            )
        if same:
            bill_id = same[0]["id"]
            for anhang in rechnung.anhaenge:
                _attach_one(client, anhang, bill_id)
            return _done(
                "bereits_vorhanden",
                meldung=f"Rechnung {daten.rechnungsnummer} von {supplier['name']} war schon in Odoo.",
                rechnung_id=bill_id,
                lieferant_id=partner_id,
            )
        if not daten.summen:
            return RechnungAblageDTO(
                status="error",
                ergebnis="error",
                error="betraege missing: read the net totals per tax rate from the invoice and call again",
                status_code=422,
            )

        cost_type = client.find_cost_type(rechnung.kategorie) if rechnung.kategorie else None
        hinweise = list(daten.hinweise)
        if rechnung.kategorie and cost_type is None:
            hinweise.append(f"Kostenart {rechnung.kategorie!r} gibt es nicht; vorhanden: {', '.join(client.cost_type_names())}")
        betraege = _betraege(client, daten)
        files = [(anhang.dateiname, anhang.raw()) for anhang in rechnung.anhaenge]
        gesamt = _euro(sum(b["netto"] for b in betraege))

        if partner_id is None:
            # Nothing is stored before the human approves: without a supplier
            # there is nothing to file the bill under.
            lieferant = {
                "name": daten.lieferant_name or daten.ust_id,
                "ust_id": daten.ust_id,
                **{key: value for key, value in daten.kontakt.items() if value},
            }
            data = {
                "lieferant": {key: value for key, value in lieferant.items() if value},
                **_card_fields(daten, cost_type),
                "waehrung": daten.waehrung,
                "betraege": betraege,
                "dateinamen": [name for name, _ in files],
            }
            request = approvals.create(
                tenant_id,
                action="odoo_rechnung_mit_lieferant_buchen",
                resource_id=lieferant["name"],
                data=data,
                blobs=[blob for _, blob in files],
                ttl=ODOO_APPROVAL_TTL,
            )
            return RechnungAblageDTO(
                status="pending",
                ergebnis="neuer_lieferant",
                id=request.id,
                message=(
                    f"Neuer Lieferant {lieferant['name']}: anlegen und Rechnung {daten.rechnungsnummer} "
                    f"über {gesamt} netto buchen?"
                ),
                payload=request.payload,
                meldung=(
                    f"Rechnung {daten.rechnungsnummer} von {lieferant['name']}: Lieferant ist neu, "
                    "Freigabe zum Anlegen und Buchen angefragt."
                ),
                quelle=daten.quelle,
                positionen=daten.positionen,
                hinweis="; ".join(hinweise) or None,
            )

        bill_id, stored, more = _store_bill(
            client,
            partner_id=partner_id,
            rechnungsnummer=daten.rechnungsnummer,
            rechnungsdatum=daten.rechnungsdatum.isoformat(),
            faelligkeitsdatum=daten.faelligkeitsdatum.isoformat() if daten.faelligkeitsdatum else None,
            waehrung=daten.waehrung,
            betraege=betraege,
            cost_type=cost_type,
            files=files,
        )
        hinweise += more
        total = client.bill_total(bill_id)
    except OdooError as exc:
        return _failed(exc)

    filled, differ, iban_warning, more = _enrich_contact(client, partner_id, daten)
    hinweise += more
    amount = _euro(total) if total else gesamt
    meldung = f"Rechnung {daten.rechnungsnummer} von {supplier['name']} ({amount}) als Entwurf abgelegt, Buchung angefragt."
    if filled:
        meldung += f" Beim Lieferanten ergänzt: {_labels(filled)}."
    if iban_warning:
        meldung += (
            " Achtung: Die Rechnung nennt eine andere IBAN als in Odoo hinterlegt. Bitte telefonisch "
            "beim Lieferanten prüfen, bevor bezahlt wird."
        )
    # The bill's id goes in resource_id, which the card cannot edit - so a card
    # can correct the number or a date but never point at another bill.
    request = approvals.create(
        tenant_id,
        action="odoo_rechnung_buchen",
        resource_id=str(bill_id),
        data=_card_fields(daten, cost_type),
        ttl=ODOO_APPROVAL_TTL,
    )
    return RechnungAblageDTO(
        status="pending",
        ergebnis="abgelegt",
        id=request.id,
        message=f"Rechnung {daten.rechnungsnummer} von {supplier['name']} über {amount} buchen?",
        payload=request.payload,
        meldung=meldung,
        rechnung_id=bill_id,
        lieferant_id=partner_id,
        quelle=daten.quelle,
        anhaenge=stored,
        positionen=daten.positionen,
        abweichende_kontaktdaten=differ,
        naechster_schritt=(
            "call odoo_lieferant_aktualisieren with this lieferant_id and the new values, "
            "so a human can approve the change"
        )
        if differ
        else None,
        hinweis="; ".join(hinweise) or None,
    )


def _card_fields(daten: _Rechnungsdaten, cost_type: dict | None) -> dict:
    """What a human may correct on the card before booking."""
    return {
        "rechnungsnummer": daten.rechnungsnummer,
        "rechnungsdatum": daten.rechnungsdatum.isoformat(),
        "faelligkeitsdatum": daten.faelligkeitsdatum.isoformat() if daten.faelligkeitsdatum else "",
        "kostenart": cost_type["name"] if cost_type else "",
    }


def _enrich_contact(client: OdooClient, partner_id: int, daten: _Rechnungsdaten) -> tuple[dict, dict, bool, list[str]]:
    """Only after the bill is safe: a problem with the supplier's details must
    never cost the invoice."""
    printed = {**daten.kontakt, "ust_id": daten.ust_id}
    try:
        filled, differ = _contact_changes(client.supplier_contact(partner_id), printed)
        if filled:
            client.update_supplier_contact(partner_id, filled)
        iban_warning = False
        if daten.iban:
            known = client.supplier_ibans(partner_id)
            iban_warning = bool(known) and _normalized_iban(daten.iban) not in known
        return filled, differ, iban_warning, []
    except OdooError as exc:
        return {}, {}, False, [f"Kontaktdaten nicht ergänzt: {exc}"]


def _card_values(data: dict, client: OdooClient) -> tuple[str, str, str | None, dict | None]:
    """The card's (possibly corrected) fields, checked: a typo there fails the
    booking instead of booking something odd."""
    nummer = str(data.get("rechnungsnummer") or "").strip()
    if not nummer:
        raise OdooError(422, "Rechnungsnummer fehlt")
    try:
        datum = date.fromisoformat(str(data["rechnungsdatum"]).strip()).isoformat()
        faellig_raw = str(data.get("faelligkeitsdatum") or "").strip()
        faellig = date.fromisoformat(faellig_raw).isoformat() if faellig_raw else None
    except ValueError as exc:
        raise OdooError(422, f"Datum nicht lesbar (JJJJ-MM-TT): {exc}") from exc
    kostenart = str(data.get("kostenart") or "").strip()
    cost_type = client.find_cost_type(kostenart) if kostenart else None
    if kostenart and cost_type is None:
        raise OdooError(422, f"Kostenart {kostenart!r} gibt es nicht; vorhanden: {', '.join(client.cost_type_names())}")
    return nummer, datum, faellig, cost_type


def _attach_one(client: OdooClient, anhang: Anhang, bill_id: int) -> dict:
    blob = anhang.raw()
    content_type = sniff_content_type(blob) or "application/octet-stream"
    return {"dateiname": anhang.dateiname, **client.attach("account.move", bill_id, anhang.dateiname, blob, content_type)}


@mcp.tool()
def odoo_lieferant_anlegen(ctx: Context, lieferant: Lieferant) -> PendingApprovalDTO:
    """Request human approval to create a new supplier in Odoo, with the details
    printed on its invoice. Writes nothing yet. Fails if the supplier exists.
    Once approved, call odoo_lieferant_anlegen_execute, then odoo_rechnung_ablegen
    with the returned lieferant_id."""
    partner_id, candidates = _odoo().match_supplier(lieferant.name, lieferant.ust_id)
    if partner_id is not None:
        raise OdooError(409, f"supplier already exists (lieferant_id {partner_id})")
    if candidates:
        names = ", ".join(f"{row['name']} ({row['id']})" for row in candidates)
        raise OdooError(409, f"similar suppliers exist, check whether one is meant: {names}")
    request = approvals.create(
        _resolve_tenant_id(ctx),
        action="odoo_lieferant_anlegen",
        resource_id=lieferant.name,
        data=lieferant.model_dump(exclude_none=True),
        ttl=ODOO_APPROVAL_TTL,
    )
    return _pending_response(request, f"Neuer Lieferant: {lieferant.name} in Odoo anlegen?")


@mcp.tool()
def odoo_lieferant_anlegen_execute(ctx: Context, approval_id: str) -> dict:
    """Create the supplier of an approved odoo_lieferant_anlegen request. Fails
    unless a human has approved it; each approval works once."""
    try:
        approval = approvals.consume_approved(approval_id, _resolve_tenant_id(ctx), "odoo_lieferant_anlegen")
    except (approvals.ApprovalNotFoundError, approvals.ApprovalStateError) as exc:
        return _approval_error(exc, approval_id)
    try:
        lieferant = Lieferant.model_validate(approval.data)
    except ValidationError as exc:
        return _invalid(exc)
    client = _odoo()
    try:
        partner_id, _ = client.match_supplier(lieferant.name, lieferant.ust_id)
        created = partner_id is None
        if created:
            partner_id = client.create_supplier(**lieferant.model_dump())
    except OdooError as exc:
        return _error(exc)
    return {
        "lieferant_id": partner_id,
        "angelegt": created,
        "naechster_schritt": "call odoo_rechnung_ablegen with this lieferant_id",
    }


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
def odoo_anhang_ablegen(ctx: Context, ablage: AnhangAblageRequest) -> PendingApprovalDTO:
    """Request human approval to store file(s) on an existing Odoo record: an
    invoice, order, partner or product. Writes nothing yet. Once approved, call
    odoo_anhang_ablegen_execute. Use odoo_rechnung_ablegen for a new supplier
    invoice instead."""
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
        kosten = client.set_supplier_price(aenderung.lieferantenpreis_id, aenderung.neuer_preis)
    except OdooError as exc:
        return _error(exc)
    return {
        "lieferantenpreis_id": aenderung.lieferantenpreis_id,
        "alter_preis": expected,
        "neuer_preis": aenderung.neuer_preis,
        "kosten_aktualisiert": kosten,
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


@mcp.tool()
def odoo_lieferant_aktualisieren(ctx: Context, aenderung: LieferantAenderung) -> PendingApprovalDTO:
    """Request human approval to change a supplier's contact details (e-mail,
    phone, VAT id, address) where they differ from Odoo. Writes nothing yet.
    Bank details cannot be changed here. Once approved, call
    odoo_lieferant_aktualisieren_execute."""
    client = _odoo()
    supplier = client.get_supplier(aenderung.lieferant_id)
    if supplier is None:
        raise OdooError(404, f"partner {aenderung.lieferant_id} is not a supplier")
    current = client.supplier_contact(aenderung.lieferant_id)
    printed = aenderung.model_dump(exclude={"lieferant_id"})
    changes = {
        field: {"bisher": current.get(field) or "", "neu": value.strip()}
        for field, value in printed.items()
        if value and value.strip() and _normalized(field, current.get(field)) != _normalized(field, value)
    }
    if not changes:
        raise OdooError(409, "these details already match the supplier in Odoo")
    data = {"lieferant_id": aenderung.lieferant_id, "lieferant": supplier["name"], "aenderungen": changes}
    request = approvals.create(
        _resolve_tenant_id(ctx),
        action="odoo_lieferant_aktualisieren",
        resource_id=supplier["name"],
        data=data,
        ttl=ODOO_APPROVAL_TTL,
    )
    summary = "; ".join(
        f"{_LABELS[field]}: {change['bisher'] or '–'} → {change['neu']}" for field, change in changes.items()
    )
    return _pending_response(request, f"Kontaktdaten von {supplier['name']} ändern? {summary}")


@mcp.tool()
def odoo_lieferant_aktualisieren_execute(ctx: Context, approval_id: str) -> dict:
    """Apply an approved odoo_lieferant_aktualisieren request. Fails unless a
    human has approved it; each approval works once. Refuses if a detail changed
    in Odoo after the request was made. A value cleared on the card is skipped,
    never erased in Odoo."""
    try:
        approval = approvals.consume_approved(approval_id, _resolve_tenant_id(ctx), "odoo_lieferant_aktualisieren")
    except (approvals.ApprovalNotFoundError, approvals.ApprovalStateError) as exc:
        return _approval_error(exc, approval_id)
    partner_id = approval.data["lieferant_id"]
    changes = approval.data["aenderungen"]
    if set(changes) - set(SUPPLIER_CONTACT_FIELDS):
        return {"error": "invalid data after approval: unknown fields", "status_code": 422}
    client = _odoo()
    try:
        current = client.supplier_contact(partner_id)
        moved = [
            field for field, change in changes.items()
            if _normalized(field, current.get(field)) != _normalized(field, change.get("bisher"))
        ]
        if moved:
            raise OdooError(409, f"{_labels(moved)} changed in Odoo since the request; ask again")
        values = {field: change["neu"].strip() for field, change in changes.items() if (change.get("neu") or "").strip()}
        if values:
            client.update_supplier_contact(partner_id, values)
    except OdooError as exc:
        return _error(exc)
    return {"lieferant_id": partner_id, "geaendert": sorted(values)}


@mcp.tool()
def odoo_einkaufspreise_aendern(ctx: Context, aenderungen: list[EinkaufspreisRequest]) -> PendingApprovalDTO:
    """Request ONE human approval for several supplier price changes at once,
    e.g. all prices odoo_preise_pruefen found different or missing on one
    invoice. Writes nothing yet. Once approved, call
    odoo_einkaufspreise_aendern_execute."""
    if not 1 <= len(aenderungen) <= 30:
        raise OdooError(400, "give between 1 and 30 price changes")
    client = _odoo()
    preise = []
    for aenderung in aenderungen:
        row = client.get_one(
            "product.supplierinfo", aenderung.lieferantenpreis_id, ["partner_id", "product_tmpl_id", "price"]
        )
        if row is None:
            raise OdooError(404, f"supplier price {aenderung.lieferantenpreis_id} not found")
        preise.append(
            {
                "lieferantenpreis_id": aenderung.lieferantenpreis_id,
                "artikel": _m2o_name(row.get("product_tmpl_id")),
                "lieferant": _m2o_name(row.get("partner_id")),
                "alter_preis": float(row.get("price") or 0.0),
                "neuer_preis": aenderung.neuer_preis,
            }
        )
    suppliers = sorted({p["lieferant"] or "?" for p in preise})
    request = approvals.create(
        _resolve_tenant_id(ctx),
        action="odoo_einkaufspreise_aendern",
        resource_id=", ".join(suppliers),
        data={"preise": preise},
        ttl=ODOO_APPROVAL_TTL,
    )
    return _pending_response(
        request, f"Einkaufspreise von {len(preise)} Artikel(n) bei {', '.join(suppliers)} anpassen?"
    )


@mcp.tool()
def odoo_einkaufspreise_aendern_execute(ctx: Context, approval_id: str) -> dict:
    """Apply an approved odoo_einkaufspreise_aendern request. Fails unless a
    human has approved it; each approval works once. A price that changed in
    Odoo since the request, or whose new value was cleared on the card, is
    skipped and reported; the others are applied."""
    try:
        approval = approvals.consume_approved(approval_id, _resolve_tenant_id(ctx), "odoo_einkaufspreise_aendern")
    except (approvals.ApprovalNotFoundError, approvals.ApprovalStateError) as exc:
        return _approval_error(exc, approval_id)
    client = _odoo()
    geaendert, uebersprungen = [], []
    try:
        for preis in approval.data["preise"]:
            artikel = preis.get("artikel") or str(preis["lieferantenpreis_id"])
            try:
                neu = float(preis["neuer_preis"])
            except (TypeError, ValueError):
                uebersprungen.append({"artikel": artikel, "grund": "kein neuer Preis"})
                continue
            if neu < 0:
                uebersprungen.append({"artikel": artikel, "grund": "negativer Preis"})
                continue
            row = client.get_one("product.supplierinfo", int(preis["lieferantenpreis_id"]), ["price"])
            if row is None:
                uebersprungen.append({"artikel": artikel, "grund": "Lieferantenpreis nicht mehr vorhanden"})
                continue
            if abs((row.get("price") or 0.0) - float(preis["alter_preis"])) > 1e-6:
                uebersprungen.append({"artikel": artikel, "grund": f"Preis inzwischen {row.get('price')}"})
                continue
            kosten = client.set_supplier_price(int(preis["lieferantenpreis_id"]), neu)
            geaendert.append(
                {"artikel": artikel, "alter_preis": float(preis["alter_preis"]), "neuer_preis": neu, "kosten_aktualisiert": kosten}
            )
    except OdooError as exc:
        error = _error(exc)
        error["bereits_geaendert"] = geaendert
        return error
    return {"geaendert": geaendert, "uebersprungen": uebersprungen}


@mcp.tool()
def odoo_rechnung_buchen_execute(ctx: Context, approval_id: str) -> dict:
    """Book (post) the draft bill of an approved odoo_rechnung_ablegen card,
    after applying any correction made on the card. Fails unless a human has
    approved it; each approval works once. A bill no longer in draft is left
    alone."""
    try:
        approval = approvals.consume_approved(approval_id, _resolve_tenant_id(ctx), "odoo_rechnung_buchen")
    except (approvals.ApprovalNotFoundError, approvals.ApprovalStateError) as exc:
        return _approval_error(exc, approval_id)
    client = _odoo()
    bill_id = int(approval.resource_id)
    try:
        nummer, datum, faellig, cost_type = _card_values(approval.data, client)
        bill = client.get_one("account.move", bill_id, ["state", "ref", "invoice_date", "invoice_date_due"])
        if bill is None:
            raise OdooError(404, f"bill {bill_id} not found")
        if bill["state"] != "draft":
            raise OdooError(409, f"bill {bill_id} is {bill['state']}, not a draft any more")
        changes = {
            key: value
            for key, value in (("ref", nummer), ("invoice_date", datum), ("invoice_date_due", faellig))
            if value and value != bill.get(key)
        }
        client.update_draft_bill(bill_id, changes, cost_type["id"] if cost_type else None)
        name = client.post_bill(bill_id)
    except OdooError as exc:
        error = _error(exc)
        error["hinweis"] = "Die Rechnung bleibt als Entwurf in Odoo und kann dort gebucht werden."
        return error
    return {"rechnung_id": bill_id, "gebucht_als": name, "meldung": f"Rechnung {nummer} gebucht ({name})."}


@mcp.tool()
def odoo_rechnung_mit_lieferant_buchen_execute(ctx: Context, approval_id: str) -> dict:
    """Create the supplier, store and book the invoice of an approved
    odoo_rechnung_ablegen card for a new supplier. Fails unless a human has
    approved it; each approval works once. If the supplier or the invoice turns
    out to exist by now, they are reused, not created twice."""
    try:
        approval = approvals.consume_approved(
            approval_id, _resolve_tenant_id(ctx), "odoo_rechnung_mit_lieferant_buchen"
        )
    except (approvals.ApprovalNotFoundError, approvals.ApprovalStateError) as exc:
        return _approval_error(exc, approval_id)
    client = _odoo()
    bill_id = None
    try:
        lieferant = Lieferant.model_validate(approval.data["lieferant"])
        nummer, datum, faellig, cost_type = _card_values(approval.data, client)
        betraege = [
            {"netto": float(b["netto"]), "steuer": str(b.get("steuer") or "").strip()}
            for b in approval.data["betraege"]
        ]
        partner_id, _ = client.match_supplier(lieferant.name, lieferant.ust_id)
        supplier_created = partner_id is None
        if supplier_created:
            partner_id = client.create_supplier(**lieferant.model_dump())
        existing = [bill for bill in client.find_bills_by_ref(nummer) if bill["partner_id"] == partner_id]
        if existing:
            bill_id = existing[0]["id"]
        else:
            names = approval.data.get("dateinamen") or [f"rechnung-{i + 1}.pdf" for i in range(len(approval.blobs))]
            bill_id, _, _ = _store_bill(
                client,
                partner_id=partner_id,
                rechnungsnummer=nummer,
                rechnungsdatum=datum,
                faelligkeitsdatum=faellig,
                waehrung=str(approval.data.get("waehrung") or "EUR"),
                betraege=betraege,
                cost_type=cost_type,
                files=list(zip([safe_filename(n) for n in names], approval.blobs)),
            )
        name = client.post_bill(bill_id)
    except (ValidationError, ValueError, KeyError) as exc:
        return {"error": f"invalid data after approval: {exc}", "status_code": 422}
    except OdooError as exc:
        error = _error(exc)
        if bill_id is not None:
            error["rechnung_id"] = bill_id
            error["hinweis"] = "Die Rechnung liegt als Entwurf in Odoo und kann dort gebucht werden."
        return error
    approvals.discard_blobs(approval)
    return {
        "lieferant_id": partner_id,
        "lieferant_angelegt": supplier_created,
        "rechnung_id": bill_id,
        "gebucht_als": name,
        "meldung": f"Lieferant {lieferant.name} {'angelegt' if supplier_created else 'gefunden'}, Rechnung {nummer} gebucht ({name}).",
    }

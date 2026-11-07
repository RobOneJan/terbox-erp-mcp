"""Read the structured data of a German e-invoice (ZUGFeRD / Factur-X / XRechnung).

Such an invoice carries its data as XML, either embedded in the PDF or as the
file itself. Reading that is exact, where a model reading the printed page can
misread a digit. Only what storing a bill needs is taken: header, seller,
totals per tax rate, IBAN and the item lines (for comparing prices).

Both syntaxes German suppliers send are handled: UN/CEFACT CII (ZUGFeRD,
Factur-X, most XRechnungen) and OASIS UBL (some XRechnungen). Anything that is
not a readable e-invoice returns None, never an error - the caller then falls
back to what the agent read from the document.

The XML comes from outside, so it is parsed with the standard library parser,
which does not resolve external entities, after a size check.
"""

from __future__ import annotations

import io
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date

logger = logging.getLogger(__name__)

MAX_XML_BYTES = 5 * 1024 * 1024

# Names suppliers' software gives the embedded file.
_EMBEDDED_NAMES = ("factur-x.xml", "zugferd-invoice.xml", "xrechnung.xml", "zugferd.xml")

_CII = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
}
_UBL = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
}
_UBL_INVOICE = "{urn:oasis:names:specification:ubl:schema:xsd:Invoice-2}Invoice"
_UBL_CREDIT_NOTE = "{urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2}CreditNote"
_CII_ROOT = f"{{{_CII['rsm']}}}CrossIndustryInvoice"
# UNTDID 1001 codes for a credit note (381) and its variants.
_CREDIT_TYPE_CODES = {"381", "396", "532"}


@dataclass
class Steuersumme:
    netto: float
    prozent: float
    # UNTDID 5305: "S" standard rate. Others (AE reverse charge, E exempt, K
    # intra-EU, Z zero) need a tax choice no XML field settles on its own.
    kategorie: str


@dataclass
class Position:
    beschreibung: str
    lieferanten_artikelnummer: str | None
    menge: float | None
    einheit: str | None
    einzelpreis: float | None


@dataclass
class ERechnung:
    format: str
    gutschrift: bool
    rechnungsnummer: str | None = None
    rechnungsdatum: date | None = None
    faelligkeitsdatum: date | None = None
    waehrung: str | None = None
    lieferant_name: str | None = None
    ust_id: str | None = None
    email: str | None = None
    telefon: str | None = None
    strasse: str | None = None
    plz: str | None = None
    ort: str | None = None
    iban: str | None = None
    brutto: float | None = None
    steuern: list[Steuersumme] = field(default_factory=list)
    positionen: list[Position] = field(default_factory=list)


def read_einvoice(content: bytes) -> ERechnung | None:
    """The e-invoice in a PDF's embedded XML or in an XML file, or None."""
    try:
        xml = _embedded_xml(content) if content[:1024].find(b"%PDF-") >= 0 else content
        if not xml or len(xml) > MAX_XML_BYTES:
            return None
        root = ET.fromstring(xml)
        if root.tag == _CII_ROOT:
            return _read_cii(root)
        if root.tag in (_UBL_INVOICE, _UBL_CREDIT_NOTE):
            return _read_ubl(root)
    except Exception as exc:  # noqa: BLE001 - a broken file is "no e-invoice", never a failure
        logger.info("no readable e-invoice data: %s: %s", type(exc).__name__, exc)
    return None


def _embedded_xml(pdf: bytes) -> bytes | None:
    from pypdf import PdfReader

    attachments = PdfReader(io.BytesIO(pdf)).attachments
    by_name = {name.lower(): data for name, data in attachments.items()}
    for name in _EMBEDDED_NAMES:
        if by_name.get(name):
            return by_name[name][0]
    # Some generators use their own name; take the only XML file if there is one.
    xml_files = [data[0] for name, data in by_name.items() if name.endswith(".xml") and data]
    return xml_files[0] if len(xml_files) == 1 else None


def _text(node: ET.Element | None, path: str, ns: dict) -> str | None:
    if node is None:
        return None
    found = node.find(path, ns)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def _number(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _cii_date(value: str | None) -> date | None:
    # Format 102 is YYYYMMDD, the only one EN 16931 allows here.
    if not value or len(value) < 8 or not value[:8].isdigit():
        return None
    return date(int(value[:4]), int(value[4:6]), int(value[6:8]))


def _iso_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


def _read_cii(root: ET.Element) -> ERechnung:
    ns = _CII
    doc = root.find("rsm:ExchangedDocument", ns)
    trade = root.find("rsm:SupplyChainTradeTransaction", ns)
    seller = trade.find("ram:ApplicableHeaderTradeAgreement/ram:SellerTradeParty", ns) if trade is not None else None
    settlement = trade.find("ram:ApplicableHeaderTradeSettlement", ns) if trade is not None else None
    address = seller.find("ram:PostalTradeAddress", ns) if seller is not None else None
    vat = None
    if seller is not None:
        for registration in seller.findall("ram:SpecifiedTaxRegistration/ram:ID", ns):
            if registration.get("schemeID") == "VA" and registration.text:
                vat = registration.text.strip()
    email = _text(seller, "ram:DefinedTradeContact/ram:EmailURIUniversalCommunication/ram:URIID", ns) or _text(
        seller, "ram:URIUniversalCommunication/ram:URIID", ns
    )

    invoice = ERechnung(
        format="CII",
        gutschrift=_text(doc, "ram:TypeCode", ns) in _CREDIT_TYPE_CODES,
        rechnungsnummer=_text(doc, "ram:ID", ns),
        rechnungsdatum=_cii_date(_text(doc, "ram:IssueDateTime/udt:DateTimeString", ns)),
        faelligkeitsdatum=_cii_date(
            _text(settlement, "ram:SpecifiedTradePaymentTerms/ram:DueDateDateTime/udt:DateTimeString", ns)
        ),
        waehrung=_text(settlement, "ram:InvoiceCurrencyCode", ns),
        lieferant_name=_text(seller, "ram:Name", ns),
        ust_id=vat,
        email=email if email and "@" in email else None,
        telefon=_text(seller, "ram:DefinedTradeContact/ram:TelephoneUniversalCommunication/ram:CompleteNumber", ns),
        strasse=_text(address, "ram:LineOne", ns),
        plz=_text(address, "ram:PostcodeCode", ns),
        ort=_text(address, "ram:CityName", ns),
        iban=_text(settlement, "ram:SpecifiedTradeSettlementPaymentMeans/ram:PayeePartyCreditorFinancialAccount/ram:IBANID", ns),
        brutto=_number(_text(settlement, "ram:SpecifiedTradeSettlementHeaderMonetarySummation/ram:GrandTotalAmount", ns)),
    )
    if settlement is not None:
        for tax in settlement.findall("ram:ApplicableTradeTax", ns):
            netto = _number(_text(tax, "ram:BasisAmount", ns))
            if netto is not None:
                invoice.steuern.append(
                    Steuersumme(
                        netto=netto,
                        prozent=_number(_text(tax, "ram:RateApplicablePercent", ns)) or 0.0,
                        kategorie=_text(tax, "ram:CategoryCode", ns) or "",
                    )
                )
    if trade is not None:
        for line in trade.findall("ram:IncludedSupplyChainTradeLineItem", ns):
            price = _number(_text(line, "ram:SpecifiedLineTradeAgreement/ram:NetPriceProductTradePrice/ram:ChargeAmount", ns))
            per = _number(_text(line, "ram:SpecifiedLineTradeAgreement/ram:NetPriceProductTradePrice/ram:BasisQuantity", ns))
            quantity = line.find("ram:SpecifiedLineTradeDelivery/ram:BilledQuantity", ns)
            invoice.positionen.append(
                Position(
                    beschreibung=_text(line, "ram:SpecifiedTradeProduct/ram:Name", ns) or "",
                    lieferanten_artikelnummer=_text(line, "ram:SpecifiedTradeProduct/ram:SellerAssignedID", ns),
                    menge=_number(quantity.text) if quantity is not None else None,
                    einheit=quantity.get("unitCode") if quantity is not None else None,
                    einzelpreis=round(price / per, 6) if price is not None and per else price,
                )
            )
    return invoice


def _read_ubl(root: ET.Element) -> ERechnung:
    ns = _UBL
    credit = root.tag == _UBL_CREDIT_NOTE
    party = root.find("cac:AccountingSupplierParty/cac:Party", ns)
    address = party.find("cac:PostalAddress", ns) if party is not None else None
    due = _text(root, "cbc:DueDate", ns) or _text(root, "cac:PaymentMeans/cbc:PaymentDueDate", ns)
    invoice = ERechnung(
        format="UBL",
        gutschrift=credit or _text(root, "cbc:InvoiceTypeCode", ns) in _CREDIT_TYPE_CODES,
        rechnungsnummer=_text(root, "cbc:ID", ns),
        rechnungsdatum=_iso_date(_text(root, "cbc:IssueDate", ns)),
        faelligkeitsdatum=_iso_date(due),
        waehrung=_text(root, "cbc:DocumentCurrencyCode", ns),
        lieferant_name=_text(party, "cac:PartyName/cbc:Name", ns)
        or _text(party, "cac:PartyLegalEntity/cbc:RegistrationName", ns),
        ust_id=_text(party, "cac:PartyTaxScheme/cbc:CompanyID", ns),
        email=_text(party, "cac:Contact/cbc:ElectronicMail", ns),
        telefon=_text(party, "cac:Contact/cbc:Telephone", ns),
        strasse=_text(address, "cbc:StreetName", ns),
        plz=_text(address, "cbc:PostalZone", ns),
        ort=_text(address, "cbc:CityName", ns),
        iban=_text(root, "cac:PaymentMeans/cac:PayeeFinancialAccount/cbc:ID", ns),
        brutto=_number(_text(root, "cac:LegalMonetaryTotal/cbc:PayableAmount", ns)),
    )
    for subtotal in root.findall("cac:TaxTotal/cac:TaxSubtotal", ns):
        netto = _number(_text(subtotal, "cbc:TaxableAmount", ns))
        if netto is not None:
            invoice.steuern.append(
                Steuersumme(
                    netto=netto,
                    prozent=_number(_text(subtotal, "cac:TaxCategory/cbc:Percent", ns)) or 0.0,
                    kategorie=_text(subtotal, "cac:TaxCategory/cbc:ID", ns) or "",
                )
            )
    line_tag = "cac:CreditNoteLine" if credit else "cac:InvoiceLine"
    quantity_tag = "cbc:CreditedQuantity" if credit else "cbc:InvoicedQuantity"
    for line in root.findall(line_tag, ns):
        price = _number(_text(line, "cac:Price/cbc:PriceAmount", ns))
        per = _number(_text(line, "cac:Price/cbc:BaseQuantity", ns))
        quantity = line.find(quantity_tag, ns)
        invoice.positionen.append(
            Position(
                beschreibung=_text(line, "cac:Item/cbc:Name", ns) or "",
                lieferanten_artikelnummer=_text(line, "cac:Item/cac:SellersItemIdentification/cbc:ID", ns),
                menge=_number(quantity.text) if quantity is not None else None,
                einheit=quantity.get("unitCode") if quantity is not None else None,
                einzelpreis=round(price / per, 6) if price is not None and per else price,
            )
        )
    return invoice

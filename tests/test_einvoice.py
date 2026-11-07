"""The e-invoice reader and how odoo_rechnung_ablegen uses it, on synthetic
invoices built here - never real supplier documents in the repository."""

from __future__ import annotations

import base64
import io
from datetime import date

import pytest
from pypdf import PdfWriter

from terbox_mcp import odoo_tools
from terbox_mcp.einvoice import read_einvoice
from terbox_mcp.odoo_models import RechnungAblageRequest
from tests.test_odoo_tools import FakeOdoo, _odoo_for_bills, odoo  # noqa: F401 - fixture

CII = """<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
  xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
  xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
  <rsm:ExchangedDocument>
    <ram:ID>{nummer}</ram:ID><ram:TypeCode>{typ}</ram:TypeCode>
    <ram:IssueDateTime><udt:DateTimeString format="102">20261106</udt:DateTimeString></ram:IssueDateTime>
  </rsm:ExchangedDocument>
  <rsm:SupplyChainTradeTransaction>
    <ram:IncludedSupplyChainTradeLineItem>
      <ram:SpecifiedTradeProduct><ram:SellerAssignedID>L-A</ram:SellerAssignedID><ram:Name>Torbeschlag</ram:Name></ram:SpecifiedTradeProduct>
      <ram:SpecifiedLineTradeAgreement><ram:NetPriceProductTradePrice><ram:ChargeAmount>25.00</ram:ChargeAmount><ram:BasisQuantity>10</ram:BasisQuantity></ram:NetPriceProductTradePrice></ram:SpecifiedLineTradeAgreement>
      <ram:SpecifiedLineTradeDelivery><ram:BilledQuantity unitCode="C62">40</ram:BilledQuantity></ram:SpecifiedLineTradeDelivery>
    </ram:IncludedSupplyChainTradeLineItem>
    <ram:ApplicableHeaderTradeAgreement>
      <ram:SellerTradeParty>
        <ram:Name>{name}</ram:Name>
        <ram:DefinedTradeContact>
          <ram:TelephoneUniversalCommunication><ram:CompleteNumber>0201 555</ram:CompleteNumber></ram:TelephoneUniversalCommunication>
          <ram:EmailURIUniversalCommunication><ram:URIID>rechnung@zaunbau.de</ram:URIID></ram:EmailURIUniversalCommunication>
        </ram:DefinedTradeContact>
        <ram:PostalTradeAddress><ram:PostcodeCode>45127</ram:PostcodeCode><ram:LineOne>Weg 1</ram:LineOne><ram:CityName>Essen</ram:CityName></ram:PostalTradeAddress>
        <ram:SpecifiedTaxRegistration><ram:ID schemeID="FC">111/222</ram:ID></ram:SpecifiedTaxRegistration>
        <ram:SpecifiedTaxRegistration><ram:ID schemeID="VA">DE123456789</ram:ID></ram:SpecifiedTaxRegistration>
      </ram:SellerTradeParty>
    </ram:ApplicableHeaderTradeAgreement>
    <ram:ApplicableHeaderTradeSettlement>
      <ram:InvoiceCurrencyCode>EUR</ram:InvoiceCurrencyCode>
      <ram:SpecifiedTradeSettlementPaymentMeans><ram:PayeePartyCreditorFinancialAccount><ram:IBANID>DE02120300000000202051</ram:IBANID></ram:PayeePartyCreditorFinancialAccount></ram:SpecifiedTradeSettlementPaymentMeans>
      <ram:ApplicableTradeTax><ram:BasisAmount>100.00</ram:BasisAmount><ram:CategoryCode>{steuerart}</ram:CategoryCode><ram:RateApplicablePercent>19</ram:RateApplicablePercent></ram:ApplicableTradeTax>
      <ram:SpecifiedTradePaymentTerms><ram:DueDateDateTime><udt:DateTimeString format="102">20261120</udt:DateTimeString></ram:DueDateDateTime></ram:SpecifiedTradePaymentTerms>
      <ram:SpecifiedTradeSettlementHeaderMonetarySummation><ram:GrandTotalAmount>119.00</ram:GrandTotalAmount></ram:SpecifiedTradeSettlementHeaderMonetarySummation>
    </ram:ApplicableHeaderTradeSettlement>
  </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>"""

UBL = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
  xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
  xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>UBL-7</cbc:ID><cbc:IssueDate>2026-11-02</cbc:IssueDate><cbc:DueDate>2026-12-02</cbc:DueDate>
  <cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode><cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty><cac:Party>
    <cac:PostalAddress><cbc:StreetName>Ring 2</cbc:StreetName><cbc:CityName>Bonn</cbc:CityName><cbc:PostalZone>53111</cbc:PostalZone></cac:PostalAddress>
    <cac:PartyTaxScheme><cbc:CompanyID>DE987654321</cbc:CompanyID></cac:PartyTaxScheme>
    <cac:PartyLegalEntity><cbc:RegistrationName>Stahl UBL GmbH</cbc:RegistrationName></cac:PartyLegalEntity>
    <cac:Contact><cbc:ElectronicMail>info@stahl.de</cbc:ElectronicMail></cac:Contact>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:PaymentMeans><cac:PayeeFinancialAccount><cbc:ID>DE89370400440532013000</cbc:ID></cac:PayeeFinancialAccount></cac:PaymentMeans>
  <cac:TaxTotal><cac:TaxSubtotal><cbc:TaxableAmount>200.00</cbc:TaxableAmount><cac:TaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>19</cbc:Percent></cac:TaxCategory></cac:TaxSubtotal>
  <cac:TaxSubtotal><cbc:TaxableAmount>50.00</cbc:TaxableAmount><cac:TaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>7</cbc:Percent></cac:TaxCategory></cac:TaxSubtotal></cac:TaxTotal>
  <cac:LegalMonetaryTotal><cbc:PayableAmount>291.50</cbc:PayableAmount></cac:LegalMonetaryTotal>
  <cac:InvoiceLine><cbc:InvoicedQuantity unitCode="MTR">5</cbc:InvoicedQuantity>
    <cac:Item><cbc:Name>Flachstahl</cbc:Name><cac:SellersItemIdentification><cbc:ID>FS-40</cbc:ID></cac:SellersItemIdentification></cac:Item>
    <cac:Price><cbc:PriceAmount>40.00</cbc:PriceAmount></cac:Price></cac:InvoiceLine>
</Invoice>"""


def _cii(nummer: str = "ER-1", typ: str = "380", name: str = "Zaunbau Müller GmbH", steuerart: str = "S") -> bytes:
    return CII.format(nummer=nummer, typ=typ, name=name, steuerart=steuerart).encode()


def _pdf_with(xml: bytes | None, name: str = "factur-x.xml") -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    if xml is not None:
        writer.add_attachment(name, xml)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# --- reader ------------------------------------------------------------------


def test_reads_zugferd_embedded_in_a_pdf() -> None:
    invoice = read_einvoice(_pdf_with(_cii()))

    assert invoice.format == "CII"
    assert invoice.rechnungsnummer == "ER-1"
    assert invoice.rechnungsdatum == date(2026, 11, 6)
    assert invoice.faelligkeitsdatum == date(2026, 11, 20)
    assert invoice.ust_id == "DE123456789"  # the VAT id, not the tax number next to it
    assert (invoice.email, invoice.telefon) == ("rechnung@zaunbau.de", "0201 555")
    assert (invoice.strasse, invoice.plz, invoice.ort) == ("Weg 1", "45127", "Essen")
    assert invoice.iban == "DE02120300000000202051"
    assert invoice.brutto == 119.0
    assert [(t.netto, t.prozent, t.kategorie) for t in invoice.steuern] == [(100.0, 19.0, "S")]
    [line] = invoice.positionen
    assert line.lieferanten_artikelnummer == "L-A"
    assert line.einzelpreis == 2.5  # 25.00 per 10 pieces
    assert not invoice.gutschrift


def test_reads_an_embedded_file_with_an_unusual_name() -> None:
    assert read_einvoice(_pdf_with(_cii(), name="invoice-data.xml")).rechnungsnummer == "ER-1"


def test_reads_xrechnung_ubl_as_plain_xml() -> None:
    invoice = read_einvoice(UBL.encode())

    assert invoice.format == "UBL"
    assert (invoice.rechnungsnummer, invoice.rechnungsdatum, invoice.faelligkeitsdatum) == (
        "UBL-7",
        date(2026, 11, 2),
        date(2026, 12, 2),
    )
    assert invoice.lieferant_name == "Stahl UBL GmbH"
    assert invoice.ust_id == "DE987654321"
    assert [(t.netto, t.prozent) for t in invoice.steuern] == [(200.0, 19.0), (50.0, 7.0)]
    assert invoice.positionen[0].einheit == "MTR"


def test_recognises_a_credit_note() -> None:
    assert read_einvoice(_cii(typ="381")).gutschrift


@pytest.mark.parametrize("content", [b"%PDF-1.4 not really a pdf", _pdf_with(None), b"<html/>", b"\x00\x01"])
def test_anything_else_is_simply_no_einvoice(content: bytes) -> None:
    assert read_einvoice(content) is None


# --- storing ------------------------------------------------------------------


def _anhang(content: bytes) -> list[dict]:
    return [{"dateiname": "re.pdf" if content.startswith(b"%PDF") else "re.xml", "inhalt_base64": base64.b64encode(content).decode()}]


def _request(content: bytes, **overrides) -> RechnungAblageRequest:
    values = {
        "lieferant_name": "Zaunbau Müller",
        "rechnungsnummer": "ER-1",
        "rechnungsdatum": "2026-11-06",
        "betraege": [{"netto": 99.0}],
        "anhaenge": _anhang(content),
    }
    return RechnungAblageRequest(**{**values, **overrides})


def test_einvoice_data_wins_over_what_the_agent_read(odoo: FakeOdoo) -> None:  # noqa: F811
    _odoo_for_bills(odoo)

    result = odoo_tools.odoo_rechnung_ablegen(
        _request(_pdf_with(_cii()), rechnungsnummer="ER-l", rechnungsdatum="2026-11-07")
    )

    vals = odoo.body_of("account.move", "create")["vals_list"][0]
    assert vals["ref"] == "ER-1"
    assert vals["invoice_date"] == "2026-11-06"
    assert vals["invoice_date_due"] == "2026-11-20"
    [line] = vals["invoice_line_ids"]
    assert line[2]["price_unit"] == 100.0  # from the XML, not the agent's 99
    assert result["quelle"] == "e-rechnung (CII)"
    assert "Rechnungsnummer laut E-Rechnung ER-1" in result["hinweis"]
    assert result["positionen"][0]["lieferanten_artikelnummer"] == "L-A"


def test_supplier_is_found_by_the_einvoice_vat_id_despite_a_garbled_name(odoo: FakeOdoo) -> None:  # noqa: F811
    _odoo_for_bills(odoo)
    result = odoo_tools.odoo_rechnung_ablegen(
        # What the agent reads off such a document, too: no usable name, no VAT id.
        _request(_pdf_with(_cii(name="Hohe Giethorst 60 46397 Bocholt")), lieferant_name="Hohe Giethorst 60")
    )
    assert result["lieferant_id"] == 26


def test_non_standard_tax_is_stored_with_its_amount_but_no_guessed_tax(odoo: FakeOdoo) -> None:  # noqa: F811
    _odoo_for_bills(odoo)
    result = odoo_tools.odoo_rechnung_ablegen(_request(_pdf_with(_cii(steuerart="AE"))))
    [line] = odoo.body_of("account.move", "create")["vals_list"][0]["invoice_line_ids"]
    assert line[2]["price_unit"] == 100.0
    assert "tax_ids" not in line[2]
    assert "Steuerart AE" in result["hinweis"]


def test_unknown_supplier_gets_a_proposal_from_the_einvoice(odoo: FakeOdoo, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    import sys

    monkeypatch.setattr(sys.modules["tests.test_odoo_tools"], "SUPPLIERS", [])
    _odoo_for_bills(odoo)

    result = odoo_tools.odoo_rechnung_ablegen(_request(_pdf_with(_cii()), lieferant_name="Zaunbau Neu GmbH"))

    assert result["status"] == "lieferant_unbekannt"
    assert result["lieferant_vorschlag"]["ust_id"] == "DE123456789"
    assert result["lieferant_vorschlag"]["email"] == "rechnung@zaunbau.de"
    assert odoo.writes() == []


def test_invoice_already_stored_under_another_supplier_is_not_stored_again(odoo: FakeOdoo, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    import sys

    monkeypatch.setattr(sys.modules["tests.test_odoo_tools"], "SUPPLIERS", [])
    _odoo_for_bills(odoo)
    odoo.responses[("account.move", "search_read")] = [{"id": 5, "partner_id": [30, "Reiswich GmbH"], "state": "draft"}]

    result = odoo_tools.odoo_rechnung_ablegen(_request(_pdf_with(_cii()), lieferant_name="Haus & Dach"))

    assert result["status"] == "bereits_abgelegt"
    assert "Reiswich GmbH" in result["meldung"]
    assert odoo.writes() == []


def test_same_number_at_a_different_known_supplier_is_flagged_not_stored(odoo: FakeOdoo) -> None:  # noqa: F811
    _odoo_for_bills(odoo)
    odoo.responses[("account.move", "search_read")] = [{"id": 5, "partner_id": [30, "Reiswich GmbH"], "state": "draft"}]

    result = odoo_tools.odoo_rechnung_ablegen(_request(_pdf_with(_cii())))

    assert result["status"] == "nummer_bei_anderem_lieferanten"
    assert odoo.writes() == []


def test_cost_type_goes_on_the_line_as_analytic_distribution(odoo: FakeOdoo) -> None:  # noqa: F811
    _odoo_for_bills(odoo)
    odoo.responses[("account.analytic.account", "search_read")] = [
        {"id": 3, "name": "Werkzeuge & Betriebsmittel", "plan_id": [4, "Kostenarten / Gemeinkosten"]}
    ]

    odoo_tools.odoo_rechnung_ablegen(_request(_pdf_with(_cii()), kategorie="werkzeuge & betriebsmittel"))

    [line] = odoo.body_of("account.move", "create")["vals_list"][0]["invoice_line_ids"]
    assert line[2]["analytic_distribution"] == {"3": 100}
    assert "account_id" not in line[2]  # the financial account stays Odoo's choice
    assert line[2]["name"].endswith("– Werkzeuge & Betriebsmittel")
    search = odoo.body_of("account.analytic.account", "search_read")
    assert ["root_plan_id.name", "=", "Kostenarten"] in search["domain"]


def test_unknown_cost_type_still_stores_the_invoice(odoo: FakeOdoo) -> None:  # noqa: F811
    _odoo_for_bills(odoo)
    result = odoo_tools.odoo_rechnung_ablegen(_request(_pdf_with(_cii()), kategorie="Gibtsnicht"))
    assert result["status"] == "abgelegt"
    assert "Kostenart 'Gibtsnicht' gibt es nicht" in result["hinweis"]
    [line] = odoo.body_of("account.move", "create")["vals_list"][0]["invoice_line_ids"]
    assert "analytic_distribution" not in line[2]

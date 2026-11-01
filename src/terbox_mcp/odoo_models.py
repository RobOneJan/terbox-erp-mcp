"""Pydantic models for the Odoo write tools.

Attachment bytes travel separately from everything else: `inhalt_base64` is
split off into the approval's `blobs` before the rest is stored as reviewable
`data`. agent-hub renders every payload field as an editable input on the Teams
card, so a base64 PDF left in there would produce an unusable card.
"""

from __future__ import annotations

import base64
import binascii
import re
from datetime import date
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024

# Sniffed from the content, never trusted from the filename or the sender.
_MAGIC_TYPES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)

ATTACHABLE_MODELS = frozenset(
    {"account.move", "purchase.order", "sale.order", "res.partner", "product.template"}
)


def sniff_content_type(raw: bytes) -> str | None:
    head = raw[:1024]
    for magic, content_type in _MAGIC_TYPES:
        if magic == b"%PDF-":  # some generators put a few bytes before the header
            if magic in head:
                return content_type
        elif head.startswith(magic):
            return content_type
    if head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<?xml"):  # XRechnung
        return "application/xml"
    return None


def safe_filename(value: str) -> str:
    name = re.split(r"[\\/]", value.strip())[-1]  # never keep a path
    name = re.sub(r"[^\w.\- ()]", "_", name)
    if not name.strip(". "):
        raise ValueError("dateiname is empty")
    return name


class Anhang(BaseModel):
    dateiname: str = Field(min_length=1, max_length=200)
    inhalt_base64: str = Field(min_length=1)

    @field_validator("dateiname")
    @classmethod
    def _safe_filename(cls, value: str) -> str:
        return safe_filename(value)

    @field_validator("inhalt_base64")
    @classmethod
    def _allowed_file_within_limit(cls, value: str) -> str:
        value = "".join(value.split())  # tolerate line breaks from MIME-style encoders
        if len(value) * 3 // 4 > MAX_ATTACHMENT_BYTES + 3:
            raise ValueError(f"file is larger than {MAX_ATTACHMENT_BYTES // 1024 // 1024} MB")
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("inhalt_base64 is not valid base64") from exc
        if sniff_content_type(raw) is None:
            raise ValueError("only PDF, PNG, JPEG and XML files can be stored")
        return value

    def raw(self) -> bytes:
        return base64.b64decode(self.inhalt_base64)


def _distinct_filenames(anhaenge: list[Anhang]) -> None:
    names = [anhang.dateiname.lower() for anhang in anhaenge]
    if len(set(names)) != len(names):
        raise ValueError("anhaenge must have distinct dateiname values")


class Lieferant(BaseModel):
    name: str = Field(min_length=1)
    ust_id: Optional[str] = None
    email: Optional[str] = None
    telefon: Optional[str] = None
    strasse: Optional[str] = None
    plz: Optional[str] = None
    ort: Optional[str] = None


class Rechnungsposition(BaseModel):
    beschreibung: str = Field(min_length=1)
    menge: float = Field(default=1.0, gt=0)
    einzelpreis: float = Field(description="Net unit price.")
    steuer_prozent: Optional[float] = Field(default=None, ge=0, le=100)
    steuer_name: Optional[str] = Field(
        default=None,
        description=(
            "Exact Odoo tax name, e.g. '19% I' (domestic) or '19% EU' (reverse charge). Filled in "
            "automatically from steuer_prozent if omitted; set it explicitly for non-domestic suppliers."
        ),
    )
    artikel_referenz: Optional[str] = Field(
        default=None, description="Internal product reference (default_code), links the line to the product."
    )


class EingangsrechnungDaten(BaseModel):
    """Everything about a vendor bill except the file contents - this is what a
    human reviews on the approval card."""

    lieferant_id: Optional[int] = Field(default=None, description="Existing Odoo partner id.")
    lieferant: Optional[Lieferant] = Field(default=None, description="New supplier, if none exists yet.")
    rechnungsnummer: str = Field(min_length=1, description="The supplier's own invoice number.")
    rechnungsdatum: date
    faelligkeitsdatum: Optional[date] = None
    waehrung: str = Field(default="EUR", min_length=3, max_length=3)
    positionen: list[Rechnungsposition] = Field(default_factory=list)

    @field_validator("waehrung")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _exactly_one_supplier(self):
        if (self.lieferant is None) == (self.lieferant_id is None):
            raise ValueError("give exactly one of `lieferant` or `lieferant_id`")
        return self


class EingangsrechnungRequest(EingangsrechnungDaten):
    anhaenge: list[Anhang] = Field(min_length=1)

    @model_validator(mode="after")
    def _distinct(self):
        _distinct_filenames(self.anhaenge)
        return self


class AnhangAblageDaten(BaseModel):
    res_model: str = Field(description="One of: " + ", ".join(sorted(ATTACHABLE_MODELS)))
    res_id: int

    @field_validator("res_model")
    @classmethod
    def _attachable(cls, value: str) -> str:
        if value not in ATTACHABLE_MODELS:
            raise ValueError(f"attachments can only be stored on: {', '.join(sorted(ATTACHABLE_MODELS))}")
        return value


class AnhangAblageRequest(AnhangAblageDaten):
    anhaenge: list[Anhang] = Field(min_length=1)

    @model_validator(mode="after")
    def _distinct(self):
        _distinct_filenames(self.anhaenge)
        return self


class MaterialRequest(BaseModel):
    artikel_referenz: str = Field(min_length=1, description="Internal reference (default_code), e.g. 'P00067'.")
    name: str = Field(min_length=1)
    kategorie: str = Field(min_length=1, description="Exact name of an existing product category.")
    einheit: str = Field(default="Stk", min_length=1, description="Exact name of an existing unit of measure.")
    lieferant_id: Optional[int] = None
    lieferanten_artikelnummer: Optional[str] = None
    einkaufspreis: Optional[float] = Field(default=None, ge=0, description="Net purchase price from this supplier.")

    @model_validator(mode="after")
    def _price_needs_supplier(self):
        if self.lieferant_id is None and (self.einkaufspreis is not None or self.lieferanten_artikelnummer):
            raise ValueError("einkaufspreis and lieferanten_artikelnummer need a lieferant_id")
        return self


class EinkaufspreisRequest(BaseModel):
    lieferantenpreis_id: int = Field(description="product.supplierinfo id, e.g. from odoo_preise_pruefen.")
    neuer_preis: float = Field(ge=0, description="New net purchase price.")


class PreisPruefPosition(BaseModel):
    beschreibung: Optional[str] = None
    lieferanten_artikelnummer: Optional[str] = None
    artikel_referenz: Optional[str] = None
    einzelpreis: float = Field(description="Net unit price on the supplier's document.")


class AttachmentDTO(BaseModel):
    """Exact shape agent-hub's orchestrator._looks_like_attachment detects, so the
    file reaches the user and Claude can read it. Must be returned on its own,
    not as a Union: a Union return nests structured_content under "result" and
    the hub would no longer recognize it."""

    filename: str
    content_type: str
    content_base64: str
    size_bytes: int

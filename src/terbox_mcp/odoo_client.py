"""Client for the Odoo External JSON-2 API (POST /json/2/<model>/<method>).

Two deliberately different halves:

- Reads are generic, so every model in the database is reachable - but only
  through `READ_METHODS`, and never for `DENIED_MODELS` / `DENIED_FIELDS`. The
  API key behind this client can see far more than an agent should (credentials
  in `ir.config_parameter`, other users' API keys, ...), so those limits live
  here rather than in the Odoo user's rights alone.
- Writes are NOT generic. There is one typed method per permitted action and no
  way to reach `create`/`write` on an arbitrary model through this class. Bills
  are only ever created as drafts; nothing here posts, pays or deletes business
  records.

Every JSON-2 call runs in its own Odoo transaction, so multi-step writes are not
atomic. Rule used throughout: look up before writing, so retries are safe.

Field names follow Odoo saas~19.4, where `ir.attachment.datas` no longer exists
(`raw` replaced it) and `uom.uom.category_id` / `product.template.uom_po_id` are
gone.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

READ_METHODS = frozenset(
    {"search_read", "read", "search", "search_count", "fields_get", "name_search", "formatted_read_group"}
)

DENIED_MODELS = frozenset(
    {
        "account.online.account",
        "account.online.link",
        "auth.passkey.key",
        "auth_totp.device",
        "fetchmail.server",
        "iap.account",
        "ir.config_parameter",
        "ir.mail_server",
        "payment.provider",
        "res.users.apikeys",
        "res.users.apikeys.description",
        "res.users.identitycheck",
    }
)

# Checked on every path segment, in requested fields, groupings, aggregates and
# domain leaves - a domain like [["password", "=like", "a%"]] would otherwise
# leak a secret one character at a time without ever returning the field.
DENIED_FIELDS = frozenset(
    {
        "access_token",
        "api_key_ids",
        "datas",
        "db_datas",
        "new_password",
        "oauth_access_token",
        "password",
        "raw",
        "signup_token",
        "store_fname",
        "totp_secret",
    }
)

MAX_LIMIT = 200
DEFAULT_LIMIT = 50
# Binary fields (images, reports) come back as huge base64 strings. Nothing an
# agent reads through a generic tool needs them; odoo_anhang_lesen is the path
# for actual files.
MAX_VALUE_CHARS = 5000


class OdooError(ToolError):
    """Raised when Odoo rejects a call or cannot be reached.

    A ToolError because the MCP SDK replaces the text of any other exception with
    a generic message, and the agent needs the reason (unknown supplier, article
    already exists, ...) from a prepare tool that has to raise instead of return.
    """

    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Odoo error {status_code}: {detail}")


class OdooAccessDenied(OdooError):
    """The request touches a model, method or field this server never exposes."""

    def __init__(self, detail: str):
        super().__init__(403, detail)


@dataclass
class OdooSettings:
    base_url: str
    api_key: str
    database: str | None = None
    timeout: float = 25.0

    @classmethod
    def from_env(cls) -> "OdooSettings":
        base_url = os.environ.get("ODOO_URL")
        api_key = os.environ.get("ODOO_API_KEY")
        if not base_url:
            raise RuntimeError("ODOO_URL is not set")
        if not api_key:
            raise RuntimeError("ODOO_API_KEY is not set")
        return cls(
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            database=os.environ.get("ODOO_DB") or None,
            timeout=float(os.environ.get("ODOO_API_TIMEOUT", "25")),
        )


def _field_segments(name: str) -> list[str]:
    # "partner_id.user_id.password:max" -> ["partner_id", "user_id", "password"]
    return name.split(":", 1)[0].split(".")


def check_fields(names: list[str]) -> None:
    for name in names:
        if not isinstance(name, str):
            raise OdooAccessDenied(f"invalid field name: {name!r}")
        denied = DENIED_FIELDS.intersection(_field_segments(name))
        if denied:
            raise OdooAccessDenied(f"field {sorted(denied)[0]!r} is not available through this server")


def check_domain(domain: list) -> None:
    for leaf in domain:
        if isinstance(leaf, str):  # "&", "|", "!"
            continue
        if not isinstance(leaf, (list, tuple)) or len(leaf) != 3:
            raise OdooError(400, f"invalid domain leaf: {leaf!r}")
        check_fields([leaf[0]])
        # Sub-domains, e.g. ["partner_id", "any", [["password", "=", "x"]]].
        if leaf[1] in ("any", "not any") and isinstance(leaf[2], list):
            check_domain(leaf[2])


def check_model(model: str) -> None:
    if model in DENIED_MODELS:
        raise OdooAccessDenied(f"model {model!r} is not available through this server")


def _shorten(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        return f"<{len(value)} characters omitted>"
    return value


def _shorten_rows(rows: list[dict]) -> list[dict]:
    return [{key: _shorten(value) for key, value in row.items()} for row in rows]


def _clean(vals: dict) -> dict:
    return {key: value for key, value in vals.items() if value not in (None, "")}


class OdooClient:
    """Thin wrapper around httpx for calling Odoo."""

    def __init__(self, settings: OdooSettings | None = None, transport: httpx.BaseTransport | None = None):
        self.settings = settings or OdooSettings.from_env()
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "terbox-erp-mcp",
        }
        if self.settings.database:
            headers["X-Odoo-Database"] = self.settings.database
        return headers

    def _call(self, model: str, method: str, **payload: Any) -> Any:
        url = f"{self.settings.base_url}/json/2/{model}/{method}"
        try:
            with httpx.Client(timeout=self.settings.timeout, transport=self._transport) as client:
                response = client.post(url, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise OdooError(502, f"{model}.{method}: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("message") or response.text
            except (ValueError, AttributeError):
                detail = response.text
            raise OdooError(response.status_code, f"{model}.{method}: {str(detail)[:500]}")
        return response.json()

    def _create(self, model: str, vals: dict) -> int:
        result = self._call(model, "create", vals_list=[vals])
        return result[0] if isinstance(result, list) else result

    # --- generic reads -----------------------------------------------------

    def search_read(
        self,
        model: str,
        domain: list | None = None,
        fields: list[str] | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        order: str | None = None,
    ) -> list[dict]:
        check_model(model)
        domain = domain or []
        check_domain(domain)
        # Required: Odoo's default of "all fields" returns every binary and
        # computed field of the model.
        if not fields:
            raise OdooError(400, "fields must list the fields to read (see odoo_felder)")
        check_fields(fields)
        payload: dict[str, Any] = {
            "domain": domain,
            "fields": fields,
            "limit": max(1, min(limit, MAX_LIMIT)),
            "offset": max(0, offset),
        }
        if order:
            check_fields([part.strip().split(" ")[0] for part in order.split(",")])
            payload["order"] = order
        return _shorten_rows(self._call(model, "search_read", **payload))

    def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict]:
        check_model(model)
        if not fields:
            raise OdooError(400, "fields must list the fields to read (see odoo_felder)")
        check_fields(fields)
        return _shorten_rows(self._call(model, "read", ids=ids[:MAX_LIMIT], fields=fields))

    def search_count(self, model: str, domain: list | None = None) -> int:
        check_model(model)
        domain = domain or []
        check_domain(domain)
        return self._call(model, "search_count", domain=domain)

    def read_group(self, model: str, domain: list | None, groupby: list[str], aggregates: list[str]) -> list[dict]:
        check_model(model)
        domain = domain or []
        check_domain(domain)
        check_fields(groupby)
        check_fields(aggregates)
        return self._call(model, "formatted_read_group", domain=domain, groupby=groupby, aggregates=aggregates)

    def name_search(self, model: str, name: str, limit: int = 10) -> list:
        check_model(model)
        return self._call(model, "name_search", name=name, limit=max(1, min(limit, MAX_LIMIT)))

    def fields_get(self, model: str) -> dict[str, dict]:
        check_model(model)
        result = self._call(model, "fields_get", attributes=["string", "type", "relation", "required", "selection"])
        return {name: meta for name, meta in result.items() if name not in DENIED_FIELDS}

    def list_models(self, search: str | None = None, limit: int = MAX_LIMIT, offset: int = 0) -> list[dict]:
        domain: list = [["transient", "=", False]]
        if search:
            domain += ["|", ["model", "ilike", search], ["name", "ilike", search]]
        rows = self.search_read("ir.model", domain, ["model", "name"], limit=limit, offset=offset, order="model")
        return [row for row in rows if row["model"] not in DENIED_MODELS]

    def get_attachment(self, attachment_id: int) -> dict:
        rows = self._call(
            "ir.attachment",
            "search_read",
            domain=[["id", "=", attachment_id]],
            fields=["name", "mimetype", "file_size", "raw"],
            limit=1,
        )
        if not rows:
            raise OdooError(404, f"attachment {attachment_id} not found")
        return rows[0]

    # --- lookups used to prepare a write -----------------------------------

    def get_one(self, model: str, record_id: int, fields: list[str]) -> dict | None:
        rows = self.read(model, [record_id], fields)
        return rows[0] if rows else None

    def resolve_purchase_tax(self, percent: float, name: str | None = None) -> dict:
        """Several purchase taxes share one percentage ("19% I" domestic, "19% EU"
        reverse charge, ...). Without an explicit name the first by Odoo's own
        sequence wins, which is the domestic one - the caller surfaces the
        resolved name on the approval card so a human can correct it."""
        domain = [["type_tax_use", "=", "purchase"], ["amount_type", "=", "percent"], ["amount", "=", percent]]
        if name:
            domain.append(["name", "=", name])
        rows = self.search_read("account.tax", domain, ["name", "amount"], limit=1, order="sequence, id")
        if not rows:
            suffix = f" named {name!r}" if name else ""
            raise OdooError(404, f"no purchase tax of {percent}%{suffix} found")
        return rows[0]

    def find_supplier(self, name: str, ust_id: str | None = None) -> int | None:
        domains = ([[["vat", "=", ust_id]]] if ust_id else []) + [[["name", "=", name]]]
        for domain in domains:
            rows = self.search_read("res.partner", domain + [["parent_id", "=", False]], ["id"], limit=1)
            if rows:
                return rows[0]["id"]
        return None

    def find_vendor_bill(self, partner_id: int, rechnungsnummer: str) -> int | None:
        rows = self.search_read(
            "account.move",
            [["move_type", "=", "in_invoice"], ["partner_id", "=", partner_id], ["ref", "=", rechnungsnummer]],
            ["id"],
            limit=1,
        )
        return rows[0]["id"] if rows else None

    def find_product_template(self, default_code: str) -> dict | None:
        rows = self.search_read(
            "product.template", [["default_code", "=", default_code]], ["name", "default_code", "list_price"], limit=1
        )
        return rows[0] if rows else None

    def resolve_by_name(self, model: str, field: str, name: str) -> int:
        rows = self.search_read(model, [[field, "=", name]], ["id"], limit=1)
        if not rows:
            raise OdooError(404, f"no {model} with {field} {name!r} found")
        return rows[0]["id"]

    def supplier_prices(self, partner_id: int) -> list[dict]:
        rows = self.search_read(
            "product.supplierinfo",
            [["partner_id", "=", partner_id]],
            ["product_tmpl_id", "product_code", "product_name", "price", "currency_id"],
            limit=MAX_LIMIT,
        )
        tmpl_ids = list({row["product_tmpl_id"][0] for row in rows if row.get("product_tmpl_id")})
        codes = {t["id"]: t["default_code"] for t in self.read("product.template", tmpl_ids, ["default_code"])} if tmpl_ids else {}
        for row in rows:
            tmpl = row.get("product_tmpl_id")
            row["default_code"] = codes.get(tmpl[0]) if tmpl else None
        return rows

    # --- the only writes this server can perform ---------------------------

    def attach(self, res_model: str, res_id: int, filename: str, content: bytes, mimetype: str) -> dict:
        """Attach a file, skipping it if a file of that name is already there.

        Verifies Odoo's own sha1 `checksum` against the bytes we sent: `raw` is a
        binary field whose wire encoding differs across Odoo versions, and a
        wrongly decoded upload would otherwise look successful while storing a
        corrupt invoice. A mismatching attachment is removed again (it is the one
        record we just created) and reported as an error.
        """
        present = self._call(
            "ir.attachment",
            "search_read",
            domain=[["res_model", "=", res_model], ["res_id", "=", res_id], ["name", "=", filename]],
            fields=["id"],
            limit=1,
        )
        if present:
            return {"attachment_id": present[0]["id"], "added": False}
        attachment_id = self._create(
            "ir.attachment",
            {
                "name": filename,
                "raw": base64.b64encode(content).decode("ascii"),
                "res_model": res_model,
                "res_id": res_id,
                "mimetype": mimetype,
            },
        )
        stored = self._call("ir.attachment", "read", ids=[attachment_id], fields=["checksum"])
        if stored and stored[0].get("checksum") != hashlib.sha1(content).hexdigest():
            self._call("ir.attachment", "unlink", ids=[attachment_id])
            raise OdooError(502, f"attachment {filename!r} was stored corrupted and has been removed again")
        return {"attachment_id": attachment_id, "added": True}

    def create_supplier(
        self,
        *,
        name: str,
        ust_id: str | None = None,
        email: str | None = None,
        telefon: str | None = None,
        strasse: str | None = None,
        plz: str | None = None,
        ort: str | None = None,
    ) -> int:
        return self._create(
            "res.partner",
            _clean(
                {
                    "name": name,
                    "is_company": True,
                    "supplier_rank": 1,
                    "vat": ust_id,
                    "email": email,
                    "phone": telefon,
                    "street": strasse,
                    "zip": plz,
                    "city": ort,
                }
            ),
        )

    def create_draft_vendor_bill(
        self,
        *,
        partner_id: int,
        rechnungsnummer: str,
        rechnungsdatum: str,
        faelligkeitsdatum: str | None,
        waehrung: str,
        lines: list[dict],
    ) -> int:
        currency_id = self.resolve_by_name("res.currency", "name", waehrung)
        return self._create(
            "account.move",
            _clean(
                {
                    "move_type": "in_invoice",
                    "partner_id": partner_id,
                    "ref": rechnungsnummer,
                    "invoice_date": rechnungsdatum,
                    "invoice_date_due": faelligkeitsdatum,
                    "currency_id": currency_id,
                    "invoice_line_ids": [[0, 0, line] for line in lines] or None,
                }
            ),
        )

    def set_main_attachment(self, bill_id: int, attachment_id: int) -> None:
        self._call("account.move", "write", ids=[bill_id], vals={"message_main_attachment_id": attachment_id})

    def create_product(self, *, default_code: str, name: str, categ_id: int, uom_id: int) -> int:
        return self._create(
            "product.template",
            {"default_code": default_code, "name": name, "categ_id": categ_id, "uom_id": uom_id, "purchase_ok": True},
        )

    def create_supplier_price(
        self, *, tmpl_id: int, partner_id: int, price: float, product_code: str | None
    ) -> int:
        return self._create(
            "product.supplierinfo",
            _clean({"product_tmpl_id": tmpl_id, "partner_id": partner_id, "price": price, "product_code": product_code}),
        )

    def set_supplier_price(self, supplierinfo_id: int, price: float) -> None:
        self._call("product.supplierinfo", "write", ids=[supplierinfo_id], vals={"price": price})

    def set_sales_price(self, tmpl_id: int, price: float) -> None:
        self._call("product.template", "write", ids=[tmpl_id], vals={"list_price": price})

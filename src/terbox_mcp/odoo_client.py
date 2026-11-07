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

# The supplier fields the agent may fill in or (with approval) change: tool
# field name -> Odoo field. Deliberately no bank details.
SUPPLIER_CONTACT_FIELDS = {
    "email": "email",
    "telefon": "phone",
    "ust_id": "vat",
    "strasse": "street",
    "plz": "zip",
    "ort": "city",
}

# Analytic plan whose accounts are the cost types an invoice can be filed under.
COST_TYPE_PLAN = "Kostenarten"

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

    def match_supplier(self, name: str | None, ust_id: str | None) -> tuple[int | None, list[dict]]:
        """Find the supplier of an invoice: by VAT id, then exact name, then a
        unique partial name among suppliers (either name containing the other).
        Returns (id, []) on a clear match,
        (None, candidates) when several fit, and (None, []) when none does."""
        top = [["parent_id", "=", False]]
        if ust_id:
            vat = "".join(ust_id.split()).upper()
            rows = self.search_read("res.partner", top + [["vat", "=", vat]], ["id"], limit=1)
            if rows:
                return rows[0]["id"], []
        if not name:
            return None, []
        rows = self.search_read("res.partner", top + [["name", "=ilike", name.strip()]], ["id"], limit=1)
        if rows:
            return rows[0]["id"], []
        suppliers = top + [["supplier_rank", ">", 0]]
        rows = self.search_read("res.partner", suppliers + [["name", "ilike", name.strip()]], ["id", "name"], limit=5)
        if not rows:
            # The other way round: the invoice prints more than Odoo stores, e.g.
            # "E&S Zaun GbR, Inh. M. Schmidt" for "E&S Zaun GbR".
            printed = name.lower()
            everyone = self.search_read("res.partner", suppliers, ["id", "name"], limit=MAX_LIMIT)
            rows = [row for row in everyone if row["name"] and row["name"].lower() in printed]
        if len(rows) == 1:
            return rows[0]["id"], []
        return None, rows[:5]

    def get_supplier(self, partner_id: int) -> dict | None:
        """Only companies marked as suppliers - an invoice must never land on a
        customer or a contact person because a model guessed the wrong id."""
        rows = self.search_read(
            "res.partner",
            [["id", "=", partner_id], ["parent_id", "=", False], ["supplier_rank", ">", 0]],
            ["name"],
            limit=1,
        )
        return rows[0] if rows else None

    def supplier_contact(self, partner_id: int) -> dict[str, str]:
        rows = self.read("res.partner", [partner_id], list(SUPPLIER_CONTACT_FIELDS.values()))
        row = rows[0] if rows else {}
        return {name: row.get(field) or "" for name, field in SUPPLIER_CONTACT_FIELDS.items()}

    def supplier_ibans(self, partner_id: int) -> set[str]:
        rows = self.search_read(
            "res.partner.bank", [["partner_id", "child_of", partner_id]], ["sanitized_account_number"], limit=MAX_LIMIT
        )
        return {row["sanitized_account_number"] for row in rows if row.get("sanitized_account_number")}

    def update_supplier_contact(self, partner_id: int, values: dict[str, str]) -> None:
        """`values` uses the tool's field names; anything outside
        SUPPLIER_CONTACT_FIELDS is refused rather than passed on."""
        unknown = set(values) - set(SUPPLIER_CONTACT_FIELDS)
        if unknown:
            raise OdooAccessDenied(f"supplier fields {sorted(unknown)} cannot be changed through this server")
        vals = {SUPPLIER_CONTACT_FIELDS[name]: value for name, value in values.items()}
        self._call("res.partner", "write", ids=[partner_id], vals=vals)

    def find_bills_by_ref(self, rechnungsnummer: str) -> list[dict]:
        """Vendor bills with this supplier invoice number, for any supplier,
        leaving out cancelled ones."""
        rows = self.search_read(
            "account.move",
            [["move_type", "=", "in_invoice"], ["ref", "=", rechnungsnummer], ["state", "!=", "cancel"]],
            ["partner_id", "state", "amount_total"],
            limit=10,
        )
        return [
            {
                "id": row["id"],
                "partner_id": row["partner_id"][0] if row.get("partner_id") else None,
                "partner_name": row["partner_id"][1] if row.get("partner_id") else "?",
                "status": row.get("state"),
                "betrag": row.get("amount_total"),
            }
            for row in rows
        ]

    def find_cost_type(self, name: str) -> dict | None:
        """An analytic account of the COST_TYPE_PLAN plan (Einzelkosten /
        Gemeinkosten below it), for internal reporting of what money went on."""
        rows = self.search_read(
            "account.analytic.account",
            [["root_plan_id.name", "=", COST_TYPE_PLAN], ["name", "=ilike", name.strip()]],
            ["name", "plan_id"],
            limit=1,
        )
        if not rows:
            return None
        plan = rows[0].get("plan_id")
        return {"id": rows[0]["id"], "name": rows[0]["name"], "plan": plan[1] if isinstance(plan, list) else None}

    def cost_type_names(self) -> list[str]:
        rows = self.search_read(
            "account.analytic.account", [["root_plan_id.name", "=", COST_TYPE_PLAN]], ["name"], limit=MAX_LIMIT
        )
        return sorted(row["name"] for row in rows)

    def bill_total(self, bill_id: int) -> float | None:
        rows = self.read("account.move", [bill_id], ["amount_total"])
        return rows[0].get("amount_total") if rows else None

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
        supplierinfo_id = self._create(
            "product.supplierinfo",
            _clean({"product_tmpl_id": tmpl_id, "partner_id": partner_id, "price": price, "product_code": product_code}),
        )
        if price:
            self._sync_cost(supplierinfo_id, price)
        return supplierinfo_id

    def set_supplier_price(self, supplierinfo_id: int, price: float) -> bool:
        """Returns whether the article's cost followed (see _sync_cost)."""
        self._call("product.supplierinfo", "write", ids=[supplierinfo_id], vals={"price": price})
        return self._sync_cost(supplierinfo_id, price)

    def _sync_cost(self, supplierinfo_id: int, price: float) -> bool:
        """Set the article's cost (standard_price) to this supplier price when it
        is the article's main supplier - the first by Odoo's own sequence, the
        one purchase orders use too. Sales prices follow from the cost via the
        pricelist's markups, so a new purchase price reaches them without anyone
        touching a sales price. A failure here never undoes the price change."""
        try:
            rows = self.read("product.supplierinfo", [supplierinfo_id], ["product_tmpl_id"])
            tmpl = rows[0].get("product_tmpl_id") if rows else None
            if not tmpl:
                return False
            tmpl_id = tmpl[0] if isinstance(tmpl, list) else tmpl
            main = self.search_read(
                "product.supplierinfo", [["product_tmpl_id", "=", tmpl_id]], ["id"], limit=1, order="sequence, id"
            )
            if not main or main[0]["id"] != supplierinfo_id:
                return False
            self._call("product.template", "write", ids=[tmpl_id], vals={"standard_price": price})
            return True
        except OdooError:
            return False

    def set_sales_price(self, tmpl_id: int, price: float) -> None:
        self._call("product.template", "write", ids=[tmpl_id], vals={"list_price": price})

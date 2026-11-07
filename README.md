# terbox-erp-mcp

MCP server exposing the TER BOX CAD Backend API (AB1000 box configuration,
customer creation and quoting) and Odoo as agent tools, driven through
`agent-hub` (Teams, Telegram). agent-hub already reaches this server as
`terbox`, so new tools need no change there.

## Odoo tools

Talks directly to Odoo's JSON-2 API (`odoo_client.py`), not through the CAD
Backend.

**Reading** runs immediately and covers every model:

- `odoo_firmenueberblick` — open/overdue invoices, orders, master data gaps.
- `odoo_modelle`, `odoo_felder` — discover what can be queried.
- `odoo_suchen`, `odoo_lesen`, `odoo_zaehlen`, `odoo_gruppieren`,
  `odoo_namen_suchen` — generic reads on any model.
- `odoo_anhang_lesen` — fetch a stored file.
- `odoo_preise_pruefen` — compare a supplier document with stored prices.

Credentials and security models (`DENIED_MODELS`) and secret or binary fields
(`DENIED_FIELDS`) are refused before a request leaves this server, including
when used only in a domain filter, grouping or sort order. Custom read methods
on individual models are not exposed, only the generic ones (`READ_METHODS`).

**Supplier invoices** go to `odoo_rechnung_ablegen` without approval: it finds
the supplier from name or VAT id and stores a draft bill with the PDF and one
total line per tax rate, never the individual items, and never posts it. It is
never stored without amounts. It first checks whether the invoice number is
already in Odoo for any supplier. An optional cost type (`kategorie`, an analytic
account of the plan "Kostenarten", split into Einzelkosten and Gemeinkosten)
goes on the line for internal overhead reporting; the financial account stays
Odoo's own choice. Only known suppliers qualify.

For e-invoices (ZUGFeRD / Factur-X / XRechnung, embedded in the PDF or as an XML
file; see `einvoice.py`) the data in the file wins over what the agent read:
number, dates, totals per tax rate, VAT id, contact details and IBAN. Its items
come back so the agent can compare them with stored prices
(`odoo_preise_pruefen`) and request all changes on one card
(`odoo_einkaufspreise_aendern`). Odoo's own e-invoice import is not used: it
creates every item line and stores the invoice's IBAN as a bank account. For an unknown one the agent asks for approval to
create it (`odoo_lieferant_anlegen`), then stores the invoice. Repeating it for
the same invoice creates nothing twice.

Contact details on the invoice or in its email (e-mail, phone, VAT id, address)
are added to the supplier where Odoo has none; existing values are never
overwritten. Differing ones go to `odoo_lieferant_aktualisieren` for approval.
Bank details are never changed: an IBAN differing from the one in Odoo only
adds a warning to the chat message, since a changed bank account is a common
invoice fraud.

**Other writes** need approval, each a prepare tool plus an `_execute` tool.
There is no generic write.

| Prepare tool | Effect after approval |
| --- | --- |
| `odoo_lieferant_anlegen` | New supplier with the details from its invoice. |
| `odoo_lieferant_aktualisieren` | Changes a supplier's differing contact details. |
| `odoo_anhang_ablegen` | Files on an invoice, order, partner or product. |
| `odoo_material_anlegen` | New article, optionally with supplier and price. |
| `odoo_einkaufspreis_aendern` | Changes one stored supplier price. |
| `odoo_einkaufspreise_aendern` | Changes several supplier prices on one card. |

A changed or new supplier price also becomes the article's cost when that
supplier is the article's main one (first by Odoo's sequence). Sales prices are
not written at all: the pricelist "Verkauf (Kosten + Aufschlag)" derives them
from the cost with a markup per product category.
| `odoo_verkaufspreis_aendern` | Changes an article's list price. Existing quotes keep theirs. |

A prepare tool returns a pending approval, which agent-hub turns into a Teams
card. The human can correct any field on the card, then approving calls
`/internal/approvals/{id}/approve`, a route the LLM cannot reach. agent-hub
then resumes the conversation and the agent calls the `_execute` tool, which
works once per approval. `odoo_freigabe_status` shows where a request stands.

File contents are kept apart from the reviewable fields, so the card shows name
and size only. Every upload is verified against Odoo's checksum afterwards.

## AB1000 tools

- `ab1000_preview` — preview a box configuration.
- `ab1000_bom_xlsx` — generate a bill-of-materials Excel file (saved locally,
  path returned).
- `ab1000_kunde` — create/find a SevDesk customer, returns `contact_id`.
- `ab1000_angebot_konfiguration` — generate a quote PDF and SevDesk draft for
  a box configuration (requires `sevdesk_contact_id` from `ab1000_kunde`).

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env  # fill in the TER BOX and Odoo settings
```

## Run

```bash
terbox-mcp
```

Or point an MCP client (Claude Desktop, Claude Code, etc.) at it directly,
e.g. in Claude Desktop's config:

```json
{
  "mcpServers": {
    "terbox-erp": {
      "command": "/absolute/path/to/.venv/bin/terbox-mcp",
      "env": {
        "TERBOX_API_BASE_URL": "https://api.example.com",
        "TERBOX_API_KEY": "your-api-key-here"
      }
    }
  }
}
```

## Deployment (Cloud Run)

Runs as an HTTP server (`MCP_TRANSPORT=streamable-http`) exposing the MCP
endpoint at `/mcp`, listening on `$PORT` — matching the `agent-hub` /
`email-mcp-server` setup in the same GCP project. `Dockerfile` builds the
image; `cloudbuild.yaml` builds, pushes to Artifact Registry and deploys to
Cloud Run via a Cloud Build trigger on pushes to `main`.

Needs, once, in the GCP project:

```bash
gcloud storage buckets create gs://$PROJECT_ID-terbox-erp-mcp-var --location=europe-west3
gcloud storage buckets add-iam-policy-binding gs://$PROJECT_ID-terbox-erp-mcp-var \
  --member=serviceAccount:terbox-erp-mcp-run@$PROJECT_ID.iam.gserviceaccount.com --role=roles/storage.objectUser
printf '%s' "$ODOO_API_KEY" | gcloud secrets create terbox-erp-mcp-odoo-api-key --data-file=-
gcloud secrets add-iam-policy-binding terbox-erp-mcp-odoo-api-key \
  --member=serviceAccount:terbox-erp-mcp-run@$PROJECT_ID.iam.gserviceaccount.com --role=roles/secretmanager.secretAccessor
```

Odoo caps API key lifetime at 3 months, so the key needs rotating before expiry.
The key currently stored has broad rights; replace it with a key of a dedicated,
minimal Odoo user.

Locally:

```bash
docker build -t terbox-erp-mcp .
docker run -p 8080:8080 \
  -e MCP_TRANSPORT=streamable-http \
  -e TERBOX_API_BASE_URL=https://api.example.com \
  -e TERBOX_API_KEY=your-api-key-here \
  terbox-erp-mcp
```

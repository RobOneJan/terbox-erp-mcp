# terbox-erp-mcp

MCP server exposing the TER BOX CAD Backend API (AB1000 box configuration,
customer creation and quoting) as agent tools. Intended to later be driven
from a Telegram bot and a scheduler.

## Tools

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
cp .env.example .env  # fill in TERBOX_API_BASE_URL and TERBOX_API_KEY
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

Locally:

```bash
docker build -t terbox-erp-mcp .
docker run -p 8080:8080 \
  -e MCP_TRANSPORT=streamable-http \
  -e TERBOX_API_BASE_URL=https://api.example.com \
  -e TERBOX_API_KEY=your-api-key-here \
  terbox-erp-mcp
```

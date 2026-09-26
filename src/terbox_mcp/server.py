"""MCP server exposing the TER BOX CAD Backend API as agent tools."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

from terbox_mcp.client import TerboxApiError, TerboxClient
from terbox_mcp.models import BoxAngebotRequest, BoxConfig, KundeRequest, KundeResponse

load_dotenv()

mcp = MCPServer(
    "terbox-erp",
    instructions=(
        "Tools for configuring and quoting TER BOX AB1000 garden boxes via the "
        "TER BOX CAD Backend. Typical flow: preview a config with ab1000_preview, "
        "create the customer with ab1000_kunde to get a sevdesk_contact_id, then "
        "create the quote with ab1000_angebot_konfiguration."
    ),
)


def _client() -> TerboxClient:
    return TerboxClient()


@mcp.tool()
def ab1000_preview(config: BoxConfig) -> dict:
    """Render a preview of an AB1000 box configuration.

    Returns preview data (e.g. render/model info) for the given box
    configuration without persisting anything in the ERP.
    """
    try:
        return _client().post_json("/ab1000/preview", config.model_dump(exclude_none=True))
    except TerboxApiError as exc:
        return {"error": str(exc), "status_code": exc.status_code, "detail": exc.detail}


@mcp.tool()
def ab1000_bom_xlsx(config: BoxConfig) -> dict:
    """Generate the bill of materials (BOM) for an AB1000 box configuration as an Excel file.

    The resulting .xlsx file is saved to the local downloads directory and its
    path is returned.
    """
    try:
        return _client().post_json("/ab1000/bom-xlsx", config.model_dump(exclude_none=True))
    except TerboxApiError as exc:
        return {"error": str(exc), "status_code": exc.status_code, "detail": exc.detail}


@mcp.tool()
def ab1000_kunde(kunde: KundeRequest) -> KundeResponse | dict:
    """Create or find a customer in SevDesk.

    Returns the SevDesk contact_id and Kundennummer, which are required for
    creating a quote via ab1000_angebot_konfiguration.
    """
    try:
        data = _client().post_json("/ab1000/kunde", kunde.model_dump(exclude_none=True))
        return KundeResponse.model_validate(data)
    except TerboxApiError as exc:
        return {"error": str(exc), "status_code": exc.status_code, "detail": exc.detail}


@mcp.tool()
def ab1000_angebot_konfiguration(request: BoxAngebotRequest) -> dict:
    """Generate a quote PDF for an AB1000 box configuration and create a draft in SevDesk.

    The customer must already exist in SevDesk (call ab1000_kunde first to get
    sevdesk_contact_id). Optional extras (additional line items) can be added.
    """
    try:
        return _client().post_json(
            "/ab1000/angebot-konfiguration",
            request.model_dump(exclude_none=True),
        )
    except TerboxApiError as exc:
        return {"error": str(exc), "status_code": exc.status_code, "detail": exc.detail}


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        # Cloud Run injects PORT and only routes HTTP traffic, so the server
        # must listen on 0.0.0.0:$PORT rather than talk stdio.
        port = int(os.environ.get("PORT", "8080"))
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()

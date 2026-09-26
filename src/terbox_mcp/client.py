"""HTTP client for the TER BOX CAD Backend API."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


class TerboxApiError(RuntimeError):
    """Raised when the TER BOX backend returns an error response."""

    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"TER BOX API error {status_code}: {detail}")


@dataclass
class TerboxSettings:
    base_url: str
    api_key: str
    timeout: float = 60.0
    download_dir: Path = Path("./downloads")

    @classmethod
    def from_env(cls) -> "TerboxSettings":
        base_url = os.environ.get("TERBOX_API_BASE_URL")
        api_key = os.environ.get("TERBOX_API_KEY")
        if not base_url:
            raise RuntimeError("TERBOX_API_BASE_URL is not set")
        if not api_key:
            raise RuntimeError("TERBOX_API_KEY is not set")
        timeout = float(os.environ.get("TERBOX_API_TIMEOUT", "60"))
        download_dir = Path(os.environ.get("TERBOX_DOWNLOAD_DIR", "./downloads"))
        return cls(
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            timeout=timeout,
            download_dir=download_dir,
        )


class TerboxClient:
    """Thin wrapper around httpx for calling the TER BOX CAD Backend."""

    def __init__(self, settings: TerboxSettings | None = None):
        self.settings = settings or TerboxSettings.from_env()

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.settings.api_key}

    def post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        url = f"{self.settings.base_url}{path}"
        with httpx.Client(timeout=self.settings.timeout) as client:
            response = client.post(url, json=payload, headers=self._headers())
        if response.status_code >= 400:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise TerboxApiError(response.status_code, detail)
        return response

    def post_json(self, path: str, payload: dict[str, Any]) -> Any:
        response = self.post(path, payload)
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return self._save_binary(response, path)

    def _save_binary(self, response: httpx.Response, path: str) -> dict[str, Any]:
        self.settings.download_dir.mkdir(parents=True, exist_ok=True)
        filename = self._filename_from_response(response, path)
        file_path = self.settings.download_dir / filename
        file_path.write_bytes(response.content)
        return {
            "saved_to": str(file_path.resolve()),
            "content_type": response.headers.get("content-type"),
            "size_bytes": len(response.content),
        }

    @staticmethod
    def _filename_from_response(response: httpx.Response, path: str) -> str:
        disposition = response.headers.get("content-disposition", "")
        marker = "filename="
        if marker in disposition:
            name = disposition.split(marker, 1)[1].strip('"; ')
            if name:
                return name
        slug = path.strip("/").replace("/", "_") or "download"
        return f"{slug}.bin"

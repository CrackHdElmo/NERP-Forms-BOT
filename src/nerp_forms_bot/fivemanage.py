"""Optional FiveManage CDN uploads for player-facing FiveM assets."""

from __future__ import annotations

import json
from dataclasses import dataclass

import requests

FIVEMANAGE_UPLOAD_URL = "https://api.fivemanage.com/api/v3/file"


@dataclass(frozen=True)
class FiveManageUpload:
    """The public CDN address returned by FiveManage after a successful upload."""

    url: str
    original_url: str | None


class FiveManageService:
    """Upload approved player-facing media without exposing tokens to Discord users."""

    def __init__(self, api_token: str, storage_path: str) -> None:
        self.api_token = api_token
        self.storage_path = storage_path.strip("/")

    def upload_png(self, *, filename: str, content: bytes, request_id: str) -> FiveManageUpload:
        """Store a PNG and return the CDN link supplied by FiveManage's V3 API."""
        response = requests.post(
            FIVEMANAGE_UPLOAD_URL,
            headers={"Authorization": self.api_token},
            files={"file": (filename, content, "image/png")},
            data={
                "filename": filename,
                "path": self.storage_path,
                "metadata": json.dumps({"requestId": request_id, "workflow": "court_order"}),
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", {})
        url = data.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("FiveManage returned a successful response without a CDN URL.")
        original_url = data.get("originalUrl")
        return FiveManageUpload(
            url=url,
            original_url=original_url if isinstance(original_url, str) else None,
        )

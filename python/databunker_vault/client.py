"""The Databunker Pro calls the vault store needs, on top of the official SDK (databunkerpro)."""
from __future__ import annotations

import base64
import os
from typing import Any, Dict, List, Optional

from databunkerpro import DatabunkerproAPI


class DatabunkerError(Exception):
    """An API error; status is the HTTP status, or 0 when the server was not reached."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Databunker error {status}: {message}")
        self.status = status
        self.message = message


def _ok(result: Dict[str, Any]) -> Dict[str, Any]:
    if result.get("status") == "error":
        raise DatabunkerError(result.get("httpcode", 0), result.get("message", "unknown error"))
    return result


class DatabunkerClient:
    def __init__(self, url: Optional[str] = None, token: Optional[str] = None, *, tenant: str = "", api: Optional[DatabunkerproAPI] = None) -> None:
        self.api = api or DatabunkerproAPI(
            url or os.environ.get("DATABUNKER_URL", "http://localhost:3000"),
            token or os.environ.get("DATABUNKER_TOKEN", "DEMO"),
            tenant or os.environ.get("DATABUNKER_TENANT", ""),
        )
        self.url = self.api.base_url

    # Users

    def user_get(self, mode: str, identity: str) -> Dict[str, Any]:
        return _ok(self.api.get_user(mode, identity))

    def user_create(self, profile: Dict[str, Any]) -> str:
        return _ok(self.api.create_user(profile))["token"]

    # Tokens

    def token_create_bulk(self, values: List[str], *, unique: bool = True, slidingtime: Optional[str] = None) -> Dict[str, str]:
        """value -> token uuid. With unique, a value already stored keeps its token and its expiry restarts."""
        options: Dict[str, Any] = {"unique": unique}
        if slidingtime:
            options["slidingtime"] = slidingtime
        resp = _ok(self.api.create_tokens_bulk([{"tokentype": "string", "record": v} for v in values], options))
        if (resp.get("summary") or {}).get("errors"):
            raise DatabunkerError(500, f"TokenCreateBulk: {resp['summary']['errors']} records failed")
        return {r["record"]: r["tokenuuid"] for r in (resp.get("created") or []) + (resp.get("duplicates") or [])}

    def tokens_get(self, tokens: List[str]) -> Dict[str, str]:
        """token uuid -> value; expired or unknown tokens are left out. Reading a token extends its sliding expiry."""
        if not tokens:
            return {}
        unlock = _ok(self.api.bulk_list_unlock())["unlockuuid"]
        rows = _ok(self.api.bulk_list_tokens(unlock, list(tokens))).get("rows") or []
        return {r["tokenuuid"]: r["record"] for r in rows}

    # Files (content travels base64-encoded; Databunker encrypts it at rest)

    def file_create(self, user_token: str, filename: str, data: bytes, *, tags=(), slidingtime: Optional[str] = None) -> Dict[str, Any]:
        options: Dict[str, Any] = {"mimetype": "application/json", "tags": list(tags)}
        if slidingtime:
            options["slidingtime"] = slidingtime
        return _ok(self.api.create_file("token", user_token, filename, base64.b64encode(data).decode(), options))

    def file_get(self, user_token: str, filename: str) -> Dict[str, Any]:
        """The newest file with this name; filedata is decoded to bytes."""
        resp = _ok(self.api.get_file("token", user_token, filename=filename))
        resp["filedata"] = base64.b64decode(resp["filedata"])
        return resp

    def file_list(self, user_token: str, tag: Optional[str] = None) -> list:
        return _ok(self.api.list_user_files("token", user_token, tag=tag)).get("files") or []

    def file_delete(self, user_token: str, fileuuid: str) -> None:
        _ok(self.api.delete_file("token", user_token, fileuuid))

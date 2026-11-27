"""Minimal Databunker Pro HTTP client: only the calls the vault store needs."""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class DatabunkerError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Databunker error {status}: {message}")
        self.status = status
        self.message = message


class DatabunkerClient:
    def __init__(self, url: Optional[str] = None, token: Optional[str] = None, timeout: float = 10.0) -> None:
        self.url = (url or os.environ.get("DATABUNKER_URL", "http://localhost:3000")).rstrip("/")
        self.token = token or os.environ.get("DATABUNKER_TOKEN", "DEMO")
        self.timeout = timeout

    def call(self, op: str, body: Dict[str, Any]) -> Dict[str, Any]:
        req = urllib.request.Request(
            f"{self.url}/v2/{op}",
            data=json.dumps(body).encode(),
            headers={"X-Bunker-Token": self.token, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                message = json.load(e).get("message", e.reason)
            except ValueError:
                message = str(e.reason)
            raise DatabunkerError(e.code, message) from None

    # Users

    def user_get(self, mode: str, identity: str) -> Dict[str, Any]:
        return self.call("UserGet", {"mode": mode, "identity": identity})

    def user_create(self, profile: Dict[str, Any]) -> str:
        return self.call("UserCreate", {"profile": profile})["token"]

    # Tokens

    def token_create_bulk(self, values: List[str], *, unique: bool = True, slidingtime: Optional[str] = None) -> Dict[str, str]:
        """value -> token uuid. With unique, a value already stored keeps its token and its expiry restarts."""
        body: Dict[str, Any] = {"records": [{"tokentype": "string", "record": v} for v in values], "unique": unique}
        if slidingtime:
            body["slidingtime"] = slidingtime
        resp = self.call("TokenCreateBulk", body)
        if (resp.get("summary") or {}).get("errors"):
            raise DatabunkerError(500, f"TokenCreateBulk: {resp['summary']['errors']} records failed")
        return {r["record"]: r["tokenuuid"] for r in (resp.get("created") or []) + (resp.get("duplicates") or [])}

    def tokens_get(self, tokens: List[str]) -> Dict[str, str]:
        """token uuid -> value; expired or unknown tokens are left out. Reading a token extends its sliding expiry."""
        if not tokens:
            return {}
        unlock = self.call("BulkListUnlock", {})["unlockuuid"]
        rows = self.call("BulkListTokens", {"unlockuuid": unlock, "tokens": list(tokens)}).get("rows") or []
        return {r["tokenuuid"]: r["record"] for r in rows}

    # Files (content travels base64-encoded; Databunker encrypts it at rest)

    def file_create(self, user_token: str, filename: str, data: bytes, *, tags=(), slidingtime: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "mode": "token",
            "identity": user_token,
            "filename": filename,
            "filedata": base64.b64encode(data).decode(),
            "mimetype": "application/json",
            "tags": list(tags),
        }
        if slidingtime:
            body["slidingtime"] = slidingtime
        return self.call("FileCreate", body)

    def file_get(self, user_token: str, filename: str) -> Dict[str, Any]:
        """The newest file with this name; filedata is decoded to bytes."""
        resp = self.call("FileGet", {"mode": "token", "identity": user_token, "filename": filename})
        resp["filedata"] = base64.b64decode(resp["filedata"])
        return resp

    def file_list(self, user_token: str) -> list:
        return self.call("FileListUserFiles", {"mode": "token", "identity": user_token}).get("files") or []

    def file_delete(self, user_token: str, fileuuid: str) -> None:
        self.call("FileDelete", {"mode": "token", "identity": user_token, "fileuuid": fileuuid})

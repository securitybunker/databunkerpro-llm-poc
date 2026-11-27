"""Keeps a newmean-pii Vault in Databunker Pro.

Every value becomes a Databunker token (TokenCreateBulk, unique). A session file holds only the session's
placeholders and their token uuids ([PERSON_1] -> 7f3a...), so it carries no PII.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from typing import Dict, List, Optional, Tuple

import newmean_pii
from newmean_pii import Vault

from .client import DatabunkerClient, DatabunkerError

FORMAT = 2
TAG = "pii-vault"

_users: Dict[Tuple[str, str], str] = {}
_users_lock = threading.Lock()


def service_user_token(client: DatabunkerClient, login: str) -> str:
    """Token of the service user that owns the session files; created on first use."""
    key = (client.url, login)
    with _users_lock:
        if key in _users:
            return _users[key]
        try:
            token = client.user_get("login", login)["token"]
        except DatabunkerError as e:
            if e.status != 404:
                raise
            try:
                token = client.user_create({"login": login})
            except DatabunkerError as e2:
                if e2.status != 409:  # another process created it first
                    raise
                token = client.user_get("login", login)["token"]
        _users[key] = token
        return token


class VaultFormatError(Exception):
    pass


class DatabunkerVaultStore:
    def __init__(
        self,
        client: DatabunkerClient,
        session_id: str,
        *,
        app: str = "default",
        slidingtime: str = "30d",
        file_slidingtime: Optional[str] = None,
    ) -> None:
        if not session_id:
            raise ValueError("session_id is required")
        self.client = client
        self.app = re.sub(r"[^a-z0-9-]", "-", app.lower()) or "default"
        self.session_hash = hashlib.sha256(session_id.encode()).hexdigest()
        self.filename = f"vault-{self.app}-{self.session_hash}.json"
        self.slidingtime = slidingtime
        self.file_slidingtime = file_slidingtime or slidingtime
        #: placeholders whose token had expired or was deleted at the last load
        self.expired: List[str] = []
        self._tokens: Dict[str, str] = {}  # placeholder -> token uuid
        self._fileuuid: Optional[str] = None
        self._saved: Optional[bytes] = None

    @property
    def user_token(self) -> str:
        return service_user_token(self.client, f"pii-vault-{self.app}")

    def load(self) -> Vault:
        """The session's vault (values fetched by token, which extends their life), or an empty one."""
        self.expired, self._tokens = [], {}
        try:
            resp = self.client.file_get(self.user_token, self.filename)
        except DatabunkerError as e:
            if e.status != 404:
                raise
            self._fileuuid, self._saved = None, None
            return Vault()
        doc = json.loads(resp["filedata"])
        if doc.get("format") != FORMAT:
            raise VaultFormatError(f"unsupported session file format {doc.get('format')!r}")
        if doc.get("session") != self.session_hash:
            raise VaultFormatError("session file belongs to another session")
        self._fileuuid, self._saved = resp["fileuuid"], resp["filedata"]

        values = self.client.tokens_get([e["token"] for e in doc["entries"]])
        entries = []
        for e in doc["entries"]:
            if e["token"] in values:
                entries.append({"tag": e["tag"], "label": e["label"], "value": values[e["token"]]})
                self._tokens[e["tag"]] = e["token"]
            else:
                self.expired.append(e["tag"])
        # counters keep the numbers of expired values, so a placeholder is never reused for another value
        return Vault({"entries": entries, "counters": doc.get("counters", {}), "local": [t for t in doc.get("local", []) if t in self._tokens]})

    def save(self, vault: Vault) -> None:
        """Tokenizes new values, then writes the session file (new file, previous one removed)."""
        state = vault.to_json()
        new = [e for e in state["entries"] if e["tag"] not in self._tokens]
        if new:
            minted = self.client.token_create_bulk([e["value"] for e in new], unique=True, slidingtime=self.slidingtime)
            for e in new:
                self._tokens[e["tag"]] = minted[e["value"]]
        data = self._encode(state)
        if data == self._saved:
            return
        resp = self.client.file_create(self.user_token, self.filename, data, tags=[TAG], slidingtime=self.file_slidingtime)
        new_uuid = resp["fileuuid"]
        # Databunker dedupes identical content and returns the existing file: never delete that one.
        if self._fileuuid and self._fileuuid != new_uuid:
            try:
                self.client.file_delete(self.user_token, self._fileuuid)
            except DatabunkerError as e:
                if e.status != 404:
                    raise
        self._fileuuid, self._saved = new_uuid, data

    def delete(self) -> int:
        """Removes the session file (every copy). Tokens stay: other sessions may share them, and they expire on their own."""
        token = self.user_token
        n = 0
        for f in self.client.file_list(token, tag=TAG):
            if f.get("filename") == self.filename:
                self.client.file_delete(token, f["fileuuid"])
                n += 1
        self._fileuuid, self._saved, self._tokens = None, None, {}
        return n

    def _encode(self, state: dict) -> bytes:
        # Replays are dropped: their keys hold restored (plaintext) text. The session hash keeps content unique
        # per session, so Databunker's file dedupe never merges two sessions.
        doc = {
            "format": FORMAT,
            "newmean": newmean_pii.__version__,
            "session": self.session_hash,
            "entries": [{"tag": e["tag"], "label": e["label"], "token": self._tokens[e["tag"]]} for e in state["entries"]],
            "counters": state.get("counters", {}),
            "local": state.get("local", []),
        }
        return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()

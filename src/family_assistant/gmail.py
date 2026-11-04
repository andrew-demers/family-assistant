"""Read-only Gmail access, one OAuth token per account.

REST endpoints (developers.google.com/workspace/gmail/api/reference/rest/v1):
  GET /gmail/v1/users/me/messages?q=&maxResults=   -> {messages: [{id, threadId}], nextPageToken}
  GET /gmail/v1/users/me/messages/{id}?format=full -> Message with payload (MessagePart tree)
MessagePartBody.data is base64url encoded.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
import re
from dataclasses import dataclass
from email.utils import parseaddr
from pathlib import Path

import httpx
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

log = logging.getLogger(__name__)

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_BODY_CHARS = 20000
_LABEL_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


class GmailError(Exception):
    pass


class GmailAuthError(GmailError):
    """The token is missing, revoked or expired; the user must re-run gmail-auth."""


@dataclass(frozen=True)
class Email:
    account: str
    message_id: str
    subject: str
    sender: str
    date: str
    body: str
    truncated: bool = False


def token_path(config_dir: Path, label: str) -> Path:
    if not _LABEL_RE.match(label):
        raise ValueError("account label must be 1-32 chars: lowercase letters, digits, - or _")
    return config_dir / "google" / f"{label}.json"


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def authorize_account(config_dir: Path, label: str, client_id: str, client_secret: str) -> Path:
    """One-time interactive consent in a local browser; writes the token file."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config(
        {"installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }},
        scopes=[GMAIL_SCOPE],
    )
    creds = flow.run_local_server(port=0, prompt="consent")
    if not creds.refresh_token:
        raise GmailAuthError("Google did not return a refresh token")
    path = token_path(config_dir, label)
    _write_private(path, creds.to_json())
    return path


# ------------------------------------------------------------------- message parsing


def _decode(data: str | None) -> str:
    if not data:
        return ""
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return raw.decode("utf-8", errors="replace")


def _html_to_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", markup)
    text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _collect(part: dict, plain: list[str], rich: list[str]) -> None:
    mime = (part.get("mimeType") or "").lower()
    if part.get("filename"):  # attachments are not read
        return
    if mime == "text/plain":
        plain.append(_decode(part.get("body", {}).get("data")))
    elif mime == "text/html":
        rich.append(_html_to_text(_decode(part.get("body", {}).get("data"))))
    for child in part.get("parts") or []:
        _collect(child, plain, rich)


def parse_message(account: str, message: dict) -> Email:
    payload = message.get("payload") or {}
    headers = {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers") or []}
    plain: list[str] = []
    rich: list[str] = []
    _collect(payload, plain, rich)
    body = "\n\n".join(p for p in plain if p.strip()) or "\n\n".join(r for r in rich if r.strip())
    body = body or message.get("snippet", "")
    truncated = len(body) > MAX_BODY_CHARS
    name, addr = parseaddr(headers.get("from", ""))
    return Email(
        account=account,
        message_id=message["id"],
        subject=headers.get("subject", "(no subject)"),
        sender=f"{name} <{addr}>" if name and addr else (addr or headers.get("from", "")),
        date=headers.get("date", ""),
        body=body[:MAX_BODY_CHARS],
        truncated=truncated,
    )


# ------------------------------------------------------------------------- client


class GmailAccount:
    def __init__(self, label: str, path: Path, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.label = label
        self._path = path
        self._creds: Credentials | None = None
        self._http = httpx.AsyncClient(base_url=API_BASE, timeout=20.0, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _load(self) -> Credentials:
        if self._creds is None:
            if not self._path.is_file():
                raise GmailAuthError(f"no Gmail token for '{self.label}' (expected {self._path.name}); run gmail-auth")
            info = json.loads(self._path.read_text(encoding="utf-8"))
            self._creds = Credentials.from_authorized_user_info(info, scopes=[GMAIL_SCOPE])
        return self._creds

    async def _token(self) -> str:
        creds = self._load()
        if not creds.valid:
            try:
                await asyncio.to_thread(creds.refresh, Request())
            except RefreshError as exc:
                raise GmailAuthError(
                    f"Gmail token for '{self.label}' was rejected by Google; run gmail-auth again"
                ) from exc
            _write_private(self._path, creds.to_json())
        return creds.token

    async def _get(self, path: str, params: dict) -> dict:
        token = await self._token()
        try:
            response = await self._http.get(path, params=params, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            raise GmailError(f"could not reach Gmail ({exc.__class__.__name__})") from exc
        if response.status_code == 401:
            self._creds = None
            raise GmailAuthError(f"Gmail rejected the token for '{self.label}'; run gmail-auth again")
        if response.status_code >= 400:
            raise GmailError(f"Gmail HTTP {response.status_code}")
        return response.json()

    async def list_message_ids(self, query: str, limit: int) -> list[str]:
        data = await self._get("/messages", {"q": query, "maxResults": limit})
        return [m["id"] for m in data.get("messages") or []]

    async def get_email(self, message_id: str) -> Email:
        return parse_message(self.label, await self._get(f"/messages/{message_id}", {"format": "full"}))

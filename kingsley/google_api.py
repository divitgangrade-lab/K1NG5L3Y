"""Google integration: OAuth, Gmail read access, Calendar writes.

Design notes:
* Tokens live in the local SQLite kv table, never in the repo.
* Nothing here runs until GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET exist, and
  every entry point raises GoogleError with a setup hint instead of crashing at
  import time.
* Only scopes Kingsley actually needs are requested: read Gmail, manage
  calendar events, read the account email.
"""

from __future__ import annotations

import base64
import re
import time
import urllib.parse
from typing import Any

import httpx

from kingsley import config, store

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
_GMAIL = "https://gmail.googleapis.com/gmail/v1"
_CALENDAR = "https://www.googleapis.com/calendar/v3"
_HTML_TAG = re.compile(r"<[^>]+>")


class GoogleError(RuntimeError):
    """Google integration is not configured or the call failed."""


def credentials_configured() -> bool:
    client_id, client_secret = config.google_credentials()
    return bool(client_id and client_secret)


def redirect_uri() -> str:
    return f"{config.public_url()}/api/google/callback"


def auth_url(state: str = "kingsley") -> str:
    client_id, _ = config.google_credentials()
    if not client_id:
        raise GoogleError(
            "GOOGLE_CLIENT_ID is not set. Create an OAuth client (type: Web application) in "
            "Google Cloud Console, add " + redirect_uri() + " as an authorized redirect URI, "
            "then export GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET."
        )
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(config.GOOGLE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{_AUTH_URL}?{urllib.parse.urlencode(params)}"


async def exchange_code(code: str) -> dict[str, Any]:
    client_id, client_secret = config.google_credentials()
    if not (client_id and client_secret):
        raise GoogleError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set.")
    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri(),
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(_TOKEN_URL, data=payload)
        if response.status_code >= 400:
            raise GoogleError(f"token exchange failed ({response.status_code}): {response.text[:300]}")
        tokens = response.json()
        email = ""
        try:
            info = await client.get(
                _USERINFO_URL, headers={"Authorization": f"Bearer {tokens['access_token']}"}
            )
            if info.status_code < 400:
                email = info.json().get("email", "")
        except httpx.HTTPError:
            pass
    stored = {
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "expires_at": time.time() + float(tokens.get("expires_in", 3600)) - 60,
        "scope": tokens.get("scope", ""),
        "email": email,
    }
    store.kv_set("google_tokens", stored)
    store.add_event(f"Google connected{f' as {email}' if email else ''}", source="google")
    return {"email": email, "scope": stored["scope"]}


async def access_token() -> str:
    tokens = store.kv_get("google_tokens") or {}
    token = tokens.get("access_token")
    if not token:
        raise GoogleError("Google is not connected yet. Open /api/google/connect to authorize.")
    if time.time() < float(tokens.get("expires_at", 0)):
        return str(token)
    refresh = tokens.get("refresh_token")
    client_id, client_secret = config.google_credentials()
    if not refresh or not (client_id and client_secret):
        raise GoogleError("Google token expired and cannot be refreshed; reconnect the account.")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            _TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
        )
    if response.status_code >= 400:
        raise GoogleError(f"token refresh failed: {response.text[:200]}")
    fresh = response.json()
    tokens["access_token"] = fresh["access_token"]
    tokens["expires_at"] = time.time() + float(fresh.get("expires_in", 3600)) - 60
    store.kv_set("google_tokens", tokens)
    return str(tokens["access_token"])


async def status() -> dict[str, Any]:
    tokens = store.kv_get("google_tokens") or {}
    return {
        "credentials_configured": credentials_configured(),
        "connected": bool(tokens.get("access_token")),
        "email": tokens.get("email", ""),
        "redirect_uri": redirect_uri(),
        "scopes": list(config.GOOGLE_SCOPES),
    }


def _decode_body(payload: dict[str, Any]) -> str:
    """Walk a Gmail MIME tree and return readable text."""
    if payload.get("mimeType", "").startswith("text/plain"):
        data = payload.get("body", {}).get("data")
        if data:
            try:
                return base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace")
            except Exception:
                pass
    for part in payload.get("parts", []) or []:
        text = _decode_body(part)
        if text:
            return text
    return ""


async def gmail_message(message_id: str) -> dict[str, Any]:
    token = await access_token()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{_GMAIL}/users/me/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"format": "full"},
        )
    if response.status_code >= 400:
        raise GoogleError(f"gmail get failed ({response.status_code}): {response.text[:200]}")
    raw = response.json()
    payload = raw.get("payload", {})
    headers = {item.get("name", "").lower(): item.get("value", "") for item in payload.get("headers", [])}
    body = _decode_body(payload) or raw.get("snippet", "")
    body = _HTML_TAG.sub(" ", body)
    return {
        "id": message_id,
        "thread_id": raw.get("threadId", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", "(no subject)"),
        "date": headers.get("date", ""),
        "snippet": raw.get("snippet", ""),
        "labels": raw.get("labelIds", []),
        "body": re.sub(r"\s+", " ", body).strip()[:4000],
    }


async def gmail_recent(query: str = "is:unread newer_than:14d", limit: int = 12) -> list[dict[str, Any]]:
    token = await access_token()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{_GMAIL}/users/me/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": query, "maxResults": str(limit)},
        )
        if response.status_code >= 400:
            raise GoogleError(f"gmail list failed ({response.status_code}): {response.text[:200]}")
        ids = [item["id"] for item in response.json().get("messages", [])]
    messages: list[dict[str, Any]] = []
    for message_id in ids:
        try:
            messages.append(await gmail_message(message_id))
        except GoogleError:
            continue
    return messages


async def calendar_insert(
    summary: str,
    *,
    description: str = "",
    start_iso: str,
    end_iso: str,
    location: str = "",
) -> dict[str, Any]:
    token = await access_token()
    body = {
        "summary": summary[:500],
        "description": description[:4000],
        "start": {"dateTime": start_iso, "timeZone": "UTC"},
        "end": {"dateTime": end_iso, "timeZone": "UTC"},
    }
    if location:
        body["location"] = location
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{_CALENDAR}/calendars/primary/events",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
        )
    if response.status_code >= 400:
        raise GoogleError(f"calendar insert failed ({response.status_code}): {response.text[:300]}")
    data = response.json()
    return {
        "id": data.get("id", ""),
        "html_link": data.get("htmlLink", ""),
        "summary": data.get("summary", summary),
        "start": (data.get("start") or {}).get("dateTime", start_iso),
    }


async def calendar_upcoming(limit: int = 10) -> list[dict[str, Any]]:
    token = await access_token()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{_CALENDAR}/calendars/primary/events",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "timeMin": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "maxResults": str(limit),
                "singleEvents": "true",
                "orderBy": "startTime",
            },
        )
    if response.status_code >= 400:
        raise GoogleError(f"calendar list failed ({response.status_code}): {response.text[:200]}")
    events = []
    for item in response.json().get("items", []):
        start = item.get("start", {})
        events.append(
            {
                "id": item.get("id", ""),
                "summary": item.get("summary", "(untitled)"),
                "start": start.get("dateTime") or start.get("date", ""),
                "link": item.get("htmlLink", ""),
            }
        )
    return events

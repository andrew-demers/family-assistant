"""Thin async client for the Yuvomi REST API (/api/v1).

Endpoints and shapes come from Yuvomi's OpenAPI spec (server/openapi, v2.84.0).
Every response is wrapped as {"data": ...}; errors as {"error", "code", "reason"?}.
"""

from __future__ import annotations

from typing import Any

import httpx


class YuvomiError(Exception):
    def __init__(self, message: str, status: int | None = None, reason: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason

    def __str__(self) -> str:
        prefix = f"HTTP {self.status}: " if self.status else ""
        return f"{prefix}{self.args[0]}"


class YuvomiClient:
    def __init__(self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/api/v1",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=15.0,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise YuvomiError(f"could not reach Yuvomi ({exc.__class__.__name__})") from exc
        if response.status_code >= 400:
            try:
                body = response.json()
                message = body.get("error") or response.reason_phrase
                reason = body.get("reason")
            except ValueError:
                message, reason = response.reason_phrase, None
            raise YuvomiError(message, status=response.status_code, reason=reason)
        try:
            body = response.json()
        except ValueError as exc:
            raise YuvomiError("Yuvomi returned a non-JSON response", status=response.status_code) from exc
        if not isinstance(body, dict) or "data" not in body:
            raise YuvomiError("Yuvomi response had no 'data' field", status=response.status_code)
        return body["data"]

    # --- Shopping ---
    async def list_shopping_lists(self) -> list[dict]:
        return await self._request("GET", "/shopping")

    async def list_shopping_items(self, list_id: int) -> list[dict]:
        return await self._request("GET", f"/shopping/{list_id}/items")

    async def add_shopping_item(self, list_id: int, name: str, quantity: str | None = None) -> dict:
        payload: dict[str, Any] = {"name": name}
        if quantity:
            payload["quantity"] = quantity
        return await self._request("POST", f"/shopping/{list_id}/items", json=payload)

    # --- Tasks ---
    async def list_tasks(self, status: str | None = None) -> list[dict]:
        params = {"status": status} if status else None
        return await self._request("GET", "/tasks", params=params)

    async def create_task(self, payload: dict[str, Any]) -> dict:
        return await self._request("POST", "/tasks", json=payload)

    async def get_task(self, task_id: int) -> dict:
        return await self._request("GET", f"/tasks/{task_id}")

    # --- Calendar ---
    async def list_events(self, date_from: str, date_to: str) -> list[dict]:
        return await self._request("GET", "/calendar", params={"from": date_from, "to": date_to})

    async def create_event(self, payload: dict[str, Any]) -> dict:
        return await self._request("POST", "/calendar", json=payload)

    async def get_event(self, event_id: int) -> dict:
        return await self._request("GET", f"/calendar/{event_id}")

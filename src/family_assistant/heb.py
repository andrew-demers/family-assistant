"""Client for texas-grocery-mcp (stdio MCP server), spawned per use.

Tool names and result shapes are from texas-grocery-mcp 0.1.3 (tools/*.py). Results arrive as
structured content (a plain dict). Notable facts from that source:
- There is no checkout, payment, order or pickup/delivery-slot tool.
- Cart writes take confirm=False (preview) / confirm=True (execute).
- HEB_DEFAULT_STORE is read into settings but never applied, so the store is set with store_change.
- cart_get reports price 0.0 when it cannot find a price.
- Login with saved credentials opens a visible (headed) browser; in Docker that needs Xvfb.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger(__name__)


class HebError(Exception):
    pass


def heb_state_dir(config_dir: Path) -> Path:
    return config_dir / "heb"


class HebMcp:
    """Async context manager: starts the server, exposes call(), stops it on exit."""

    def __init__(
        self, command: str, config_dir: Path, store_id: str | None = None, args: list[str] | None = None,
    ) -> None:
        self._command = command
        self._args = list(args or [])
        state = heb_state_dir(config_dir)
        self._env = {
            # Minimal environment: the server must not see our API keys or tokens.
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", str(state)),
            "AUTH_STATE_PATH": str(state / "auth.json"),
            "LOG_LEVEL": "WARNING",
        }
        for key in ("DISPLAY", "SYSTEMROOT", "PLAYWRIGHT_BROWSERS_PATH", "TZ"):
            if os.environ.get(key):
                self._env[key] = os.environ[key]
        if store_id:
            self._env["HEB_DEFAULT_STORE"] = store_id
        self._state = state
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> HebMcp:
        self._state.mkdir(parents=True, exist_ok=True)
        self._stack = AsyncExitStack()
        try:
            # The server's banner and logs go to a file, not the terminal or the bot's log.
            errlog = self._stack.enter_context(open(self._state / "server.log", "a", encoding="utf-8"))
            read, write = await self._stack.enter_async_context(
                stdio_client(
                    StdioServerParameters(command=self._command, args=self._args, env=self._env), errlog=errlog,
                )
            )
            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
        except BaseException as exc:
            await self._stack.aclose()
            raise HebError(f"could not start the HEB tool ({exc.__class__.__name__})") from exc
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._stack is not None:
            await self._stack.aclose()

    async def call(self, name: str, args: dict[str, Any] | None = None) -> dict:
        if self._session is None:
            raise HebError("HEB tool is not running")
        try:
            result = await self._session.call_tool(name, args or {})
        except Exception as exc:
            raise HebError(f"HEB tool {name} failed ({exc.__class__.__name__})") from exc
        data = result.structured_content
        if data is None and result.content and getattr(result.content[0], "type", None) == "text":
            try:
                data = json.loads(result.content[0].text)
            except json.JSONDecodeError:
                data = {"message": result.content[0].text}
        if result.is_error:
            message = (data or {}).get("message") if isinstance(data, dict) else None
            raise HebError(f"HEB tool {name} returned an error: {message or 'no details'}")
        if not isinstance(data, dict):
            raise HebError(f"HEB tool {name} returned an unexpected result")
        return data

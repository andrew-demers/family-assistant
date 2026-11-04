"""Stand-in stdio MCP server for HebMcp tests (same tool names as texas-grocery-mcp)."""

import os

from mcp.server import MCPServer

server = MCPServer("fake-heb")


@server.tool()
def cart_check_auth() -> dict:
    return {"authenticated": False, "auth_required": True, "message": "Login required for cart operations"}


@server.tool()
def debug_env() -> dict:
    return {"keys": sorted(os.environ), "auth_state_path": os.environ.get("AUTH_STATE_PATH")}


@server.tool()
def broken() -> dict:
    raise RuntimeError("boom")


if __name__ == "__main__":
    server.run("stdio")

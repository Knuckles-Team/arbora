"""A minimal MCP connector for the real-boot test: one ``echo`` tool over HTTP."""

from __future__ import annotations

import sys

from fastmcp import FastMCP

connector = FastMCP("boot-echo")


@connector.tool(annotations={"readOnlyHint": True})
def echo(text: str) -> str:
    """Return the given text unchanged, prefixed with ``echo:``."""
    return f"echo:{text}"


if __name__ == "__main__":
    connector.run(transport="streamable-http", host="127.0.0.1", port=int(sys.argv[1]))

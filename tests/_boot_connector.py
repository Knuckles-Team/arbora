"""A minimal MCP connector for the real-boot test: one ``echo`` tool over HTTP."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from fastmcp import FastMCP

connector = FastMCP("boot-echo")


@connector.tool(annotations={"readOnlyHint": True})
def echo(text: str) -> str:
    """Return the given text unchanged, prefixed with ``echo:``."""
    if path := os.getenv("BOOT_EFFECT_RECEIPTS"):
        with Path(path).open("a") as receipts:
            receipts.write(json.dumps({"tool": "echo", "text": text}) + "\n")
    return f"echo:{text}"


if __name__ == "__main__":
    connector.run(transport="streamable-http", host="127.0.0.1", port=int(sys.argv[1]))

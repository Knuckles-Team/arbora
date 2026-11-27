"""The ``arbora-sandbox`` entry point: the local engine the sandbox runs on (ARBORA-DEPLOY-R019).

The deployment unit declares the engine it needs (``LocalEngine``); this process composes it
from the engine launcher of the host composition.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from arbora.deployment import sandbox
from arbora.mcp_server import composition
from arbora.mcp_server.served_engine import EngineAttachments

__all__ = ["CompositionEngine", "main"]


class CompositionEngine:
    """``LocalEngine`` over the host composition's local-engine launcher."""

    def __init__(self) -> None:
        self._attachments = EngineAttachments()

    def ensure(self, port: int, persist_dir: Path, extra_args: Sequence[str]) -> Any:
        return composition.ensure_local_engine(
            composition.LocalEngineConfig(
                port=port, persist_dir=persist_dir, extra_args=tuple(extra_args)
            ),
            self._attachments,
        )

    def stand_down(self, address: str) -> None:
        composition.stand_down_engine(
            composition.EngineEndpoint("tcp", address), self._attachments
        )


def main(argv: Sequence[str] | None = None) -> int:
    return sandbox.main(argv, CompositionEngine())


if __name__ == "__main__":
    raise SystemExit(main())

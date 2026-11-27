"""The ``arbora-production-ops`` entry point: the host the one-shot operations run under.

The deployment unit declares what a one-shot operation needs (``OneShotHost``); this process
is its own composition root, so it mints the verified process session, owns its engine and
passes both down (ARBORA-HOST-R038.2).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from arbora.core.settings import RuntimeConfig
from arbora.deployment import production_ops
from arbora.security.key_cache import JwksCache

__all__ = ["ProcessHost", "main"]


class ProcessHost:
    """The process session and engine of one one-shot command."""

    @contextmanager
    def open(self) -> Iterator[tuple[Any, Any]]:
        # ``runtime`` binds bootstrap's host slots on import; import it first.
        from arbora.mcp_server import bootstrap, runtime
        from arbora.mcp_server.served_engine import EngineHandles
        from arbora.security.actor_context import use_actor
        from arbora.security.session import use_session

        session = runtime._mint_process_session(
            "http", JwksCache(config := RuntimeConfig()), config
        )
        with use_actor(session.actor), use_session(session):
            engine = EngineHandles().served()
            bootstrap._wait_for_engine_materialization(engine)
            yield session, engine


def main(argv: list[str] | None = None) -> int:
    return production_ops.main(argv, host=ProcessHost())


if __name__ == "__main__":
    raise SystemExit(main())

"""The ``arbora-identity`` composition root: the engine environment of the identity commands.

The identity package never imports a served package (ARBORA-IDENTITY-R021). This module sits
beside the served host, mints Arbora's own process session, opens the engine client and hands
the identity commands their :class:`~arbora.identity.cli.Environment`.
"""

from __future__ import annotations

from arbora.identity.cli import (
    DEFAULT_OVERLAP_SECONDS,
    OVERLAP_RANGE,
    Environment,
    KeyRing,
)
from arbora.identity.cli import main as identity_main


def process_environment() -> Environment:
    """The composition-owned engine client under Arbora's own process session."""
    from arbora.core.settings import RuntimeConfig, config_dir
    from arbora.identity.composition import build_engine_identity_ports
    from arbora.mcp_server import bootstrap, runtime
    from arbora.mcp_server.served_engine import EngineHandles
    from arbora.security.anonymous import session_context
    from arbora.security.key_cache import JwksCache

    # This one-shot CLI is its own composition root and owns its key cache.
    runtime_config = RuntimeConfig()
    session = runtime._mint_process_session(
        "http", JwksCache(runtime_config), runtime_config
    )
    # This one-shot CLI is its own composition root and owns its engine.
    engine = EngineHandles(config=runtime_config).served()
    bootstrap._wait_for_engine_materialization(engine)
    graph = str(session.graph)
    client = engine.graph_compute.for_graph(graph).async_client
    ports = build_engine_identity_ports(client, graph=graph)
    overlap = int(
        getattr(runtime_config, "identity_issuer_key_overlap_seconds", 0) or 0
    )
    low, high = OVERLAP_RANGE
    return Environment(
        admin=ports.admin,
        client=client,
        claims=dict(session_context(session)),
        ring=KeyRing(config_dir() / "identity" / "issuer-signing-keys"),
        overlap_seconds=min(max(overlap or DEFAULT_OVERLAP_SECONDS, low), high),
    )


def main() -> int:
    """Console entry point of ``arbora-identity``."""
    return identity_main(environment=process_environment)

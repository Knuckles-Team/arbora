"""The A2A authority runs agents only through the run port it was given (ARBORA-HOST-R038.3)."""

from __future__ import annotations

import pytest

from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.core.settings import RuntimeConfig
from arbora.mcp_server.host_context import compose_host_context
from arbora.mcp_server.served_engine import EngineHandles
from arbora.run_origin import RunOriginRegistry


def _authority(**kwargs: object) -> WorkItemA2AAuthority:
    return WorkItemA2AAuthority(
        lambda: None, origins=RunOriginRegistry(), config=RuntimeConfig(), **kwargs
    )


@pytest.mark.spec("ARBORA-HOST-R038.3")
def test_authority_never_default_constructs_a_run_port() -> None:
    """Without a composed run port the authority refuses; it builds none of its own."""
    with pytest.raises(RuntimeError, match="not composed"):
        _authority().runs._agent_executor()
    port = object()
    assert _authority(executor=port).runs._agent_executor() is port


@pytest.mark.spec("ARBORA-HOST-R038.3")
def test_the_host_context_hands_one_run_port_to_the_authority() -> None:
    """The host context composes the run port once and the A2A authority runs through it."""
    host = compose_host_context(EngineHandles(config=RuntimeConfig()))
    assert host.runs.a2a.authority.runs._agent_executor() is host.runs.run_port

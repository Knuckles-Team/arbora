"""The engine endpoint type (ARBORA-DEPLOY-R018.1)."""

from __future__ import annotations

import pytest

from arbora.mcp_server.composition import (
    EngineEndpoint,
    EngineEndpointError,
    parse_engine_endpoint,
)


@pytest.mark.spec("ARBORA-DEPLOY-R018.1")
def test_both_forms_parse_and_empty_or_malformed_is_rejected() -> None:
    unix = EngineEndpoint("unix", "/run/engine/medulla.sock")
    assert parse_engine_endpoint("unix:///run/engine/medulla.sock") == unix
    assert parse_engine_endpoint("/run/engine/medulla.sock") == unix
    assert unix.client_kwargs() == {"socket_path": "/run/engine/medulla.sock"}

    tcp = EngineEndpoint("tcp", "127.0.0.1:47980")
    assert parse_engine_endpoint("tcp://127.0.0.1:47980") == tcp
    assert parse_engine_endpoint(" 127.0.0.1:47980 ") == tcp
    assert tcp.client_kwargs() == {"tcp_addr": "127.0.0.1:47980"}
    assert (
        parse_engine_endpoint("engine.example.com:1").address == "engine.example.com:1"
    )
    assert parse_engine_endpoint("localhost:65535").address == "localhost:65535"

    for empty in ("", "   ", None):
        with pytest.raises(EngineEndpointError) as refused:
            parse_engine_endpoint(empty)
        assert refused.value.code == "ENGINE_ENDPOINT_EMPTY"

    for bad in (
        "relative/engine.sock",
        "unix://relative.sock",
        "unix://",
        "localhost",
        "localhost:",
        ":4000",
        "host:abc",
        "host:0",
        "host:65536",
        "host:-1",
        "tls://host:4000",
    ):
        with pytest.raises(EngineEndpointError) as refused:
            parse_engine_endpoint(bad)
        assert refused.value.code == "ENGINE_ENDPOINT_MALFORMED", bad

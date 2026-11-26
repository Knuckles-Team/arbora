"""A configured remote engine skips the local start (ARBORA-DEPLOY-R018.6)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from arbora.core.settings import runtime_config
from arbora.mcp_server import composition, served_engine
from arbora.mcp_server.composition import (
    EngineEndpoint,
    EngineRefusal,
    is_remote_engine,
)


@pytest.fixture(autouse=True)
def _fresh_pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    served_engine.reset_process_engine()
    for name in ("GRAPH_SERVICE_AUTH_SECRET", "MEDULLA_SIGNER_KEYS_JSON"):
        monkeypatch.delenv(name, raising=False)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a configured endpoint must never start a local engine")

    monkeypatch.setattr(composition, "ensure_local_engine", forbidden)
    yield
    served_engine.reset_process_engine()


@pytest.mark.spec("ARBORA-DEPLOY-R018.6")
def test_remote_url_starts_nothing_locally_and_missing_credential_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert is_remote_engine(EngineEndpoint("tcp", "engine.example.com:4000"))
    assert is_remote_engine(EngineEndpoint("tcp", "10.1.2.3:4000"))
    assert not is_remote_engine(EngineEndpoint("tcp", "127.0.0.1:4000"))
    assert not is_remote_engine(EngineEndpoint("tcp", "localhost:4000"))
    assert not is_remote_engine(EngineEndpoint("unix", "/run/engine.sock"))

    # setattr would restore the frozen live value as a permanent override at
    # teardown; setitem drops the key so later reads stay live.
    monkeypatch.setitem(
        runtime_config._overrides,
        "graph_service_endpoints",
        ["tcp://engine.example.com:4000"],
    )
    before = composition.engine_spawn_count()
    with pytest.raises(EngineRefusal) as refused:
        served_engine.process_engine_pool()
    assert refused.value.code == "ENGINE_CREDENTIAL_MISSING"

    monkeypatch.setenv("GRAPH_SERVICE_AUTH_SECRET", "a-credential")  # nosec B105
    pool = served_engine.process_engine_pool()
    assert pool is served_engine.process_engine_pool()
    assert composition.engine_spawn_count() == before

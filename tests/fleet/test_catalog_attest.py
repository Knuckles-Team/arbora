"""ARBORA-FLEET-R041.21: the attest key covers the published server component."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.deployment import semantic_provisioning as sp
from tests._eg_fakes import FakeEgEngine

TENANT = "tenant-a"
GRAPH = "tenant__tenant-a__default"


class _Engine(FakeEgEngine):
    def __init__(self) -> None:
        super().__init__()
        self.component: dict[str, Any] | None = None
        self.keys: list[str] = []

    def _on_catalog_authority_status(self, _params: Any, _key: Any) -> Any:
        return None

    def _on_attest_self_served_catalog(self, params: Any, key: Any) -> Any:
        self.keys.append(key)
        return {
            "configuration_revision": 1,
            "catalog_generation": 1,
            "snapshot_digest": "5" * 64,
            "child_connection_generation": 1,
            "authorization_scope_digest": "4" * 64,
        }


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> _Engine:
    async def page(_commons: Any) -> Any:
        view = SimpleNamespace(name="conn", url="https://x/mcp", resources={})
        return SimpleNamespace(
            entries=[view], registry_revision=3, registry_digest="3" * 64
        )

    box = _Engine()

    async def search(_client: Any, _request: Any, _graph: Any = None) -> Any:
        found = box.component
        entry = SimpleNamespace(
            attributes={"pack.connector": "conn"},
            entry_revision=0 if found is None else found["revision"],
            definition_digest="" if found is None else found["digest"],
        )
        return SimpleNamespace(
            entries=[] if found is None else [entry], next_cursor=None
        )

    monkeypatch.setattr("medulla.generated.storage.send_agent_component_search", search)
    monkeypatch.setattr(sp, "registry_page", page)
    monkeypatch.setattr(sp, "catalog_content_digest", lambda _pack: "a" * 64)
    monkeypatch.setattr(sp, "registration_config_digest", lambda *_a: "b" * 64)
    return box


async def _attest(engine: _Engine) -> str:
    pack = SimpleNamespace(
        connector="conn",
        archive=SimpleNamespace(
            server=SimpleNamespace(body=SimpleNamespace(sha256="d" * 64))
        ),
    )
    await sp.attest_self_served_catalog(
        engine,
        engine,
        tenant_id=TENANT,
        graph=GRAPH,
        pack=pack,
        bind_graph=lambda _g: contextlib.nullcontext(),
    )
    return engine.keys[-1]


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R041.21")
async def test_attest_key_changes_when_the_published_component_moves(
    engine: _Engine,
) -> None:
    engine.component = {"revision": 1, "digest": "1" * 64}
    first = await _attest(engine)
    assert await _attest(engine) == first  # unmoved: the same key replays
    engine.component = {"revision": 2, "digest": "2" * 64}
    moved = await _attest(engine)
    assert moved != first  # moved: a new key, so two attests never conflict
    engine.component = None
    assert await _attest(engine) not in {first, moved}

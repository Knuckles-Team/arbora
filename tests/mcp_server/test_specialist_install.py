"""Specialist package install is transactional per provider root (ARBORA-HOST-R034)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import specialists
from arbora.api.ops.registry_factory import get_registry
from arbora.host.specialists import MARKER_MAX_BYTES, MARKER_NAME
from tests.api._lane_double import EngineDouble, context


def _asset(name: str, body: bytes, *, digest: str | None = None) -> dict[str, str]:
    return {
        "name": name,
        "sha256": digest or hashlib.sha256(body).hexdigest(),
        "content_hex": body.hex(),
    }


def _params(*assets: dict[str, str], version: str = "1.0.0") -> dict[str, Any]:
    return {
        "provider": "acme",
        "package": {
            "name": "pdf-tools",
            "version": version,
            "owner": "team-docs",
            "assets": list(assets),
        },
    }


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import medulla.generated as generated

    monkeypatch.setenv("ARBORA_CONFIG_DIR", str(tmp_path))

    async def send(client, method, params, graph, **kw):
        return await client._send(method, params, graph, **kw)

    monkeypatch.setattr(generated, "send_by_id", send)
    client = EngineDouble(AddTriples={"ok": True})
    return client, context(client, monkeypatch), tmp_path / "specialists" / "acme"


async def _install(ctx: Any, params: dict[str, Any]) -> dict[str, Any]:
    op = specialists.operations()[0]
    return await specialists.handle_specialists_install(ctx, params, op)


@pytest.mark.spec("ARBORA-HOST-R034")
async def test_install_swaps_marker_atomically_and_registers_nodes(served) -> None:
    client, ctx, root = served
    first = await _install(ctx, _params(_asset("a.bin", b"one")))
    marker = root / MARKER_NAME
    assert marker.read_text().strip() == first["value"]["generation"]
    assert (
        root / "generations" / first["value"]["generation"] / "a.bin"
    ).read_bytes() == b"one"
    second = await _install(ctx, _params(_asset("a.bin", b"two"), version="1.1.0"))
    assert second["value"]["previous"] == first["value"]["generation"]
    assert marker.read_text().strip() == second["value"]["generation"]
    # The earlier generation stays immutable beside the new one.
    assert (
        root / "generations" / first["value"]["generation"] / "a.bin"
    ).read_bytes() == b"one"
    assert [p.name for p in root.iterdir() if p.name.startswith(".")] == []
    sent = [row for row in client.sent if row[0] == "AddTriples"]
    assert len(sent) == 2
    body = sent[0][1]["ntriples"]
    assert "SpecialistPackage>" in body and "ProviderAsset>" in body
    assert sent[0][2] == "tenant-content"


@pytest.mark.spec("ARBORA-HOST-R034")
async def test_digest_mismatch_refuses_and_leaves_the_marker(served) -> None:
    client, ctx, root = served
    good = await _install(ctx, _params(_asset("a.bin", b"one")))
    bad = _asset("a.bin", b"tampered", digest=hashlib.sha256(b"other").hexdigest())
    with pytest.raises(OperationRefused) as caught:
        await _install(ctx, _params(bad, version="2.0.0"))
    assert caught.value.code == "ASSET_DIGEST_MISMATCH"
    assert (root / MARKER_NAME).read_text().strip() == good["value"]["generation"]
    assert sorted(p.name for p in (root / "generations").iterdir()) == [
        good["value"]["generation"]
    ]
    assert len([r for r in client.sent if r[0] == "AddTriples"]) == 1


@pytest.mark.spec("ARBORA-HOST-R034")
async def test_unbounded_marker_refuses_before_any_write(served) -> None:
    client, ctx, root = served
    root.mkdir(parents=True)
    (root / MARKER_NAME).write_text("x" * (MARKER_MAX_BYTES + 1))
    with pytest.raises(OperationRefused) as caught:
        await _install(ctx, _params(_asset("a.bin", b"one")))
    assert caught.value.code == "MARKER_UNBOUNDED"
    assert not (root / "generations").exists()
    assert client.sent == []


@pytest.mark.spec("ARBORA-HOST-R034")
async def test_failed_registration_restores_the_previous_marker(
    served, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, ctx, root = served
    good = await _install(ctx, _params(_asset("a.bin", b"one")))
    client.answers["AddTriples"] = RuntimeError("engine down")
    with pytest.raises(RuntimeError):
        await _install(ctx, _params(_asset("a.bin", b"two"), version="1.1.0"))
    assert (root / MARKER_NAME).read_text().strip() == good["value"]["generation"]
    assert [p.name for p in (root / "generations").iterdir()] == [
        good["value"]["generation"]
    ]


@pytest.mark.spec("ARBORA-HOST-R034")
def test_install_operation_is_registered_in_the_served_registry() -> None:
    op = get_registry().get("specialists.install")
    assert op is not None
    assert op.effect.name == "ADMIN"

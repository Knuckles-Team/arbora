"""The engine media store: refusal under the pin, real path against main's shape."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.domain.a2a.media_refs import (
    EngineMediaRefStore,
    MediaRefRefused,
    MediaStoreUnavailable,
)
from arbora.pin_lag import meristem as guard

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
NOW = datetime(2026, 11, 26, tzinfo=UTC)


def _pinned_has_media() -> bool:
    try:
        guard.media_methods()
    except guard.PinLagUnavailable:
        return False
    return True


@pytest.mark.skipif(_pinned_has_media(), reason="the pin carries the media methods")
def test_pinned_engine_without_media_methods_refuses_naming_the_method() -> None:
    store = EngineMediaRefStore(object(), "graph", 3600)
    with pytest.raises(guard.PinLagUnavailable) as record:
        asyncio.run(store.put("t:a", "image/png", PNG, NOW))
    assert record.value.missing == "medulla.generated.METHOD_IDS.MediaPut"
    with pytest.raises(guard.PinLagUnavailable):
        asyncio.run(store.get("t:a", "0" * 64, NOW))


@pytest.mark.pin_lag
def test_real_path_stores_by_digest_and_binds_owner_and_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import medulla.generated as generated
    from medulla.generated import storage

    media: dict[str, bytes] = {}
    kv: dict[tuple[str, str], bytes] = {}

    async def put(client: Any, params: Any, graph: Any = None, **_: Any) -> str:
        digest = hashlib.sha256(params["data"]).hexdigest()
        media[digest] = params["data"]
        return f"sha256:{digest}"

    async def get(client: Any, params: Any, graph: Any = None, **_: Any) -> Any:
        return SimpleNamespace(payload=media[params["digest"].removeprefix("sha256:")])

    async def kv_put(client: Any, params: Any, graph: Any = None, **_: Any) -> Any:
        kv[(params["namespace"], params["key"])] = params["value"]
        return SimpleNamespace(version=1)

    async def kv_get(client: Any, params: Any, graph: Any = None, **_: Any) -> Any:
        return kv.get((params["namespace"], params["key"]))

    monkeypatch.setattr(
        generated, "METHOD_IDS", frozenset({"MediaPut", "MediaGet", "MediaTombstone"})
    )
    for name, fn in {
        "send_media_put": put,
        "send_media_get": get,
        "send_kv_put": kv_put,
        "send_kv_get": kv_get,
    }.items():
        monkeypatch.setattr(storage, name, fn, raising=False)
    monkeypatch.setattr(
        storage,
        "decode_kv_get",
        lambda raw: None if raw is None else SimpleNamespace(value=raw),
    )
    store = EngineMediaRefStore(object(), "graph", 3600)

    async def scenario() -> None:
        receipt = await store.put("t:ann", "image/png", PNG, NOW)
        digest = receipt.digest.removeprefix("sha256:")
        assert receipt.expires_at == NOW + timedelta(seconds=3600)
        assert (await store.get("t:ann", digest, NOW)).data == PNG
        with pytest.raises(MediaRefRefused) as foreign:
            await store.get("t:bob", digest, NOW)
        assert foreign.value.reason == "media_ref_unknown"
        with pytest.raises(MediaRefRefused) as expired:
            await store.get("t:ann", digest, NOW + timedelta(hours=2))
        assert expired.value.reason == "media_ref_expired"
        media[digest] = PNG + b"x"
        with pytest.raises(MediaRefRefused):
            await store.get("t:ann", digest, NOW)

        async def down(*_: Any, **__: Any) -> Any:
            raise RuntimeError("engine down")

        monkeypatch.setattr(storage, "send_media_put", down)
        with pytest.raises(MediaStoreUnavailable):
            await store.put("t:ann", "image/png", PNG + b"y", NOW)

    asyncio.run(scenario())

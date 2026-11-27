"""The JWKS fetch validates the document, caches it in the host key cache and refuses a bad one."""

from __future__ import annotations

import pytest

from arbora.security import auth
from arbora.security.key_cache import JwksCache

URI = "https://issuer.example/jwks"
CREDENTIALED = "https://" + ":".join(("name", "word")) + "@issuer.example/jwks"
DOCUMENT = {"keys": [{"kid": "k1", "kty": "RSA"}]}


def _downloads(monkeypatch: pytest.MonkeyPatch, document: object) -> list[str]:
    calls: list[str] = []

    async def download(uri: str) -> object:
        calls.append(uri)
        return document

    monkeypatch.setattr(auth, "_download_jwks", download)
    return calls


@pytest.mark.asyncio
async def test_a_fetched_key_set_is_cached_in_the_given_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _downloads(monkeypatch, DOCUMENT)
    keys = JwksCache()
    assert await auth.fetch_jwks(keys, URI) == DOCUMENT
    assert await auth.fetch_jwks(keys, URI) == DOCUMENT
    assert calls == [URI]
    await auth.fetch_jwks(JwksCache(), URI)
    assert calls == [URI, URI]


@pytest.mark.asyncio
@pytest.mark.parametrize("document", [{"keys": []}, {"keys": ["x"]}, [], {}])
async def test_a_malformed_key_set_is_refused_and_not_cached(
    monkeypatch: pytest.MonkeyPatch, document: object
) -> None:
    _downloads(monkeypatch, document)
    keys = JwksCache()
    with pytest.raises(RuntimeError, match="invalid shape"):
        await auth.fetch_jwks(keys, URI)
    assert keys.get(URI, 0.0) is None


@pytest.mark.asyncio
async def test_a_credentialed_endpoint_is_refused() -> None:
    with pytest.raises(RuntimeError, match="credentials"):
        await auth.fetch_jwks(JwksCache(), CREDENTIALED)

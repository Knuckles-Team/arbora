"""Upload references for A2A image parts (ARBORA-A2A-R001.5.5).

An image over the inline threshold travels as ``media:sha256:<hex>`` in the part's
``uri``. The caller obtains it from ``a2a.media.upload``, which stores the bytes by
digest through the engine media methods and records the reference owner and expiry
under the same tenant. Arbora holds no store and never fetches a network address.
A reference of another caller or tenant reads exactly like an absent one.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from arbora.a2a.models import A2AInlineMediaFile

REF_PATTERN = re.compile(r"^media:sha256:([0-9a-f]{64})$")
NAMESPACE = "a2a.media"


class MediaRefRefused(ValueError):
    """A media reference the caller cannot use; the message is the closed reason."""

    REASONS = frozenset({"media_ref_unknown", "media_ref_expired", "media_ref_invalid"})

    def __init__(self, reason: str) -> None:
        if reason not in self.REASONS:
            raise ValueError("unknown media reference refusal reason")
        super().__init__(reason)
        self.reason = reason


class MediaStoreUnavailable(RuntimeError):
    """The engine cannot store or read media bytes; nothing is admitted."""


@dataclass(frozen=True, slots=True)
class StoredMedia:
    media_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class MediaReceipt:
    digest: str
    media_type: str
    bytes: int
    expires_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "media_type": self.media_type,
            "bytes": self.bytes,
            "expires_at": self.expires_at.isoformat(),
        }


class MediaRefStore(Protocol):
    """Content-addressed bytes bound to one caller and tenant, with an expiry."""

    async def put(
        self, owner: str, media_type: str, data: bytes, now: datetime
    ) -> MediaReceipt: ...

    async def get(self, owner: str, digest: str, now: datetime) -> StoredMedia: ...


def _hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class EngineMediaRefStore:
    """``MediaRefStore`` over ``MediaPut``/``MediaGet`` and one engine KV record per owner."""

    def __init__(self, client: Any, graph: str, ttl_seconds: int) -> None:
        self._client = client
        self._graph = graph
        self._ttl = timedelta(seconds=ttl_seconds)

    @staticmethod
    def _key(owner: str, digest: str) -> str:
        return f"{_hex(owner.encode('utf-8'))[:32]}:{digest}"

    async def put(
        self, owner: str, media_type: str, data: bytes, now: datetime
    ) -> MediaReceipt:
        from arbora.pin_lag.meristem import media_methods

        media_methods()
        from medulla.generated import storage

        digest = _hex(data)
        expires_at = now + self._ttl
        record = {
            "media_type": media_type,
            "bytes": len(data),
            "exp": expires_at.isoformat(),
        }
        try:
            stored = await storage.send_media_put(
                self._client,
                {"media_type": media_type, "data": data},
                self._graph,
                idempotency_key=f"a2a.media:{digest}",
            )
            if str(stored).removeprefix("sha256:") != digest:
                raise MediaStoreUnavailable("engine stored different bytes")
            await storage.send_kv_put(
                self._client,
                {
                    "namespace": NAMESPACE,
                    "key": self._key(owner, digest),
                    "value": json.dumps(record).encode("utf-8"),
                },
                self._graph,
            )
        except MediaStoreUnavailable:
            raise
        except Exception:
            raise MediaStoreUnavailable("media upload failed") from None
        return MediaReceipt(f"sha256:{digest}", media_type, len(data), expires_at)

    async def get(self, owner: str, digest: str, now: datetime) -> StoredMedia:
        from arbora.pin_lag.meristem import media_methods

        media_methods()
        from medulla.generated import storage

        try:
            entry = storage.decode_kv_get(
                await storage.send_kv_get(
                    self._client,
                    {"namespace": NAMESPACE, "key": self._key(owner, digest)},
                    self._graph,
                )
            )
            if entry is None:
                raise MediaRefRefused("media_ref_unknown")
            record = json.loads(bytes(entry.value))
            if now >= datetime.fromisoformat(record["exp"]):
                raise MediaRefRefused("media_ref_expired")
            raw = await storage.send_media_get(
                self._client, {"digest": f"sha256:{digest}"}, self._graph
            )
            data = bytes(raw.payload if hasattr(raw, "payload") else raw)
        except MediaRefRefused:
            raise
        except Exception:
            raise MediaStoreUnavailable("media read failed") from None
        if _hex(data) != digest or len(data) != record["bytes"]:
            raise MediaRefRefused("media_ref_unknown")
        return StoredMedia(record["media_type"], data)


def ref_digest(uri: str) -> str:
    """The hex digest a ``media:sha256:<hex>`` reference names."""
    match = REF_PATTERN.fullmatch(uri)
    if match is None:
        raise MediaRefRefused("media_ref_invalid")
    return match.group(1)


async def resolve_media_refs(
    message: Any,
    store: MediaRefStore | None,
    owner: str,
    now: datetime | None = None,
) -> Any:
    """The message with each reference part replaced by the inline bytes it names."""
    from arbora.a2a.models import A2AFilePart, A2AMediaRefFile

    refs = [
        part
        for part in message.parts
        if isinstance(part, A2AFilePart) and isinstance(part.file, A2AMediaRefFile)
    ]
    if not refs:
        return message
    if store is None:
        raise MediaStoreUnavailable("no media store is bound")
    if not owner:
        raise MediaRefRefused("media_ref_unknown")
    moment = now or datetime.now(UTC)
    parts = []
    for part in message.parts:
        if part in refs:
            stored = await store.get(owner, ref_digest(part.file.uri), moment)
            if stored.media_type != part.file.mime_type:
                raise MediaRefRefused("media_ref_unknown")
            part = A2AFilePart(
                kind="file",
                file=A2AInlineMediaFile(
                    bytes=base64.b64encode(stored.data).decode("ascii"),
                    mime_type=stored.media_type,
                    name=part.file.name,
                ),
            )
        parts.append(part)
    return message.model_copy(update={"parts": parts})


def run_media(message: Any, capability: Any) -> dict[str, Any]:
    """What the run request carries for the admitted images (ARBORA-A2A-R001.5.7).

    Images pass as inline binary with the selected model's accepted types. The
    fallback mode is ``refuse`` until the engine decision carries one, so a typed
    harness refusal reaches the caller as a task failure and no image is dropped.
    """
    from arbora.a2a.parts import IMAGE_TYPES
    from arbora.api.contracts.agent_control_contracts import AgentAttachment

    images = [
        AgentAttachment(media_type=file.mime_type, data=file.decoded_bytes())
        for _, file in message.media_files()
        if file.mime_type in IMAGE_TYPES
    ]
    return {
        "attachments": tuple(images),
        "input_media_types": tuple(kind for kind, _ in capability.accepts),
        "media_fallback": "refuse",
    }


class SessionMediaRefStore:
    """The served store: the engine view and graph come from the verified session."""

    def __init__(self, engine_provider: Any) -> None:
        self._engine_provider = engine_provider

    def _store(self) -> EngineMediaRefStore:
        from arbora.core.settings import runtime_config
        from arbora.security.session import current_session

        session = current_session()
        engine = self._engine_provider()
        if session is None or engine is None or not session.graph:
            raise MediaStoreUnavailable("no verified session or engine")
        client = engine.graph_compute.for_graph(session.graph).async_client
        return EngineMediaRefStore(
            client, session.graph, int(runtime_config.a2a_media_ref_ttl_seconds)
        )

    async def put(
        self, owner: str, media_type: str, data: bytes, now: datetime
    ) -> MediaReceipt:
        return await self._store().put(owner, media_type, data, now)

    async def get(self, owner: str, digest: str, now: datetime) -> StoredMedia:
        return await self._store().get(owner, digest, now)

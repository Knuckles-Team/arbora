"""The signed-ingress verifier: HMAC, timestamp skew and single-use delivery ids (ARBORA-OPS-R896.8).

One configured source per code-host connector (``ARBORA_DEVELOPMENT_SCM_SOURCES``, JSON): its
secret is a reference, never a value; its signature, delivery-id and timestamp headers are
named; its principal is the platform service principal the verified request is served as.
The signature is ``sha256=<hex>`` of the HMAC-SHA256 of ``<timestamp>.<body>`` (the body alone
when the source declares no timestamp header), compared in constant time. A delivery id is
single use: the engine's ``register_assertion`` records it until its window passes, and a
second use refuses. Every refusal is one precise code for the audit event; the caller sees the
one uniform 401. Authorization stays the engine's: the principal is only an identity.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from arbora.security.signed_ingress import (
    SCM_INGRESS_PREFIX,
    AdmittedIngress,
    SignedIngressRefused,
)

from .engine import IdentityUnavailable
from .mfa_ceremonies import Audit, log_audit

DEFAULT_SKEW_S = 300
DEFAULT_SCOPES = ("lifecycle:write",)
MAX_BODY_BYTES = 1_048_576
_FIELDS = frozenset(
    {
        "id",
        "secret_ref",
        "principal",
        "tenant",
        "scopes",
        "signature_header",
        "delivery_header",
        "timestamp_header",
        "skew_s",
        "server",
        "normalize_tool",
        "list_tool",
    }
)


@dataclass(frozen=True, slots=True)
class ScmSource:
    """One signed source; ``secret_ref`` is a reference and never a value."""

    id: str
    secret_ref: str
    principal: str
    tenant: str
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    signature_header: str = "x-hub-signature-256"
    delivery_header: str = "x-github-delivery"
    timestamp_header: str = "x-arbora-timestamp"
    skew_s: int = DEFAULT_SKEW_S
    server: str = ""
    normalize_tool: str = ""
    list_tool: str = ""


class ReplayStore(Protocol):
    """Single-use registration: True when the id was new, False when already held."""

    async def register(self, assertion_id: str, expires_at: datetime) -> bool: ...


def _source(item: Mapping[str, Any]) -> ScmSource:
    """One configured source; a blank field, an unknown field or a literal secret refuses."""
    if not set(item) <= _FIELDS:
        raise ValueError("unknown field")
    ident = str(item["id"])

    def named(field: str, default: str) -> str:
        return str(item.get(field) or default)

    source = ScmSource(
        id=ident,
        secret_ref=str(item["secret_ref"]),
        principal=str(item["principal"]),
        tenant=str(item["tenant"]),
        scopes=tuple(item.get("scopes") or DEFAULT_SCOPES),
        signature_header=named("signature_header", "x-hub-signature-256").lower(),
        delivery_header=named("delivery_header", "x-github-delivery").lower(),
        timestamp_header=named("timestamp_header", "x-arbora-timestamp").lower(),
        skew_s=int(item.get("skew_s", DEFAULT_SKEW_S)),
        server=named("server", ident),
        normalize_tool=named("normalize_tool", f"{ident}_normalize_scm_event"),
        list_tool=named("list_tool", f"{ident}_list_scm_events"),
    )
    if not all((source.id, source.secret_ref, source.principal, source.tenant)):
        raise ValueError("blank field")
    if "://" not in source.secret_ref or not 1 <= source.skew_s <= 3600:
        raise ValueError("secret must be a reference; skew is bounded")
    return source


def scm_sources(text: str) -> dict[str, ScmSource]:
    """Parse the source list (JSON text); a malformed entry refuses the whole list."""
    try:
        sources = [_source(item) for item in json.loads(text or "[]")]
    except (ValueError, KeyError, TypeError, AttributeError):
        raise IdentityUnavailable("development scm sources are malformed") from None
    return {source.id: source for source in sources}


def configured_sources() -> dict[str, ScmSource]:
    """The sources the host is configured with (``ARBORA_DEVELOPMENT_SCM_SOURCES``)."""
    from arbora.core.settings import setting

    return scm_sources(str(setting("ARBORA_DEVELOPMENT_SCM_SOURCES", "") or ""))


def _resolve_secret(ref: str) -> str | None:
    from arbora.security.secrets import create_secrets_client

    return create_secrets_client().resolve_ref(ref)


class ScmSignedIngress:
    """The admitter: one configured source per connector path segment."""

    def __init__(
        self,
        sources: Mapping[str, ScmSource],
        replay: Callable[[str], ReplayStore],
        *,
        clock_ms: Callable[[], int],
        resolve: Callable[[str], str | None] = _resolve_secret,
        audit: Audit = log_audit,
    ) -> None:
        self._sources = dict(sources)
        self._replay = replay
        self._clock_ms = clock_ms
        self._resolve = resolve
        self._audit = audit

    def _refuse(self, source: str, code: str) -> SignedIngressRefused:
        self._audit({"protocol": "scm-ingress", "provider": source, "outcome": code})
        return SignedIngressRefused(code)

    def _signature(
        self, source: ScmSource, headers: Mapping[str, str], body: bytes
    ) -> str:
        """The verified timestamp text ('' when none is declared); raises on a bad signature."""
        secret = self._resolve(source.secret_ref)
        if not secret:
            raise self._refuse(source.id, "SECRET_UNRESOLVED")
        stamp = (
            headers.get(source.timestamp_header, "") if source.timestamp_header else ""
        )
        if source.timestamp_header and not stamp:
            raise self._refuse(source.id, "TIMESTAMP_MISSING")
        provided = headers.get(source.signature_header)
        if not provided:
            raise self._refuse(source.id, "SIGNATURE_MISSING")
        message = (stamp.encode() + b"." if stamp else b"") + body
        expected = (
            "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
        )
        if not hmac.compare_digest(provided.encode(), expected.encode()):
            raise self._refuse(source.id, "SIGNATURE_INVALID")
        return stamp

    def _expiry_ms(self, source: ScmSource, stamp: str) -> int:
        """The replay window end; refuses a timestamp outside the skew."""
        now = self._clock_ms()
        if not stamp:
            return now + source.skew_s * 1000
        try:
            at = int(stamp) * 1000
        except ValueError:
            raise self._refuse(source.id, "TIMESTAMP_INVALID") from None
        if abs(now - at) > source.skew_s * 1000:
            raise self._refuse(source.id, "TIMESTAMP_SKEW")
        return at + 2 * source.skew_s * 1000

    async def admit(
        self, path: str, headers: Mapping[str, str], body: bytes
    ) -> AdmittedIngress:
        connector = path.removeprefix(SCM_INGRESS_PREFIX).strip("/")
        source = self._sources.get(connector)
        if source is None or "/" in connector:
            raise self._refuse(connector[:64], "SOURCE_UNKNOWN")
        lowered = {key.lower(): value for key, value in headers.items()}
        if len(body) > MAX_BODY_BYTES:
            raise self._refuse(source.id, "BODY_TOO_LARGE")
        stamp = self._signature(source, lowered, body)
        expires_ms = self._expiry_ms(source, stamp)
        delivery = lowered.get(source.delivery_header, "")
        if not delivery or len(delivery) > 256:
            raise self._refuse(source.id, "DELIVERY_ID_MISSING")
        fresh = await self._replay(source.id).register(
            delivery, datetime.fromtimestamp(expires_ms / 1000, tz=UTC)
        )
        if not fresh:
            raise self._refuse(source.id, "DELIVERY_REPLAYED")
        self._audit(
            {"protocol": "scm-ingress", "provider": source.id, "outcome": "ADMITTED"}
        )
        return AdmittedIngress(
            source_id=source.id,
            principal=source.principal,
            tenant=source.tenant,
            delivery_id=delivery,
            scopes=source.scopes,
            expires_at=self._clock_ms() // 1000 + source.skew_s,
        )


class _EngineReplay:
    """Single-use registration over the engine's ``register_assertion`` on a lazily resolved client."""

    def __init__(
        self, client: Callable[[], Awaitable[Any]], graph: str, source: str
    ) -> None:
        self._client, self._graph, self._source = client, graph, source

    async def register(self, assertion_id: str, expires_at: datetime) -> bool:
        from .federated import EngineSamlReplayStore

        store = EngineSamlReplayStore(
            await self._client(), self._graph, f"scm-ingress:{self._source}"
        )
        return await store.register(assertion_id, expires_at)


def scm_ingress_from_settings(
    client: Callable[[], Awaitable[Any]], graph: str, *, clock_ms: Callable[[], int]
) -> ScmSignedIngress | None:
    """An admitter when sources are configured, else ``None`` (every delivery stays unadmitted)."""
    sources = configured_sources()
    if not sources:
        return None
    return ScmSignedIngress(
        sources, lambda source: _EngineReplay(client, graph, source), clock_ms=clock_ms
    )

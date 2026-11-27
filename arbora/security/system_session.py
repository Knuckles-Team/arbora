"""System sessions: local-process authority grants and the system write session minted from them (ARBORA-HOST-R038.11)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from arbora.security.key_cache import JwksCache
from arbora.security.settings import DEFAULT_JWT_ALGORITHMS

if TYPE_CHECKING:
    from arbora.security.session import GraphSession
from arbora.security.request_identity import (
    _LOCAL_PROCESS_AUDIENCE,
    _LOCAL_PROCESS_ISSUER,
    _LOCAL_PROCESS_POLICY_VERSION,
    _LOCAL_PROCESS_SUBJECT,
    _LOCAL_PROCESS_TENANT,
    _actor_with_credential_lease,
    _mint_graph_session,
    acquire_process_identity_token,
    actor_from_claims_and_group_map,
    local_process_authority_enabled,
    mint_actor_from_token_sync,
    mint_graph_session,
)

logger = logging.getLogger(__name__)


_LOCAL_PROCESS_BOOTSTRAP_SUBJECT = "arbora:local-process-bootstrap"


class LocalProcessGrant(Enum):
    """The closed set of authorities a tiny-profile local process may mint.

    CONCEPT:X1. The local mint is the one credential-free identity path, so
    what it may carry is fixed here rather than chosen by a caller or a
    setting: :data:`_LOCAL_PROCESS_GRANTS` maps each member to its subject and
    exact roles. Nothing in the table carries ``kg:admin`` (which EG's
    ``allows_method`` treats as every action, including ``admin:*``,
    ``security:*``, ``blob:admin`` and ``*:control``), and
    :func:`_mint_local_process_authority` refuses any grant that would.
    Anything needing administration beyond these grants goes through a real
    configured identity (``KG_AUTH_TOKEN_REF``/``KG_IDENTITY_OAUTH2``, or the
    ``KG_ADMIN_BROKER_OAUTH2`` broker for placement), never the local mint.
    """

    AMBIENT = "ambient"
    GRAPH_PROVISIONING = "graph-provisioning"


#: Subject and exact roles per grant. ``AMBIENT`` is the identity every stdio
#: tool call and background write runs under: ``kg:write`` (expands to
#: ``kg:read``). ``GRAPH_PROVISIONING`` materializes the packaged local
#: engine's own tenant graph: ``kg:read`` for ``PlacementRoute``/
#: ``ListGraphs`` and the exact ``graph:admin`` scope ``CreateGraph`` needs.
_LOCAL_PROCESS_GRANTS: Mapping[LocalProcessGrant, tuple[str, tuple[str, ...]]] = (
    MappingProxyType(
        {
            LocalProcessGrant.AMBIENT: (_LOCAL_PROCESS_SUBJECT, ("kg:write",)),
            LocalProcessGrant.GRAPH_PROVISIONING: (
                _LOCAL_PROCESS_BOOTSTRAP_SUBJECT,
                ("kg:read", "graph:admin"),
            ),
        }
    )
)


#: Scopes a local-process authority can never carry, whatever the table says.
_LOCAL_PROCESS_FORBIDDEN_SCOPES: frozenset[str] = frozenset({"kg:admin", "*"})


def _mint_local_process_authority(grant: LocalProcessGrant) -> GraphSession:
    """Mint one private, process-ephemeral authority for ``grant``.

    This is the single chokepoint for credential-free local authority
    (CONCEPT:X1). An asymmetric key signs one short-lived JWT entirely in
    memory. The token is validated by the same decoder used for external
    bearer identities, then the token and private-key references are
    discarded before the resulting actor is projected into a graph session.
    Claims are fixed neutral service values; no username, host name,
    filesystem path, endpoint, or other local identifier is represented or
    persisted. Roles come only from :data:`_LOCAL_PROCESS_GRANTS`; a projected
    session that would carry ``kg:admin`` or ``*`` is refused.

    Raises:
        PermissionError: The grant would project an administrative scope.
    """
    import secrets
    import time

    from joserfc import jwt
    from joserfc.jwk import RSAKey

    from .auth import _decode_jwt

    subject, roles = _LOCAL_PROCESS_GRANTS[grant]
    key = RSAKey.generate_key(2048)
    public_jwks = {"keys": [key.as_dict(is_private=False)]}
    now = int(time.time())
    token = jwt.encode(
        {"alg": "RS256", "typ": "at+jwt"},
        {
            "aud": _LOCAL_PROCESS_AUDIENCE,
            "exp": now + 120,
            "iat": now,
            "iss": _LOCAL_PROCESS_ISSUER,
            "jti": secrets.token_hex(16),
            "nbf": now - 1,
            "roles": list(roles),
            "scope": " ".join(roles),
            "sub": subject,
            "tenant_id": _LOCAL_PROCESS_TENANT,
        },
        key,
    )
    del key
    claims = _decode_jwt(
        token,
        public_jwks,
        issuer=_LOCAL_PROCESS_ISSUER,
        audience=_LOCAL_PROCESS_AUDIENCE,
        algorithms=DEFAULT_JWT_ALGORITHMS,
    )
    del token
    # The proof key and token are destroyed, but their validated expiry remains
    # authority. Long-running process callers renew by minting a fresh proof;
    # destroying proof material must never turn a bounded credential into an
    # indefinite process grant.
    actor = _actor_with_credential_lease(actor_from_claims_and_group_map(claims, None))
    session = _mint_graph_session(
        actor,
        audience=_LOCAL_PROCESS_AUDIENCE,
        policy_version=_LOCAL_PROCESS_POLICY_VERSION,
    )
    forbidden = session.scopes & _LOCAL_PROCESS_FORBIDDEN_SCOPES
    if forbidden:
        raise PermissionError(
            f"local-process authority may not carry {sorted(forbidden)}; "
            "administration requires a configured external identity"
        )
    return session


def mint_local_process_session() -> GraphSession:
    """Mint a private, process-ephemeral authority for tiny stdio Arbora.

    This is the identity every stdio tool call, the ``_PROCESS_SESSION``
    fallback, and background/system writes (:func:`system_write_session`) run
    under, so it is least-privilege (CONCEPT:X1): ``kg:read``/``kg:write``,
    never ``kg:admin``. Before X1 every ``tiny``-profile local process was
    minted ``kg:admin`` unconditionally, which passed every engine scope check
    including ``admin:*``/``security:*``/``*:control`` actions for any local
    caller with zero configuration. There is no setting that widens it:
    administration needs a configured external identity.
    """
    return _mint_local_process_authority(LocalProcessGrant.AMBIENT)


def mint_local_process_bootstrap_authority() -> GraphSession:
    """Mint a one-shot graph-lifecycle authority for tiny-profile provisioning.

    NOT for ambient or served use. Materializing a fresh packaged local
    engine's OWN tenant graph on first start
    (``GraphComputeEngine._ensure_local_session_graph``) is the one
    admin-scoped engine action a zero-infra ``tiny`` deployment must perform
    with no external IdP behind it. The returned session carries exactly
    ``kg:read`` and the engine's ``graph:admin`` scope -- never ``kg:admin`` --
    so it cannot reach ``admin:*``, ``security:*``, ``blob:admin`` or
    ``*:control`` actions (CONCEPT:X1). Callers MUST use it for exactly that
    one provisioning call and MUST NOT install it as ambient or served
    identity.
    """
    return _mint_local_process_authority(LocalProcessGrant.GRAPH_PROVISIONING)


def _require_system_session_mode(session: GraphSession) -> None:
    """Reject ambient authority belonging to the other deployment mode."""
    from arbora.security.anonymous import admitted_actor, enabled

    if (enabled() and not admitted_actor(session.actor)) or (
        not enabled() and getattr(session.actor, "authority_mode", "verified") == "none"
    ):
        raise PermissionError("System session authority mode mismatch")


def system_write_session(keys: JwksCache, config: Any = None) -> GraphSession:
    """Resolve the verified authority for a background/system-triggered graph write.

    BUG-033/BUG-039 (CONCEPT:MERISTEM-OS.identity.authenticated-identity-enforcement):
    three write paths (chat persistence, multi-IDE conversation ingestion,
    messaging post-conversation enrichment) used to call the engine with
    whatever ambient actor happened to be bound -- often none, since a bare
    ``asyncio`` scheduler loop or a standalone script never threads one
    through. That silently landed ``Thread``/``Message``/``Concept`` nodes
    with no owner (the downstream ``stamp_ownership`` call raised
    ``PermissionError`` and was swallowed).

    Two-step resolution, preferring the caller's own authority so a write made
    inside an already-authenticated context (a served request, a task-worker
    thread started via ``_authorized_background_thread``, a messaging
    co-service thread whose event loop inherited its daemon's bound session)
    is correctly attributed to that real caller, not a generic system actor:

    1. Prefer the already-verified ambient :class:`~meristem.
       knowledge_graph.core.session.GraphSession` (:meth:`GraphSession.
       from_ambient`).
    2. Else mint -- fresh on every call -- this process's OWN verified system identity through the
       exact mechanism every other background/served entrypoint in this
       codebase already uses to establish its process authority
       (:func:`mint_local_process_session` for the ``tiny`` local profile
       with no configured external identity, else
       :func:`acquire_process_identity_token` ->
       :func:`mint_actor_from_token_sync` -> :func:`mint_graph_session` for a
       configured external identity -- see :func:`messaging.daemon.
       mint_process_identity` and ``kg_server.py``'s own bootstrap, which run
       this identical sequence). This is a REAL, validated, authenticated
       actor -- never a synthesized or unauthenticated one -- so it is never
       a bypass of the ownership-stamping seam it is meant to satisfy.

    NOTHING IS CACHED (BUG-PE-053, ARBORA-HOST-R039.9). The session minted in
    step 2 wraps a bearer JWT with a finite lifetime, and a permanent cache of
    it expired mid-process and failed every consumer closed. A process-local
    cache is also state the host does not own, so each call mints a session
    that is current for the call it is fetched for; the identity provider
    caches its own token.

    Never returns an unauthenticated session: every branch below either
    returns a verified :class:`GraphSession` or raises (``SessionRequiredError``
    from ``from_ambient``'s failure path is caught internally; a genuine
    minting failure -- e.g. neither ``KG_AUTH_TOKEN_REF`` nor
    ``KG_IDENTITY_OAUTH2`` configured outside the ``tiny`` profile --
    propagates loudly instead of returning a degraded identity).
    """
    from arbora.security.anonymous import enabled
    from arbora.security.anonymous import session as anonymous_session
    from arbora.security.session import (
        GraphSession,
        SessionRequiredError,
    )

    try:
        ambient = GraphSession.from_ambient()
        _require_system_session_mode(ambient)
        return ambient
    except SessionRequiredError:  # expected control flow: no ambient session bound is the documented signal to fall through to minting this process's own verified system identity below, not a real failure (see docstring above)
        pass

    if enabled():
        return anonymous_session()

    if config is None:
        config = keys.config
    if local_process_authority_enabled(config):
        return mint_local_process_session()
    token = acquire_process_identity_token(config)
    actor = mint_actor_from_token_sync(token, keys)
    return mint_graph_session(actor, config)

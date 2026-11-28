"""Stable, privacy-safe failures for externally reachable surfaces.

Exception strings are not safe response or log material: HTTP clients, database
drivers, parsers, and subprocess wrappers commonly include credentials,
endpoints, local paths, queries, or payload fragments in them.  This module is
the single public-failure boundary for REST, MCP, and streaming adapters.
"""

from __future__ import annotations

import json
import logging
import traceback
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from functools import lru_cache, wraps
from importlib.resources import files
from types import MappingProxyType
from typing import Any, Protocol

from scion_sdk.privacy import sanitize_for_persistence

from arbora.security.actor_context import ActorContext, current_actor
from arbora.security.operation_result import (
    OperationError,
    OperationResult,
    OperationStatus,
)

_DEFAULT_LOGGER = logging.getLogger(__name__)

PUBLIC_ERROR_MESSAGES = MappingProxyType(
    {
        "operation_failed": "The requested operation could not be completed.",
        "invalid_request": "The request could not be processed.",
        "dependency_unavailable": "A required service is unavailable.",
        "permission_denied": "The operation is not authorized.",
        # BUG-048: a breaker-open / transport-down failure is transient and
        # retryable — distinct from "operation_failed" (which reads as a
        # rejected/malformed request) and from "dependency_unavailable"
        # (which today means "could not even obtain an engine handle").
        "engine_degraded": (
            "The knowledge graph engine is temporarily overloaded; retry shortly."
        ),
        # CONCEPT:MERISTEM-KG.backend.explicit-graph-selection — an explicit `graph`
        # parameter (a physical engine graph, distinct from `connection`, a
        # backend alias) that cannot be honored. Fail-closed: never a fallback
        # to a default graph, never a union. Two distinct codes rather than
        # collapsing into "invalid_request" so a caller can tell "this graph
        # does not exist" from "this request's graph+connection selection is
        # ambiguous/unsupported" apart programmatically.
        "graph_not_found": (
            "The requested graph does not exist in the engine's catalog."
        ),
        "graph_selection_conflict": (
            "The requested graph selection is ambiguous or unsupported for the "
            "resolved connection."
        ),
    }
)

#: Stable identity refusal codes (never messages) and the public code each one maps to
#: (ARBORA-IDENTITY-R028.4, R023.4, R026.3). A refusal code is a closed vocabulary the
#: caller may read; the mapping decides retryability and nothing else.
REFUSAL_PUBLIC_CODES = MappingProxyType(
    {
        "IDENTITY_DELEGATION_REQUIRED": "permission_denied",
        "IDENTITY_EXCHANGE_FAILED": "dependency_unavailable",
        "CLIENT_SESSION_MISSING": "invalid_request",
        "REUSE_DENIED": "permission_denied",
        "CREDENTIAL_REVOKED": "permission_denied",
        "CREDENTIAL_EXPIRED": "permission_denied",
        "CREDENTIAL_MISSING": "permission_denied",
        "INTERACTION_REQUIRED": "permission_denied",
        "STATE_MISMATCH": "permission_denied",
        "INSECURE_REDIRECT": "invalid_request",
        "CREDENTIAL_REQUEST_INVALID": "invalid_request",
        "INPUT_TIMEOUT": "dependency_unavailable",
        "PROVIDER_UNAVAILABLE": "dependency_unavailable",
        "CALLBACK_UNAVAILABLE": "dependency_unavailable",
        "BROWSER_LAUNCH_FAILED": "dependency_unavailable",
        "PERSISTENCE_FAILED": "dependency_unavailable",
    }
)


def refusal_code(exc: BaseException) -> str | None:
    """The mapped identity refusal code an exception carries, else ``None``.

    Read from the ``code`` attribute of a delegation or operation refusal or from the
    ``refusal`` enum of a credential refusal; never from the exception message.
    """
    for candidate in (
        getattr(exc, "code", None),
        getattr(getattr(exc, "refusal", None), "value", None),
    ):
        if isinstance(candidate, str) and candidate in REFUSAL_PUBLIC_CODES:
            return candidate
    return None


def _public_code(code: str, exc: BaseException) -> tuple[str, str | None]:
    """The public code of a failure and the identity refusal code it carries, if any.

    A caller-chosen code is kept; only the ambiguous default is upgraded by a refusal code.
    """
    if code in PUBLIC_ERROR_MESSAGES and code != "operation_failed":
        return code, None
    refusal = refusal_code(exc)
    if refusal is None:
        return "operation_failed", None
    return REFUSAL_PUBLIC_CODES[refusal], refusal


def _refusal_fields(refusal: str | None) -> dict[str, str]:
    return {} if refusal is None else {"refusal_code": refusal}


#: Exception CLASS NAMES (never instances or messages) that mean "the engine
#: circuit breaker tripped or the transport is down" — a transient condition,
#: not a rejected/malformed request (BUG-048). Mirrors
#: ``meristem.knowledge_graph.core.engine_breaker``'s own trip/retry
#: vocabulary (``_TRIP_EXCEPTIONS`` / ``_RETRY_EXCEPTIONS`` plus its typed
#: ``EngineCircuitOpenError``, itself a ``ConnectionError`` subclass),
#: duplicated here BY NAME rather than imported, so this module (the public
#: sanitization boundary) does not take on a security -> knowledge_graph
#: layering dependency.
_ENGINE_DEGRADED_TYPE_NAMES = frozenset(
    {"EngineCircuitOpenError", "ConnectionError", "TimeoutError", "OSError", "EOFError"}
)


@lru_cache(maxsize=1)
def _engine_error_codes() -> frozenset[str]:
    """Read the installed engine's generated refusal vocabulary, never a copy."""
    contract = json.loads(
        files("medulla.contract").joinpath("errors.json").read_text("utf-8")
    )
    return frozenset(
        item["code"] for item in contract["errors"] if isinstance(item["code"], str)
    )


def validated_engine_error_code(value: object) -> str | None:
    """Keep only a declared engine code; absent or invalid metadata stays private."""
    if not isinstance(value, str):
        return None
    try:
        return value if value in _engine_error_codes() else None
    except (ImportError, OSError, ValueError, KeyError, TypeError) as exc:
        _DEFAULT_LOGGER.warning("Engine error vocabulary unavailable: %s", exc)
        return None


def _engine_code_fields(exc: BaseException) -> dict[str, str]:
    """Expose only an installed-contract code, with no arbitrary diagnostics."""
    code = validated_engine_error_code(getattr(exc, "engine_error_code", None))
    return {"engine_error_code": code} if code is not None else {}


def _is_engine_degraded(exc: BaseException) -> bool:
    """True when ``exc`` is a breaker-open / transport-down condition.

    Two shapes reach this boundary:

    1. The breaker's own :class:`EngineCircuitOpenError` (or a bare
       transport ``OSError``/``EOFError`` — ``ConnectionError`` and
       ``TimeoutError`` are both ``OSError`` SIBLINGS, not one a subclass of
       the other, so both must be checked explicitly) raised directly. This
       is exactly the breaker's own ``_TRIP_EXCEPTIONS = (OSError, EOFError)``
       vocabulary (:mod:`meristem.knowledge_graph.core.engine_breaker`)
       — anything that trips the breaker is, by definition, this condition.
    2. The native-Cypher sanitization boundary's ``CypherEngineError``
       (:mod:`meristem.knowledge_graph.backends.medulla_backend`),
       which wraps ``from None`` — deliberately discarding
       ``__cause__``/``__context__`` so the original query/connection detail
       never crosses the boundary — but preserves the ORIGINAL exception's
       class NAME on its own ``error_type`` attribute for exactly this
       purpose: telling a transient breaker/connection failure apart from a
       genuine query rejection without resurrecting the sanitized detail.
       Checked by class name only (never message text), so this stays a
       class-level distinction — sanitizing the detail, not the class.
    """
    # ``PermissionError`` is a builtin ``OSError`` subclass, but this codebase
    # extensively reuses the builtin class itself for APPLICATION-level
    # authorization/policy rejections (166+ ``raise PermissionError(...)``
    # sites across tenant_sharing.py, engine_query.py's ACL guard, the
    # graph-scoped-view/GraphSession guards, ...) — never a transport
    # condition. Exclude it before the OSError check below so an
    # access-denied response is never misreported as a retryable "engine
    # degraded" condition (which also flips ``retryable=True`` for the
    # caller — actively wrong advice for a rejection that will never
    # succeed by retrying).
    if isinstance(exc, PermissionError):
        return False
    if isinstance(exc, (OSError, EOFError)):
        return True
    error_type = getattr(exc, "error_type", None)
    return isinstance(error_type, str) and error_type in _ENGINE_DEGRADED_TYPE_NAMES


def is_engine_degraded(exc: BaseException) -> bool:
    """Public alias for :func:`_is_engine_degraded` (BUG-048).

    ``public_error_payload`` is the canonical OperationResult-shaped boundary
    for the ~200+ ``except Exception: return public_error_json(e)`` call
    sites, but it is not the ONLY place a per-target/per-row failure gets
    collapsed to a caller-facing label — ``arbora.mcp_server.backing.
    fanout_execute`` (and its direct-call "primary target" mirrors in
    ``query_tools.py``) reduce a raised exception to a bare string, outside
    the ``OperationResult``/``OperationError`` schema entirely. Those sites
    need the SAME breaker-vs-rejection classification without duplicating
    the vocabulary in :data:`_ENGINE_DEGRADED_TYPE_NAMES` a second time (the
    duplication this module already avoided once, by reusing
    ``error_type`` rather than reimporting ``engine_breaker``). Exported
    under a public name specifically so a cross-module caller never has to
    import the underscore-prefixed original.
    """
    return _is_engine_degraded(exc)


def _engine_retry_after_hint() -> float:
    """Best-effort ``retry_after_s`` hint for an ``engine_degraded`` failure.

    Sourced from the SAME breaker cooldown configuration
    :class:`~meristem.knowledge_graph.core.engine_breaker.CircuitBreaker`
    itself uses, so the hint is never fabricated — falls back to the
    breaker's own default cooldown only when the config isn't reachable
    here, and never raises (a hint must not break the error path it decorates).
    """
    try:
        from arbora.core.environment import read_env

        # The breaker reads the same declared variable, so the hint is its cooldown.
        return float(read_env("ENGINE_BREAKER_COOLDOWN"))
    except Exception:
        logging.getLogger(__name__).debug(
            "a retry hint must never break error reporting", exc_info=True
        )
        return 15.0  # CircuitBreaker's own default cooldown


#: Coarse, deterministic module-prefix -> layer-label mapping used to derive
#: ``failing_layer`` from the exception's own innermost traceback frame — never
#: guessed, always read from where the exception actually originated. Ordered
#: most-specific first so a sub-package (e.g. ``arbora.mcp_server``) is
#: matched before the generic ``arbora`` fallback.
_FAILING_LAYER_PREFIXES: tuple[tuple[str, str], ...] = (
    ("arbora.mcp_server", "mcp"),
    ("arbora.gateway", "gateway"),
    ("arbora.fleet", "fleet"),
    ("arbora.security", "security"),
    ("arbora.api", "api"),
    ("arbora.observability", "observability"),
    ("arbora", "arbora"),
    ("scion_sdk", "sdk"),
    ("medulla", "engine"),
)


class ErrorStore(Protocol):
    """The bounded error store the host binds (the observability ``Errors``)."""

    @property
    def sink(self) -> Any: ...

    def keep(self, correlation_id: str, record: dict[str, Any]) -> None: ...

    def detail(self, correlation_id: str) -> dict[str, Any] | None: ...


#: The one scope that holds the host's :class:`ErrorStore` for code outside a request. It has
#: no default: a read before an entry point set it refuses and never falls back to a
#: global. A request reads the host it is served by; the host's entry points (composition
#: start, each background service run, each command-line root) set it with
#: :func:`bind_errors`.
_ERRORS: ContextVar[ErrorStore] = ContextVar("arbora_errors")


class ErrorsNotBound(RuntimeError):
    """A failure was reported before any host entry point bound the error store."""


def bind_errors(errors: ErrorStore) -> Token[ErrorStore]:
    """Bind the host's error store for this context; return the reset token."""
    return _ERRORS.set(errors)


def unbind_errors(token: Token[ErrorStore]) -> None:
    """Restore the scope :func:`bind_errors` replaced."""
    _ERRORS.reset(token)


@contextmanager
def bound_errors(errors: ErrorStore) -> Iterator[ErrorStore]:
    """Run a block with ``errors`` bound, then restore the scope it replaced."""
    token = bind_errors(errors)
    try:
        yield errors
    finally:
        unbind_errors(token)


def carry_errors[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Wrap a thread or executor root so it runs with the caller's bound error store.

    A thread does not inherit the context, so a root started from a bound entry point
    carries the store across. Wrapped while nothing is bound, the root is returned
    unchanged and a failure report inside it refuses with :class:`ErrorsNotBound`.
    """
    try:
        errors = _ERRORS.get()
    except LookupError:
        return fn

    @wraps(fn)
    def rooted(*args: P.args, **kwargs: P.kwargs) -> R:
        with bound_errors(errors):
            return fn(*args, **kwargs)

    return rooted


def current_errors() -> ErrorStore:
    """The error store bound by the entry point, or refuse with :class:`ErrorsNotBound`."""
    try:
        return _ERRORS.get()
    except LookupError:
        raise ErrorsNotBound(
            "no host entry point has bound the error store in this context"
        ) from None


def _infer_failing_layer(exc: BaseException) -> str:
    """Best-effort layer label from the exception's own innermost frame.

    Walks the real traceback (never guessed from the exception's class or
    message) to the last frame and maps its module to a coarse, stable layer
    name. Falls back to ``"unknown"`` when the traceback is absent (e.g. an
    exception constructed but never raised) rather than fabricating a layer.
    """

    module_name = ""
    tb = exc.__traceback__
    while tb is not None:
        module_name = tb.tb_frame.f_globals.get("__name__", "") or module_name
        tb = tb.tb_next
    for prefix, layer in _FAILING_LAYER_PREFIXES:
        if module_name == prefix or module_name.startswith(f"{prefix}."):
            return layer
    return "unknown"


def _recording_actor_tenant() -> str:
    """Best-effort tenant scope for the failure being recorded.

    Most ``public_error_payload`` call sites run inside a verified MCP tool
    dispatch, where :func:`current_actor` already resolves (the
    ``ActorContextMiddleware`` / ``verified_tool_session_scope`` boundary binds
    it for the whole call). Some do not (a unit test, a widget fetch outside
    any served request) — those failures are recorded with an empty tenant
    scope rather than raising, since ``public_error_payload`` itself must
    never fail because diagnostics couldn't determine who was calling.
    """

    try:
        return str(current_actor().tenant_id or "").strip()
    except Exception:  # diagnostics must never fail the error path
        # Logged with its REAL cause (not just the type name) so an unexpected
        # identity-layer failure here is still diagnosable, at debug level because
        # the ordinary "no actor bound outside a served request" case is expected
        # and would otherwise be pure noise.
        logging.getLogger(__name__).debug(
            "error-detail tenant scope unresolved; recording with empty scope",
            exc_info=True,
        )
        return ""


def _record_error_detail(
    correlation_id: str, *, error_class: str, failing_layer: str, exc: BaseException
) -> None:
    """Store a sanitized diagnostic bundle resolvable via :func:`resolve_error_detail`.

    The full exception chain (``__cause__``/``__context__``) is rendered via
    :func:`traceback.format_exception` — so a wrapped/re-raised failure keeps
    its real origin — then sanitized through the SAME persistence privacy gate
    (:mod:`scion_sdk.privacy`) used for any other
    payload crossing a persistence/telemetry boundary, before it ever enters
    this process-local store. The recording actor's ``tenant_id`` is captured
    alongside it so :func:`resolve_error_detail_for_actor` can enforce that
    only a caller in the SAME authority scope may resolve it.
    """

    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    sanitized_traceback, _report = sanitize_for_persistence(rendered)
    record = {
        "error_class": error_class,
        "failing_layer": failing_layer,
        "traceback": sanitized_traceback,
        "tenant_id": _recording_actor_tenant(),
    }
    errors = current_errors()
    errors.keep(correlation_id, record)

    sink = errors.sink
    if sink is not None:
        try:
            sink(correlation_id, dict(record))
        except Exception:
            # A durable-audit sink is an OPTIONAL hardening seam (D-24, see
            # Errors.bind_sink) — it must never turn a
            # diagnostics side-channel into a second failure for the caller
            # whose original exception is already being reported. Logged with
            # the real cause (never swallowed silently), just not re-raised.
            _DEFAULT_LOGGER.warning(
                "detail persistence sink failed for correlation_id=%s",
                correlation_id,
                exc_info=True,
            )


def resolve_error_detail(detail_ref: str) -> dict[str, Any] | None:
    """Resolve a previously recorded, sanitized failure detail by its ``detail_ref``.

    Returns ``None`` for an unknown, expired (evicted), or empty reference —
    never raises, so a stale/foreign ref degrades to "no detail available"
    rather than a second failure.

    Checks the bounded in-process store first (the common, fastest case);
    on a miss, falls back to the registered durable sink's ``resolve`` method
    when it defines one (D-24) — so a ``detail_ref`` recorded before a restart,
    or by a different replica, stays resolvable. The fallback is best-effort:
    a broken/unavailable durable backend degrades to the same "no detail
    available" result as an unknown ref, never a second failure.

    This is the unscoped, in-process/durable lookup used internally (and by
    tests). A served surface reachable by an external caller MUST use
    :func:`resolve_error_detail_for_actor` instead, which enforces the tenant
    boundary below.
    """

    if not detail_ref:
        return None
    errors = current_errors()
    record = errors.detail(detail_ref)
    if record is not None:
        return record

    sink = errors.sink
    resolver = getattr(sink, "resolve", None)
    if not callable(resolver):
        return None
    try:
        durable_record = resolver(detail_ref)
    except Exception:  # a durable-backend failure must not break resolution
        _DEFAULT_LOGGER.warning(
            "durable detail persistence sink resolve failed for correlation_id=%s",
            detail_ref,
            exc_info=True,
        )
        return None
    return dict(durable_record) if isinstance(durable_record, dict) else None


def resolve_error_detail_for_actor(
    detail_ref: str, *, actor: ActorContext | None = None
) -> dict[str, Any] | None:
    """Resolve ``detail_ref`` scoped to the caller's own verified tenant authority.

    Fails closed exactly like the existing read-path guard
    (:func:`meristem.knowledge_graph.core.secured_reads._verified_actor`):
    an unauthenticated caller, or one with no ``tenant_id``, raises
    ``PermissionError`` rather than resolving anything — this reuses the SAME
    tenancy model every other served read already enforces, not a new scheme.

    A ref that does not exist and a ref that exists but belongs to a
    DIFFERENT tenant are deliberately indistinguishable from the caller's
    point of view (both return ``None``): confirming the latter would leak
    that a foreign-tenant failure occurred.
    """

    resolved_actor = actor if actor is not None else current_actor()
    resolved_actor.ensure_credential_current()
    caller_tenant = str(getattr(resolved_actor, "tenant_id", "") or "").strip()
    if not getattr(resolved_actor, "authenticated", False) or not caller_tenant:
        raise PermissionError(
            "Error-detail resolution requires verified tenant authority"
        )

    record = resolve_error_detail(detail_ref)
    if record is None:
        return None
    if str(record.get("tenant_id", "") or "").strip() != caller_tenant:
        return None
    return record


def public_error_payload(
    exc: BaseException,
    *,
    logger: logging.Logger | None = None,
    code: str = "operation_failed",
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a strict ``OperationResult`` failure and a correlation log that
    preserves the real cause.

    Unknown codes fail closed to ``operation_failed``.  Callers cannot supply a
    log message or public message, which prevents an untrusted value from being
    reintroduced accidentally at a call site. ``context`` is an optional
    diagnostic mapping (e.g. ``{"action": ..., "tool": ...}``): only its **keys**
    are logged (never values, and never the public payload) so it can aid
    correlation without reintroducing an untrusted value.

    The exception itself IS logged (not just its class name): this is the
    single public-failure boundary for REST/MCP/streaming (~200+ call sites),
    so collapsing it to ``type(exc).__name__`` here made every failure across
    the whole external surface equally undiagnosable server-side (a denied
    scope, a missing driver, an unreadable durable record all looked like the
    same bare "RuntimeError") even though the PUBLIC payload was diagnostically
    opaque by design (every failure returned the same ``operation_failed``
    code, ``retryable=false``, and a ``detail_ref`` that was ALWAYS ``null`` —
    no error class, no failing layer, no way to resolve more detail). The
    process-wide log-privacy boundary (``core/log_privacy.py``) still
    sanitizes the exception's message text (redacts endpoints/paths/emails)
    before it reaches any handler, so none of this reintroduces a leak.

    The returned dict now also carries, as siblings of the strict
    ``OperationResult`` fields (the shared cross-repo Epistemic Operations
    Protocol schema stays untouched — no engine-side regeneration required):

    * ``error_class`` — ``type(exc).__name__``. Never the exception's message —
      just the class name, exactly as already accepted into logs/caplog by
      the existing exception-surface contract (``test_exception_surface_
      hardening.py`` et al. assert the class name IS logged while the message
      never crosses the boundary); this repeats that same, already-reviewed
      trade-off in the payload instead of inventing a new one.
    * ``failing_layer`` — inferred from the exception's own innermost
      traceback frame (:func:`_infer_failing_layer`), never guessed.
    * ``engine_error_code`` — optional native refusal code validated against
      the installed engine contract; no diagnostic detail or unknown value.

    The exception's MESSAGE itself is never added to this payload — the
    existing static/behavioral gates (``test_exception_surface_hardening.py``,
    ``test_graph_query_sql.py``, ``test_graph_tool_targeting.py``, the
    ``test_served_packages_do_not_expose_raw_exception_text`` static gate)
    already enforce that the public envelope never echoes it, and query
    strings, target names, or validation detail in a message are not reliably
    caught by the persistence-privacy pattern list (it targets known secret/
    PII/location shapes, not arbitrary application text). ``error.detail_ref``
    is the SAME value as ``error.correlation_id`` rather than ``None``:
    :func:`_record_error_detail` stores the sanitized full exception chain
    (``__cause__``/``__context__`` included, via
    :func:`traceback.format_exception`, sanitized through the persistence
    privacy gate) under that key. An external caller resolves it through the
    ``graph_observe`` MCP tool's ``error_detail`` action (D-23), which calls
    :func:`resolve_error_detail_for_actor` — never echoed automatically into
    this response, and only resolvable by a caller in the SAME tenant scope
    that recorded it. A null ``detail_ref`` used to imply "no further detail
    exists"; now it is a real, dereferenceable reference.
    """

    safe_code, refusal = _public_code(code, exc)
    # BUG-048: a caller that didn't explicitly classify its failure (the
    # ubiquitous bare ``except Exception: return public_error_json(e)`` at
    # ~200+ call sites) previously collapsed a breaker-open/transport-down
    # condition into the SAME "operation_failed"/``retryable=False`` shape as
    # a rejected or malformed query — indistinguishable to the caller, who
    # then chases a query-correctness hypothesis instead of retrying. Only
    # the ambiguous DEFAULT is upgraded here; an explicit
    # ``permission_denied``/``invalid_request``/``dependency_unavailable``
    # the caller deliberately chose is never overridden.
    if safe_code == "operation_failed" and _is_engine_degraded(exc):
        safe_code = "engine_degraded"
    correlation_id = f"correlation:{uuid.uuid4().hex}"
    error_class = type(exc).__name__
    failing_layer = _infer_failing_layer(exc)
    sink = logger or _DEFAULT_LOGGER
    sink.warning(
        "External operation failed (code=%s correlation_id=%s exception=%s "
        "error_class=%s failing_layer=%s context_keys=%s)",
        safe_code,
        correlation_id,
        exc,
        error_class,
        failing_layer,
        sorted(context) if context else [],
    )
    _record_error_detail(
        correlation_id, error_class=error_class, failing_layer=failing_layer, exc=exc
    )
    payload = OperationResult(
        schema_version="1",
        operation_id=f"operation:{uuid.uuid4().hex}",
        status=OperationStatus.FAILED,
        result_kind=None,
        result_ref=None,
        error=OperationError(
            code=safe_code,
            # BUG-048: a breaker trip IS the definition of retryable — the
            # breaker exists to fail fast now and recover on its own after
            # ``cooldown``, not to reject the request outright.
            retryable=safe_code in ("dependency_unavailable", "engine_degraded"),
            correlation_id=correlation_id,
            detail_ref=correlation_id,
        ),
        redirect=None,
    ).model_dump(mode="json")
    payload["error_class"] = error_class
    payload["failing_layer"] = failing_layer
    payload.update(_engine_code_fields(exc))
    payload.update(_refusal_fields(refusal))
    if safe_code == "engine_degraded":
        payload["retry_after_s"] = _engine_retry_after_hint()
    return payload


def public_error_json(
    exc: BaseException,
    *,
    logger: logging.Logger | None = None,
    code: str = "operation_failed",
    context: dict[str, Any] | None = None,
) -> str:
    """Serialize :func:`public_error_payload` for string-returning MCP tools."""

    payload = public_error_payload(exc, logger=logger, code=code, context=context)
    return json.dumps(payload)


def public_error_text(
    exc: BaseException,
    *,
    logger: logging.Logger | None = None,
    code: str = "operation_failed",
    context: dict[str, Any] | None = None,
) -> str:
    """Return the same structured operation failure as compact JSON text."""

    return public_error_json(exc, logger=logger, code=code, context=context)

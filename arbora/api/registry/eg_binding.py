"""Generate caller-bound operations from medulla's run-time method catalog.

The engine serves its method catalog (methods, scopes and the catalog digest)
at run time through ``medulla.engine.method_catalog()``; its descriptor rows
are the one source of truth. There is no file source and no fallback: a missing
or malformed catalog stops API generation rather than silently omitting
operations.
"""

from __future__ import annotations

import functools
import importlib
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

from .spec import (
    AuditClass,
    Effect,
    EgMethod,
    Executor,
    Idempotency,
    OpSpec,
    PrincipalRule,
    Stability,
    Verb,
    eg_method_ref,
)

_EXCLUSIONS = Path(__file__).with_name("eg_exclusions.yaml")
_VALID_CLASSES = {"user", "domain", "service-only", "approver", "admin"}
#: Descriptor stability -> arbora operation stability. A deprecated engine
#: method carries no removal release, so it maps to the closest non-stable
#: value; ``internal`` methods are never wire-callable and never generated.
_STABILITY = {"stable": Stability.STABLE, "deprecated": Stability.EXPERIMENTAL}


class EgContractError(ValueError):
    """The engine's method catalog cannot safely define a complete API."""


@functools.cache
def _installed_catalog() -> Mapping[str, Any]:
    try:
        engine = importlib.import_module("medulla.engine")
    except ImportError as exc:
        raise EgContractError("the medulla engine is unavailable") from exc
    serve = getattr(engine, "method_catalog", None)
    if not callable(serve):
        raise EgContractError("the installed medulla serves no method catalog")
    try:
        value = json.loads(serve())
    except (TypeError, ValueError) as exc:
        raise EgContractError(f"cannot read the medulla method catalog: {exc}") from exc
    return _checked_catalog(value)


def _checked_catalog(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EgContractError("the medulla method catalog must be an object")
    digest = value.get("catalog_digest")
    if not isinstance(digest, str) or not digest:
        raise EgContractError("the medulla method catalog requires a catalog_digest")
    for key in ("methods", "scopes"):
        rows = value.get(key)
        if not isinstance(rows, list) or not rows:
            raise EgContractError(f"the medulla method catalog requires nonempty {key}")
    return value


def load_catalog(catalog: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    """Return a checked method catalog: ``catalog`` or the installed engine's.

    ``None`` reads ``medulla.engine.method_catalog()`` once per process.
    A supplied mapping (fixtures, tests) is shape-checked the same way.
    """

    if catalog is None:
        return _installed_catalog()
    return _checked_catalog(catalog)


def catalog_digest(catalog: Mapping[str, Any] | None = None) -> str:
    """The catalog digest the engine binds replay identities to."""

    return str(load_catalog(catalog)["catalog_digest"])


_SUPERSEDED_REASON = re.compile(
    r"superseded-by:[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+"
)


def _valid_exclusion_reason(reason: object) -> bool:
    return isinstance(reason, str) and (
        reason in {"engine-internal", "service-only"}
        or _SUPERSEDED_REASON.fullmatch(reason) is not None
    )


def _exclusion_row(row: object, seen: dict[str, str]) -> tuple[str, str]:
    if not isinstance(row, dict) or set(row) != {"method", "reason"}:
        raise EgContractError("each EG exclusion needs method and reason")
    method, reason = row["method"], row["reason"]
    if not isinstance(method, str) or not method or method in seen:
        raise EgContractError(f"duplicate or invalid EG exclusion: {method!r}")
    if not _valid_exclusion_reason(reason):
        raise EgContractError(f"invalid exclusion reason for {method}: {reason!r}")
    return method, reason


def _load_exclusions(path: Path) -> dict[str, str]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise EgContractError(f"cannot read EG exclusions {path}: {exc}") from exc
    if not isinstance(document, dict) or set(document) != {"exclusions"}:
        raise EgContractError("EG exclusions must contain only an exclusions list")
    rows = document["exclusions"]
    if not isinstance(rows, list):
        raise EgContractError("EG exclusions must be a list")
    result: dict[str, str] = {}
    for row in rows:
        method, reason = _exclusion_row(row, result)
        result[method] = reason
    return result


def _load_scopes(catalog: Mapping[str, Any]) -> dict[str, str]:
    scopes: dict[str, str] = {}
    for row in catalog["scopes"]:
        if not isinstance(row, Mapping):
            raise EgContractError("EG scope row must be an object")
        scope, scope_class = row.get("scope"), row.get("class")
        if not isinstance(scope, str) or not scope or scope in scopes:
            raise EgContractError(f"duplicate or invalid EG scope: {scope!r}")
        if scope_class not in _VALID_CLASSES:
            raise EgContractError(f"invalid EG scope class: {scope_class!r}")
        scopes[scope] = scope_class
    return scopes


def load_generated_scopes(
    catalog: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Return the engine catalog's scope name -> scope class mapping.

    This is the single generated scope registry: every scope Arbora itself
    advertises must match a name and class recorded here rather than a
    separately maintained list (ARBORA-IDENTITY-R002). ``catalog`` is for
    fixtures; production reads the installed engine's run-time catalog.
    """

    return _load_scopes(load_catalog(catalog))


def _method_identity(row: dict[str, Any]) -> tuple[str, str]:
    method_id, domain = row.get("id"), row.get("domain")
    if not isinstance(method_id, str) or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_]*", method_id
    ):
        raise EgContractError(f"invalid EG method ID: {method_id!r}")
    if not isinstance(domain, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", domain):
        raise EgContractError(f"{method_id}: invalid domain")
    return method_id, domain


def _method_policy(
    row: dict[str, Any], method_id: str, scopes: dict[str, str]
) -> tuple[str, dict[str, Any]]:
    policy = row.get("policy")
    if not isinstance(policy, dict):
        raise EgContractError(f"{method_id}: missing policy")
    scope = policy.get("authz_action")
    if not isinstance(scope, str) or scope not in scopes:
        raise EgContractError(f"{method_id}: unregistered EG scope {scope!r}")
    if not isinstance(policy.get("mutates"), bool) or not isinstance(
        policy.get("idempotent"), bool
    ):
        raise EgContractError(f"{method_id}: missing mutates/idempotent policy")
    return scope, policy


def _method_effect(
    scope: str, scope_class: str, policy: dict[str, Any]
) -> tuple[bool, Effect]:
    is_admin = (
        scope_class == "admin"
        or scope.startswith("admin:")
        or scope == "security:admin"
    )
    effect = (
        Effect.ADMIN if is_admin else Effect.WRITE if policy["mutates"] else Effect.READ
    )
    return is_admin, effect


def _method_idempotency(
    row: dict[str, Any], method_id: str, policy: dict[str, Any]
) -> Idempotency:
    replay_class = row.get("replay_class")
    if replay_class not in {"NotReplayable", "OperationIdentity", "NonceOnly"}:
        raise EgContractError(f"{method_id}: unknown replay class {replay_class!r}")
    if policy["idempotent"] and not policy["mutates"]:
        return Idempotency.NATURAL
    if replay_class in {"OperationIdentity", "NonceOnly"}:
        return Idempotency.KEY_REQUIRED
    return Idempotency.NONE


def _method_summary(row: dict[str, Any], method_id: str) -> str:
    summary = row.get("note") or re.sub(r"(?<!^)(?=[A-Z])", " ", method_id)
    if not isinstance(summary, str) or not summary.strip():
        raise EgContractError(f"{method_id}: missing summary")
    return summary.strip()


def _method_stability(row: dict[str, Any], method_id: str) -> Stability:
    stability = _STABILITY.get(row.get("stability"))  # type: ignore[arg-type]
    if stability is None:
        raise EgContractError(
            f"{method_id}: unsupported wire stability {row.get('stability')!r}"
        )
    return stability


def _method_op(row: dict[str, Any], scopes: dict[str, str]) -> OpSpec:
    method_id, domain = _method_identity(row)
    scope, policy = _method_policy(row, method_id, scopes)
    scope_class = scopes[scope]
    is_admin, effect = _method_effect(scope, scope_class, policy)
    idempotency = _method_idempotency(row, method_id, policy)
    stability = _method_stability(row, method_id)
    summary = _method_summary(row, method_id)
    return OpSpec(
        id=f"eg.{domain}.{method_id}",
        verb=Verb.MANAGE if is_admin else Verb.WRITE if policy["mutates"] else Verb.ASK,
        summary=summary,
        examples=(f"{method_id} {domain}",),
        params=eg_method_ref(method_id, "request"),
        result=eg_method_ref(method_id, "result"),
        binding=EgMethod(service=method_id, op=method_id),
        executor=Executor.CALLER,
        scopes=frozenset({scope}),
        effect=effect,
        principals=PrincipalRule.SERVICE_ONLY
        if scope_class == "service-only"
        else PrincipalRule.ANY,
        idempotency=idempotency,
        stability=stability,
        audit=AuditClass.EVENT
        if effect is not Effect.READ or policy.get("audited") is True
        else AuditClass.NONE,
    )


def _index_methods(rows: list[Any]) -> dict[str, dict[str, Any]]:
    methods: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            raise EgContractError("EG method row needs an ID")
        method_id = row["id"]
        if method_id in methods:
            raise EgContractError(f"duplicate EG method: {method_id}")
        if not isinstance(row.get("is_wire_callable"), bool):
            raise EgContractError(f"{method_id}: missing is_wire_callable")
        methods[method_id] = dict(row)
    return methods


def _check_exclusion_rot(
    exclusions: dict[str, str],
    methods: dict[str, dict[str, Any]],
    curated: tuple[OpSpec, ...],
) -> None:
    for method_id, reason in exclusions.items():
        row = methods.get(method_id)
        if row is None or not row["is_wire_callable"]:
            raise EgContractError(f"stale EG exclusion: {method_id}")
        if reason.startswith("superseded-by:"):
            expected = reason.partition(":")[2]
            if not any(op.id == expected for op in curated):
                raise EgContractError(f"{method_id}: absent superseding op {expected}")


def _bind_curated_op(
    op: OpSpec,
    binding: EgMethod,
    methods: dict[str, dict[str, Any]],
    exclusions: dict[str, str],
    scopes: dict[str, str],
    direct: dict[str, OpSpec],
) -> None:
    method_id = binding.service
    if method_id not in methods or not methods[method_id]["is_wire_callable"]:
        raise EgContractError(f"{op.id}: stale EG binding {method_id}")
    if method_id in direct or method_id in exclusions:
        raise EgContractError(f"{method_id}: duplicate EG binding or exclusion")
    method_scope = methods[method_id]["policy"]["authz_action"]
    if op.executor is not Executor.CALLER or op.scopes != frozenset({method_scope}):
        raise EgContractError(f"{op.id}: curated EG authority differs from {method_id}")
    if (
        scopes[method_scope] == "service-only"
        and op.principals is not PrincipalRule.SERVICE_ONLY
    ):
        raise EgContractError(f"{op.id}: service-only EG method exposed to users")
    direct[method_id] = op


def _bind_curated(
    curated: tuple[OpSpec, ...],
    methods: dict[str, dict[str, Any]],
    exclusions: dict[str, str],
    scopes: dict[str, str],
) -> dict[str, OpSpec]:
    direct: dict[str, OpSpec] = {}
    for op in curated:
        if isinstance(op.binding, EgMethod):
            _bind_curated_op(op, op.binding, methods, exclusions, scopes, direct)
    return direct


def _check_coverage(
    generated: tuple[OpSpec, ...],
    exclusions: dict[str, str],
    direct: dict[str, OpSpec],
    methods: dict[str, dict[str, Any]],
) -> None:
    covered = (
        {op.id.rsplit(".", 1)[1] for op in generated} | set(exclusions) | set(direct)
    )
    wire = {method_id for method_id, row in methods.items() if row["is_wire_callable"]}
    if covered != wire:
        raise EgContractError(f"EG coverage mismatch: {sorted(wire - covered)}")


def load_eg_bindings(
    *,
    catalog: Mapping[str, Any] | None = None,
    curated_ops: Iterable[OpSpec] = (),
    exclusions_path: Path | None = None,
) -> tuple[OpSpec, ...]:
    """Return one op per wire-callable EG method not covered by a curated op.

    Exact method coverage, exclusion rot, scope registration and the curated
    bindings are checked before returning. ``catalog`` and ``exclusions_path``
    are for fixtures; production reads the installed engine's run-time catalog.
    """

    checked = load_catalog(catalog)
    scopes = _load_scopes(checked)
    exclusions = _load_exclusions(exclusions_path or _EXCLUSIONS)
    methods = _index_methods(checked["methods"])
    curated = tuple(curated_ops)
    _check_exclusion_rot(exclusions, methods, curated)
    direct = _bind_curated(curated, methods, exclusions, scopes)
    generated = tuple(
        _method_op(row, scopes)
        for method_id, row in sorted(methods.items())
        if row["is_wire_callable"]
        and method_id not in exclusions
        and method_id not in direct
    )
    _check_coverage(generated, exclusions, direct, methods)
    return generated

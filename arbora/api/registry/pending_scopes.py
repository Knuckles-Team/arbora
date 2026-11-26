"""Operations that wait on an engine scope the pinned catalog lacks.

The registry loader is the one chokepoint (``registry_factory.get_registry``).
An operation naming a scope missing from the pinned engine catalog is a
declaration error, unless the scope is listed in ``pending_engine_scopes.yaml``.
Such an operation is served, holds none of its pending scopes and refuses
``UNAVAILABLE`` without calling the engine. A misspelt scope is not listed, so
it still stops loading. Removing a scope from the list restores the operation.
"""

from __future__ import annotations

import functools
from collections.abc import Collection, Iterable
from pathlib import Path

import yaml

from .spec import Composite, Executor, OpSpec

_PENDING = Path(__file__).with_name("pending_engine_scopes.yaml")
REFUSAL_HANDLER = "arbora.api.ops._pending.refuse_unavailable"


class PendingScopesError(ValueError):
    """The pending-scope list is malformed."""


@functools.cache
def load_pending_engine_scopes(path: Path = _PENDING) -> frozenset[str]:
    """The scopes the pinned engine catalog is still waiting for."""
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    listed = document.get("pending_engine_scopes")
    if not isinstance(listed, list) or not all(
        isinstance(scope, str) and scope for scope in listed
    ):
        raise PendingScopesError("pending_engine_scopes must be a list of scopes")
    if len(set(listed)) != len(listed):
        raise PendingScopesError("pending_engine_scopes lists a scope twice")
    return frozenset(listed)


def _unavailable(op: OpSpec, pending: frozenset[str]) -> OpSpec:
    """The same operation without its pending scopes, bound to the refusal."""
    scopes = op.scopes - pending
    executor_scopes = op.executor_scopes - pending
    update: dict[str, object] = {
        "scopes": scopes,
        "executor_scopes": executor_scopes,
        "binding": Composite(handler=REFUSAL_HANDLER),
    }
    if op.executor is Executor.SERVICE:
        update.update(
            executor=Executor.CALLER, executor_scopes=frozenset(), subject=None
        )
    return op.model_copy(update=update)


def apply_pending_scopes(
    curated: Iterable[OpSpec],
    catalog_scopes: Collection[str],
    pending: frozenset[str] | None = None,
) -> tuple[list[OpSpec], list[str]]:
    """Split ``curated`` into servable operations and unlisted-scope errors.

    Returns the operations (pending ones rebound to the refusal) and one error
    line per operation naming a scope that is neither in the catalog nor listed.
    """
    waiting = load_pending_engine_scopes() if pending is None else pending
    served: list[OpSpec] = []
    errors: list[str] = []
    for op in curated:
        missing = (op.scopes | op.executor_scopes) - set(catalog_scopes)
        unlisted = missing - waiting
        if unlisted:
            errors.append(
                f"{op.id}: unregistered curated EG scopes: {', '.join(sorted(unlisted))}"
            )
        served.append(_unavailable(op, missing) if missing and not unlisted else op)
    return served, errors

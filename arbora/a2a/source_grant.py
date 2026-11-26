"""The verified source grant a native run carries (ARBORA-OPS-R894).

A run's work item carries ``metadata.source_grant {repository, branch, prefixes[]}`` and
the changeset ports read their target from it. The grant is **never** taken from request
text: the ``agents.run`` admission resolves it from the engine-held grant entry of the
verified caller (KV namespace ``source_grants``, key the caller's principal, one entry per
tenant) and binds it for the one admission with :func:`source_grant_scope`. A caller with
no entry, or with a malformed one, gets no grant, so the run's changeset ports stay unbound
and submission refuses closed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

#: The work-item metadata key the changeset seams read.
SOURCE_GRANT_KEY = "source_grant"
#: The engine KV namespace holding one grant entry per principal.
NAMESPACE = "source_grants"

_bound: ContextVar[Mapping[str, Any] | None] = ContextVar(
    "arbora_verified_source_grant", default=None
)


def normalized(raw: Any) -> dict[str, Any] | None:
    """The grant in its one shape, or ``None`` when any part is malformed."""
    from arbora.agent_host.changeset_seams import source_target

    target = source_target({"metadata": {SOURCE_GRANT_KEY: raw}})
    if target is None:
        return None
    return {
        "repository": target.repository,
        "branch": target.branch,
        "prefixes": list(target.prefixes),
    }


@contextmanager
def source_grant_scope(grant: Mapping[str, Any] | None) -> Iterator[None]:
    """Bind the caller's verified grant for one admission; a malformed one binds none."""
    token = _bound.set(normalized(grant) if grant is not None else None)
    try:
        yield
    finally:
        _bound.reset(token)


def bound_source_grant() -> dict[str, Any] | None:
    """The grant bound by the admission in progress, or ``None``."""
    grant = _bound.get()
    return dict(grant) if grant is not None else None


def work_item_grant_metadata() -> dict[str, Any]:
    """The metadata fragment a submitted run carries: the bound grant, or nothing."""
    grant = bound_source_grant()
    return {SOURCE_GRANT_KEY: grant} if grant is not None else {}


class EngineSourceGrants:
    """The composed ``source_grants`` service: reads the verified caller's grant."""

    async def resolve(self, context: Any) -> dict[str, Any] | None:
        return await read_engine_grant(context)


async def resolve_verified_grant(context: Any) -> dict[str, Any] | None:
    """The verified caller's grant through the composed service, or ``None``.

    A host that composes no ``source_grants`` service grants nothing: its runs carry no
    grant and the changeset ports refuse closed.
    """
    service = context.services.get("source_grants")
    if service is None:
        return None
    return normalized(await service.resolve(context))


async def read_engine_grant(context: Any) -> dict[str, Any] | None:
    """Read the grant entry of ``context.caller.principal``; absent or malformed is ``None``."""
    from arbora.api.ops._engine import engine_send

    entry = await engine_send(
        context,
        "KvGet",
        {"namespace": NAMESPACE, "key": str(context.caller.principal)},
    )
    raw = entry.get("value") if isinstance(entry, Mapping) else None
    if isinstance(raw, (list, tuple)):
        raw = bytes(raw)
    if isinstance(raw, (bytes, bytearray, memoryview)):
        try:
            raw = json.loads(bytes(raw))
        except ValueError:
            return None
    return normalized(raw)

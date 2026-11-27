"""The verified source grant a native run carries (ARBORA-OPS-R894).

A run's work item carries ``metadata.source_grant {repository, branch, prefixes[]}`` and
the changeset ports read their target from it. The grant is **never** taken from request
text: the ``agents.run`` admission resolves it from the engine's path grants of the
verified caller (``ListPathGrants``, MEDULLA-SCM-R007.1) and binds it for the one admission
with :func:`source_grant_scope`. The repository is the one repository the caller may write,
the prefixes are its write grants, and the branch is the host's configured run branch. A
caller with no write grant, with write grants on more than one repository, or on a release
that lacks the method, gets no grant, so the run's changeset ports stay unbound and
submission refuses closed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any

#: The work-item metadata key the changeset seams read.
SOURCE_GRANT_KEY = "source_grant"
#: The engine request shapes the grant read needs (PIN-LAG: engine ``main``, unreleased).
LIST_PATH_GRANTS_MODELS: tuple[str, ...] = ("ListPathGrantsRequest", "PathGrantList")

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

    def __init__(self, branch: str) -> None:
        self._branch = branch

    async def resolve(self, context: Any) -> dict[str, Any] | None:
        return await read_engine_grant(context, branch=self._branch)

    @asynccontextmanager
    async def admission_scope(self, context: Any) -> AsyncIterator[None]:
        """Bind the verified caller's grant for one admission; a malformed one binds none."""
        with source_grant_scope(normalized(await self.resolve(context))):
            yield


async def resolve_verified_grant(context: Any) -> dict[str, Any] | None:
    """The verified caller's grant through the composed service, or ``None``.

    A host that composes no ``source_grants`` service grants nothing: its runs carry no
    grant and the changeset ports refuse closed.
    """
    service = context.services.get("source_grants")
    if service is None:
        return None
    return normalized(await service.resolve(context))


def grant_from_path_grants(grants: Any, *, branch: str) -> dict[str, Any] | None:
    """The run grant the caller's path grants amount to, or ``None``.

    Only write grants count. The caller must hold them on exactly one repository: two
    repositories make the target ambiguous and bind none. A whole-repository grant
    (empty prefix) names no path prefix and adds none. A stored deny (both bits false)
    is never a write grant; the engine still enforces it on every path.
    """
    prefixes = _write_prefixes(grants)
    if len(prefixes) != 1:
        return None
    ((repository, held),) = prefixes.items()
    return normalized(
        {
            "repository": repository,
            "branch": branch,
            "prefixes": sorted(prefix for prefix in held if prefix),
        }
    )


def _write_prefixes(grants: Any) -> dict[str, set[str]]:
    """The write-granted prefixes of each repository the path grants name."""
    prefixes: dict[str, set[str]] = {}
    for entry in grants if isinstance(grants, (list, tuple)) else ():
        if not isinstance(entry, Mapping) or entry.get("write") is not True:
            continue
        repo, prefix = entry.get("repo"), entry.get("prefix")
        if isinstance(repo, str) and repo and isinstance(prefix, str):
            prefixes.setdefault(repo, set()).add(prefix)
    return prefixes


async def read_engine_grant(context: Any, *, branch: str) -> dict[str, Any] | None:
    """List the verified principal's path grants; a release without the method is no grant."""
    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.api.ops.pin_guards import send_main

    try:
        answer = await send_main(
            context,
            "ListPathGrants",
            {"request": {"principal": str(context.caller.principal)}},
            models=LIST_PATH_GRANTS_MODELS,
        )
    except OperationRefused as refused:
        if refused.code == "UNAVAILABLE":
            return None
        raise
    grants = answer.get("grants") if isinstance(answer, Mapping) else None
    return grant_from_path_grants(grants, branch=branch)

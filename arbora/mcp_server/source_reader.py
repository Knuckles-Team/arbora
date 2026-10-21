"""Stateless native source reader under the admitted caller's authority.

Serves the engine's branch-aware source projection (ARBORA-OPS-R848.1):
one repository's ``Branch`` node, its live ``FileVersion`` memberships and
their ``Blob`` content, through the caller-bound graph reads of the pinned
engine contract. This host adapter owns no source state and retains no
caller, client or session.

The projection stores no directory nodes: directories are derived from the
``FileVersion`` path prefixes. The projection stores no line-level
attribution: ``source.blame`` serves the per-file last-touch revision of the
membership edge. Both limits are reported in the outcome, never hidden.
"""

from __future__ import annotations

from typing import Any

import msgpack

from arbora.api.invoke.bootstrap import _caller_dispatch_graph
from arbora.api.invoke.executor import ExecutionContext
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import claim_values
from arbora.api.ops.source import (
    SourceBlameOutcome,
    SourceBlobOutcome,
    SourceHistoryEntry,
    SourceHistoryOutcome,
    SourceTreeEntry,
    SourceTreeOutcome,
)

#: Keyset page size for label scans and engine batch reads.
_PAGE_SIZE = 256
#: A label scan may cover at most this many pages before refusing.
_MAX_SCAN_PAGES = 256
#: ``GetNodePropertiesBatch`` bounds ids per call; keep the chunks bounded.
_BATCH_SIZE = 256

TREE_SCOPES = frozenset({"node:read", "edge:read"})
BLOB_SCOPES = frozenset({"node:read", "edge:read", "blob:read"})


def _require_scopes(context: Any, required: frozenset[str]) -> None:
    """Fail closed before any engine call when a required scope is absent."""
    missing = sorted(required - set(context.caller.effective_scopes))
    if missing:
        raise OperationRefused("SCOPE_REQUIRED", {"missing_scopes": missing})


def _decode_properties(payload: Any) -> dict[str, Any] | None:
    """One property blob as stored: MessagePack bytes, or an absent row."""
    if payload is None:
        return None
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (bytes, bytearray, memoryview)):
        decoded = msgpack.unpackb(bytes(payload), raw=False)
        return decoded if isinstance(decoded, dict) else None
    return None


async def _send(context: Any, graph: str, method: str, params: dict[str, Any]) -> Any:
    """One admitted engine read under the caller's verified graph and claims."""
    from epistemic_graph import EngineResponseError
    from epistemic_graph.generated import send_by_id

    from arbora.api.errors import EngineRefusal

    with context.client.use_verified_context(
        claim_values(context.caller.engine_claims)
    ):
        try:
            return await send_by_id(context.client, method, params, graph)
        except EngineResponseError as exc:
            raise EngineRefusal(exc.code) from exc


def _rows(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    raise OperationRefused("UNAVAILABLE", {"reason": "engine result malformed"})


async def _branch_nodes(
    context: Any,
    graph: str,
    *,
    repository: str,
    ref: str | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Every visible live Branch node of one repository, in stable id order.

    Branch node ids are content-addressed and not invertible, so the label
    scan matches on the node properties the projection stores. A scan that
    runs out of pages without a short page refuses: the answer is unknown,
    not empty.
    """
    found: list[tuple[str, dict[str, Any]]] = []
    cursor: str | None = None
    for _ in range(_MAX_SCAN_PAGES):
        rows = _rows(
            await _send(
                context,
                graph,
                "GetNodesByLabel",
                {"label": "Branch", "limit": _PAGE_SIZE, "after": cursor},
            )
        )
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                continue
            node_id, props = row
            if not isinstance(props, dict):
                continue
            if props.get("repository_id") != repository:
                continue
            if ref is not None and props.get("ref_name") != ref:
                continue
            status = props.get("status")
            if isinstance(status, str) and status != "live":
                continue
            found.append((node_id, props))
        if len(rows) < _PAGE_SIZE:
            return found
        cursor = rows[-1][0]
    raise OperationRefused("UNAVAILABLE", {"reason": "branch scan limit exceeded"})


async def _live_file_versions(
    context: Any, graph: str, branch_id: str
) -> list[tuple[str, dict[str, Any]]]:
    """Live (fileversion id, properties) of one branch, in stable id order.

    Every successor of a Branch is a membership edge target; the stored
    ``removesFileVersion`` tombstones are removals, never stored edges, so
    the branch's successors are exactly its live file versions.
    """
    ids = _rows(await _send(context, graph, "GetSuccessors", {"node_id": branch_id}))
    if not ids:
        return []
    versions: list[tuple[str, dict[str, Any]]] = []
    for start in range(0, len(ids), _BATCH_SIZE):
        chunk = ids[start : start + _BATCH_SIZE]
        rows = _rows(
            await _send(context, graph, "GetNodePropertiesBatch", {"node_ids": chunk})
        )
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                continue
            node_id, blob = row
            props = _decode_properties(blob)
            if isinstance(node_id, str) and props is not None:
                versions.append((node_id, props))
    versions.sort(key=lambda pair: pair[0])
    return versions


async def _membership_revisions(
    context: Any,
    graph: str,
    branch_id: str,
    fileversion_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Membership-edge properties of one branch's live file versions."""
    revisions: dict[str, dict[str, Any]] = {}
    pairs = [(branch_id, fv_id) for fv_id in fileversion_ids]
    for start in range(0, len(pairs), _BATCH_SIZE):
        chunk = pairs[start : start + _BATCH_SIZE]
        rows = _rows(
            await _send(
                context,
                graph,
                "GetEdgePropertiesBatch",
                {"edges": [[source, target] for source, target in chunk]},
            )
        )
        if len(rows) != len(chunk):
            raise OperationRefused("UNAVAILABLE", {"reason": "engine result malformed"})
        for (source, target), blobs in zip(chunk, rows, strict=True):
            if source != branch_id:
                continue
            # One property blob per stored edge of the pair (an empty inner
            # list means the edge is absent); the projection stores exactly
            # one membership edge per live file version.
            for blob in blobs if isinstance(blobs, list) else []:
                props = _decode_properties(blob)
                if (
                    isinstance(props, dict)
                    and props.get("relationship") == "hasFileVersion"
                ):
                    revisions[target] = props
                    break
    return revisions


def _not_found_tree(
    tenant: str, repository: str, ref: str, path: str, reason: str
) -> SourceTreeOutcome:
    return SourceTreeOutcome(
        tenant=tenant,
        repository=repository,
        ref=ref,
        path=path,
        found=False,
        reason=reason,
    )


class NativeSourceReader:
    """One stateless reader shared by the serving root, bound afresh per call."""

    async def tree(
        self,
        *,
        repository: str,
        ref: str,
        path: str,
        context: ExecutionContext,
    ) -> SourceTreeOutcome:
        """Immediate children of one directory on one ref, in path order.

        The listing contains no directory nodes: the engine projection stores
        files only, so each directory entry is derived from the file paths
        that pass through it. A listed file carries its content digest and
        the membership edge's last-touch revision.
        """
        _require_scopes(context, TREE_SCOPES)
        graph = await _caller_dispatch_graph(context)
        tenant = context.caller.tenant
        branches = await _branch_nodes(context, graph, repository=repository, ref=ref)
        if not branches:
            return _not_found_tree(tenant, repository, ref, path, "ref-not-found")
        branch_id, _branch_props = branches[0]
        prefix = path + "/" if path else ""
        versions = await _live_file_versions(context, graph, branch_id)
        revisions = await _membership_revisions(
            context, graph, branch_id, [fv_id for fv_id, _props in versions]
        )
        children: dict[str, SourceTreeEntry] = {}
        for fv_id, props in versions:
            file_path = props.get("path")
            if not isinstance(file_path, str) or not file_path.startswith(prefix):
                continue
            rest = file_path[len(prefix) :]
            if not rest:
                continue
            if "/" in rest:
                child = rest.split("/", 1)[0]
                if child not in children:
                    children[child] = SourceTreeEntry(
                        path=f"{prefix}{child}", kind="directory"
                    )
            else:
                membership = revisions.get(fv_id)
                revision = (
                    membership.get("revision_id")
                    if isinstance(membership, dict)
                    else None
                )
                digest = props.get("content_digest")
                children[file_path] = SourceTreeEntry(
                    path=file_path,
                    kind="file",
                    content_digest=digest if isinstance(digest, str) else None,
                    revision_id=revision if isinstance(revision, str) else None,
                )
        return SourceTreeOutcome(
            tenant=tenant,
            repository=repository,
            ref=ref,
            path=path,
            found=True,
            entries=tuple(sorted(children.values(), key=lambda entry: entry.path)),
        )

    async def blob(
        self,
        *,
        repository: str,
        ref: str,
        path: str,
        context: ExecutionContext,
    ) -> SourceBlobOutcome:
        """One file's content on one ref, fetched chunk by chunk from the CAS."""
        _require_scopes(context, BLOB_SCOPES)
        graph = await _caller_dispatch_graph(context)
        tenant = context.caller.tenant
        branches = await _branch_nodes(context, graph, repository=repository, ref=ref)
        if not branches:
            return SourceBlobOutcome(
                tenant=tenant,
                repository=repository,
                ref=ref,
                path=path,
                found=False,
                reason="ref-not-found",
            )
        branch_id, _branch_props = branches[0]
        versions = await _live_file_versions(context, graph, branch_id)
        digest: str | None = None
        for _fv_id, props in versions:
            if props.get("path") == path:
                candidate = props.get("content_digest")
                if isinstance(candidate, str) and candidate:
                    digest = candidate
                break
        if digest is None:
            return SourceBlobOutcome(
                tenant=tenant,
                repository=repository,
                ref=ref,
                path=path,
                found=False,
                reason="path-not-found",
            )
        begun = _rows(await _send(context, graph, "BlobFetchBegin", {"digest": digest}))
        if not isinstance(begun, (list, tuple)) or len(begun) != 2:
            raise OperationRefused("UNAVAILABLE", {"reason": "engine result malformed"})
        cursor, chunks = begun
        if not isinstance(cursor, int) or not isinstance(chunks, int):
            raise OperationRefused("UNAVAILABLE", {"reason": "engine result malformed"})
        content = b""
        try:
            for index in range(chunks):
                part = await _send(
                    context,
                    graph,
                    "BlobChunkGet",
                    {"cursor": cursor, "idx": index},
                )
                if isinstance(part, (bytes, bytearray, memoryview)):
                    content += bytes(part)
        finally:
            await _send(context, graph, "BlobFetchEnd", {"cursor": cursor})
        return SourceBlobOutcome(
            tenant=tenant,
            repository=repository,
            ref=ref,
            path=path,
            found=True,
            content_digest=digest,
            content=content,
            size=len(content),
        )

    async def history(
        self, *, repository: str, path: str, context: ExecutionContext
    ) -> SourceHistoryOutcome:
        """Every live ref of one repository that carries one path.

        Per ref the entry reports the membership edge's last-touch revision
        and the file's content digest; refs whose live tree does not carry
        the path are absent. The full commit DAG is a write-path concern and
        is not served here.
        """
        _require_scopes(context, TREE_SCOPES)
        graph = await _caller_dispatch_graph(context)
        tenant = context.caller.tenant
        branches = await _branch_nodes(context, graph, repository=repository)
        entries: list[SourceHistoryEntry] = []
        for branch_id, branch_props in branches:
            versions = await _live_file_versions(context, graph, branch_id)
            matches = [
                (fv_id, props) for fv_id, props in versions if props.get("path") == path
            ]
            if not matches:
                continue
            revisions = await _membership_revisions(
                context,
                graph,
                branch_id,
                [fv_id for fv_id, _props in matches],
            )
            ref_name = branch_props.get("ref_name")
            if not isinstance(ref_name, str) or not ref_name:
                continue
            for fv_id, props in matches:
                membership = revisions.get(fv_id)
                revision = (
                    membership.get("revision_id")
                    if isinstance(membership, dict)
                    else None
                )
                if not isinstance(revision, str) or not revision:
                    continue
                digest = props.get("content_digest")
                entries.append(
                    SourceHistoryEntry(
                        ref=ref_name,
                        revision_id=revision,
                        content_digest=digest if isinstance(digest, str) else None,
                    )
                )
        entries.sort(key=lambda entry: (entry.ref, entry.revision_id))
        return SourceHistoryOutcome(
            tenant=tenant,
            repository=repository,
            path=path,
            entries=tuple(entries),
        )

    async def blame(
        self,
        *,
        repository: str,
        ref: str,
        path: str,
        context: ExecutionContext,
    ) -> SourceBlameOutcome:
        """One file's last-touch provenance on one ref.

        The current engine substrate records a per-file last-touch revision
        on the membership edge, never line-level attribution; the outcome
        says so instead of implying more.
        """
        _require_scopes(context, TREE_SCOPES)
        graph = await _caller_dispatch_graph(context)
        tenant = context.caller.tenant
        branches = await _branch_nodes(context, graph, repository=repository, ref=ref)
        if not branches:
            return SourceBlameOutcome(
                tenant=tenant,
                repository=repository,
                ref=ref,
                path=path,
                found=False,
                reason="ref-not-found",
            )
        branch_id, _branch_props = branches[0]
        versions = await _live_file_versions(context, graph, branch_id)
        for fv_id, props in versions:
            if props.get("path") != path:
                continue
            revisions = await _membership_revisions(context, graph, branch_id, [fv_id])
            membership = revisions.get(fv_id)
            revision = (
                membership.get("revision_id") if isinstance(membership, dict) else None
            )
            digest = props.get("content_digest")
            return SourceBlameOutcome(
                tenant=tenant,
                repository=repository,
                ref=ref,
                path=path,
                found=True,
                revision_id=revision if isinstance(revision, str) else None,
                content_digest=digest if isinstance(digest, str) else None,
            )
        return SourceBlameOutcome(
            tenant=tenant,
            repository=repository,
            ref=ref,
            path=path,
            found=False,
            reason="path-not-found",
        )


__all__ = ["NativeSourceReader"]

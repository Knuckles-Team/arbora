"""The production seams of the changeset ports (ARBORA-OPS-R894).

``ChangesetSeams`` is what the host supplies so ``changeset_submitter`` and ``gate_runner``
bind at host start. Everything here is read from the door, never held:

* the run's repository, branch and prefixes come from the **source grant** on its work
  item (``metadata["source_grant"]``), read from the claim the run owner carries; a run
  without a valid grant has no target and both ports stay unbound for it;
* blob staging streams content through the engine ``BlobBegin`` / ``BlobChunkPut`` /
  ``BlobCommit`` methods and answers the stored ``{digest, size}`` that ``source.commit``
  names; the digest is the engine's, never computed here;
* the source client is the SDK's ``SourceClient`` over a transport that serves its
  ``tree`` and ``blob`` reads from the registry's ``source.tree`` / ``source.blob`` as the
  run principal, which is what ``SourceMount`` reads through;
* a workspace backend is the sandbox only when its name is an isolated backend.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from arbora.agent_host.host_port_adapters import ChangesetSeams, RunCall, SourceTarget

SOURCE_GRANT_KEY = "source_grant"
#: Workspace backends that isolate execution from the host; ``local`` does not.
ISOLATED_BACKENDS = frozenset({"docker"})
BLOB_CHUNK_BYTES = 1 << 20
FILE_MODE = "100644"
DIRECTORY_MODE = "040000"

#: ``(method, params, tenant)`` to the decoded engine answer.
EngineSend = Callable[[str, dict[str, Any], str], Awaitable[Any]]


def source_target(claim: Any) -> SourceTarget | None:
    """The run's target from its work item's source grant, or ``None`` when it has none.

    The grant names one repository, one branch and one or more path prefixes; a grant
    that is malformed in any part is no grant.
    """
    metadata = claim.get("metadata") if isinstance(claim, Mapping) else None
    grant = metadata.get(SOURCE_GRANT_KEY) if isinstance(metadata, Mapping) else None
    if not isinstance(grant, Mapping):
        return None
    repository, branch = grant.get("repository"), grant.get("branch")
    prefixes = grant.get("prefixes")
    if (
        not isinstance(repository, str)
        or not repository
        or not isinstance(branch, str)
        or not branch
        or isinstance(prefixes, str)
        or not isinstance(prefixes, Sequence)
        or not prefixes
        or not all(isinstance(prefix, str) and prefix for prefix in prefixes)
    ):
        return None
    return SourceTarget(repository, branch, tuple(prefixes))


def is_isolated_backend(backend: Any) -> bool:
    """Whether ``backend`` is an isolated sandbox backend, by its declared name."""
    return getattr(backend, "name", None) in ISOLATED_BACKENDS


class ServedSourceTransport:
    """``scion_sdk.source.client.SourceTransport`` over the served ``source.*`` reads.

    Calls run as the run principal through the registry ``invoke``. A read the door
    refuses, or an answer that lacks what the SDK needs (a head revision), is
    ``refused`` so the SDK reports ``transport_unavailable``; nothing is invented.
    """

    def __init__(self, call: RunCall, run_ref: str) -> None:
        self._call, self._run_ref = call, run_ref

    async def call(
        self, operation: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if operation == "tree":
            return await self._tree(payload)
        if operation == "blob":
            return await self._blob(payload)
        return {"status": "refused", "reason": "unsupported"}

    async def _read(
        self, op_id: str, params: dict[str, Any]
    ) -> Mapping[str, Any] | None:
        answered = await self._call(op_id, params, self._run_ref)
        if answered.get("outcome") != "answered":
            return None
        value = (answered.get("payload") or {}).get("value")
        if not isinstance(value, Mapping) or value.get("found") is not True:
            return None
        return value

    async def _tree(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        value = await self._read(
            "source.tree",
            {
                "repository": payload["repository_id"],
                "ref": payload["ref"],
                "path": payload.get("path_prefix") or "",
            },
        )
        revision = None if value is None else value.get("head_revision")
        if value is None or not isinstance(revision, str) or not revision:
            return {"status": "refused", "reason": "not_found"}
        entries = [
            {
                "path": entry["path"],
                "mode": FILE_MODE if entry["kind"] == "file" else DIRECTORY_MODE,
                "digest": entry.get("content_digest") or "",
                "size": int(entry.get("size") or 0),
            }
            for entry in value.get("entries", ())
        ]
        return {"status": "ok", "result": {"entries": entries, "revision": revision}}

    async def _blob(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        value = await self._read(
            "source.blob",
            {
                "repository": payload["repository_id"],
                "ref": payload["ref"],
                "path": payload["path"],
            },
        )
        if value is None:
            return {"status": "refused", "reason": "not_found"}
        content = value.get("content", "")
        raw = (
            base64.b64decode(content, validate=True)
            if value.get("content_encoding") == "base64"
            else str(content).encode("utf-8")
        )
        return {
            "status": "ok",
            "result": {"content_base64": base64.b64encode(raw).decode("ascii")},
        }


def _scalar(answer: Any, name: str) -> str:
    """The cursor or digest the engine answered, as text; anything else is a refusal."""
    found = answer.get(name) if isinstance(answer, Mapping) else answer
    if not isinstance(found, str) or not found:
        raise ValueError(f"the engine answered no blob {name}")
    return found


def engine_blob_stager(
    send: EngineSend, tenant_of: Callable[[str], str]
) -> Callable[[str, bytes], Awaitable[Mapping[str, Any]]]:
    """``stage_blob(run_ref, data)``: stream ``data`` into the engine blob store.

    Each step carries an idempotency-stable position, so a replayed stage appends nothing
    twice. The answered digest is the engine's commit of the stream.
    """

    async def stage(run_ref: str, data: bytes) -> Mapping[str, Any]:
        tenant = tenant_of(run_ref)
        if not tenant:
            raise PermissionError("the run has no tenant to stage a blob for")
        cursor = _scalar(
            await send("BlobBegin", {"chunk_size": BLOB_CHUNK_BYTES}, tenant), "cursor"
        )
        for offset in range(0, len(data), BLOB_CHUNK_BYTES):
            await send(
                "BlobChunkPut",
                {"cursor": cursor, "data": data[offset : offset + BLOB_CHUNK_BYTES]},
                tenant,
            )
        digest = _scalar(await send("BlobCommit", {"cursor": cursor}, tenant), "digest")
        return {"digest": digest, "size": len(data)}

    return stage


def production_changeset_seams(
    call: RunCall,
    send: EngineSend,
    resolve_run: Callable[[str], Any],
    *,
    client_for: Callable[[Any], Any] | None = None,
) -> ChangesetSeams:
    """The host's ``ChangesetSeams``, read from the run owner and the door."""

    def owner_of(run_ref: str) -> Any:
        try:
            owner = resolve_run(run_ref)
        except RuntimeError:
            return None
        return owner if owner is not None and owner.run_id == run_ref else None

    def target(run_ref: str) -> SourceTarget | None:
        owner = owner_of(run_ref)
        return None if owner is None else source_target(owner.claim)

    def tenant_of(run_ref: str) -> str:
        owner = owner_of(run_ref)
        return "" if owner is None else str(owner.session.tenant)

    def source_client(run_ref: str) -> Any:
        from scion_sdk.source.client import SourceClient

        transport = ServedSourceTransport(call, run_ref)
        return (client_for or SourceClient)(transport)

    return ChangesetSeams(
        target=target,
        source_client=source_client,
        stage_blob=engine_blob_stager(send, tenant_of),
        is_sandbox=is_isolated_backend,
    )

"""Served prompt library operations over the engine KV modality (ARBORA-OPS-R912).

A prompt is a head record plus append-only version records in the EG KV
namespace ``prompts``, under the caller's own verified session:

- ``<id>/head``: ``{name, current, updated_at}``; its EG version is the
  prompt's ``revision`` (compare-and-set on update and rollback).
- ``<id>/v/<number>``: ``{body, author, created_at}``, written once.

``prompts.rollback`` never rewrites history: it appends a new version that
copies the chosen one. The engine holds every fact; arbora keeps none.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import Field

from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops.memory import _decode, _encode, _payload, _send
from arbora.api.registry import OpSpec

#: The EG KV namespace that holds every prompt record.
EG_NAMESPACE = "prompts"
_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$"
_READ_SCOPE = "kv:read"
_WRITE_SCOPE = "kv:write"


class PromptIdParams(Params):
    id: str = Field(pattern=_ID)


class PromptListParams(Params):
    cursor: str | None = Field(default=None, max_length=1_024)
    limit: int = Field(default=50, ge=1, le=100)


class PromptCreateParams(PromptIdParams):
    name: str = Field(min_length=1, max_length=256)
    body: str = Field(max_length=262_000)


class PromptUpdateParams(PromptIdParams):
    body: str = Field(max_length=262_000)
    expected_revision: int = Field(ge=1)


class PromptRollbackParams(PromptIdParams):
    version_id: int = Field(ge=1)
    expected_revision: int = Field(ge=1)


class PromptDiffParams(PromptIdParams):
    a: int = Field(ge=1)
    b: int = Field(ge=1)


class PromptResult(Params):
    value: dict[str, Any]


def _refuse(code: str, details: Mapping[str, Any] | None = None) -> Exception:
    from arbora.api.invoke.pipeline import OperationRefused

    return OperationRefused(code, dict(details or {}))


def _target(key: str) -> dict[str, Any]:
    return {"namespace": EG_NAMESPACE, "key": key, "shared": False}


def _key(context: Any, suffix: str) -> str | None:
    base = getattr(context, "idempotency_key", None)
    return f"{base}:{suffix}" if base else None


def _author(context: Any) -> str:
    return str(getattr(context.caller, "principal", ""))


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _get(context: Any, key: str) -> tuple[dict[str, Any], int] | None:
    entry = _payload(await _send(context, "KvGet", _target(key), None))
    if not isinstance(entry, Mapping) or "value" not in entry:
        return None
    return _decode(entry["value"]), int(entry.get("version", 0))


async def _head(context: Any, prompt_id: str) -> tuple[dict[str, Any], int]:
    head = await _get(context, f"{prompt_id}/head")
    if head is None:
        raise _refuse("NOT_FOUND")
    return head


async def _version(context: Any, prompt_id: str, number: int) -> dict[str, Any]:
    found = await _get(context, f"{prompt_id}/v/{number}")
    if found is None:
        raise _refuse("NOT_FOUND", {"version_id": number})
    return found[0]


async def _cas_head(
    context: Any, prompt_id: str, expected: int, head: Mapping[str, Any]
) -> int:
    receipt = _payload(
        await _send(
            context,
            "KvCas",
            {
                **_target(f"{prompt_id}/head"),
                "expected_version": expected,
                "new": _encode(head),
            },
            _key(context, "head"),
        )
    )
    if not (isinstance(receipt, Mapping) and receipt.get("swapped")):
        raise _refuse("REVISION_CONFLICT", {"expected_revision": expected})
    return int(receipt.get("version", 0))


async def _append(context: Any, prompt_id: str, number: int, body: str) -> None:
    record = {"body": body, "author": _author(context), "created_at": _now()}
    await _send(
        context,
        "KvCas",
        {
            **_target(f"{prompt_id}/v/{number}"),
            "expected_version": 0,
            "new": _encode(record),
        },
        _key(context, f"v{number}"),
    )


async def _advance(
    context: Any, prompt_id: str, expected: int, body: str
) -> dict[str, Any]:
    head, _revision = await _head(context, prompt_id)
    number = int(head.get("current", 0)) + 1
    head = {**head, "current": number, "updated_at": _now()}
    revision = await _cas_head(context, prompt_id, expected, head)
    await _append(context, prompt_id, number, body)
    return {"value": {"id": prompt_id, **head, "revision": revision}}


async def handle_prompts_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """List prompt heads (one row per prompt)."""
    request: dict[str, Any] = {
        "namespace": EG_NAMESPACE,
        "prefix": "",
        "limit": int(params.get("limit") or 50),
        "shared": False,
    }
    if params.get("cursor"):
        request["cursor"] = params["cursor"]
    page = _payload(await _send(context, "KvScan", request, None))
    rows = page.get("entries", []) if isinstance(page, Mapping) else []
    prompts = [
        {
            "id": str(row["key"]).removesuffix("/head"),
            **_decode(row.get("value")),
            "revision": int(row.get("version", 0)),
        }
        for row in rows
        if isinstance(row, Mapping) and str(row.get("key", "")).endswith("/head")
    ]
    cursor = page.get("next_cursor") if isinstance(page, Mapping) else None
    return {"value": {"prompts": prompts, "next_cursor": cursor}}


async def handle_prompts_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Read one prompt with the body of its current version."""
    prompt_id = params["id"]
    head, revision = await _head(context, prompt_id)
    current = await _version(context, prompt_id, int(head.get("current", 0)))
    return {
        "value": {
            "id": prompt_id,
            **head,
            "revision": revision,
            "body": current.get("body", ""),
        }
    }


async def handle_prompts_create(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Create a prompt at version 1; an existing id is a revision conflict."""
    prompt_id = params["id"]
    head = {"name": params["name"], "current": 1, "updated_at": _now()}
    revision = await _cas_head(context, prompt_id, 0, head)
    await _append(context, prompt_id, 1, params["body"])
    return {"value": {"id": prompt_id, **head, "revision": revision}}


async def handle_prompts_update(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Append a new version at the caller's expected revision."""
    return await _advance(
        context, params["id"], int(params["expected_revision"]), params["body"]
    )


async def handle_prompts_rollback(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Append a new version that copies ``version_id``; history is kept."""
    old = await _version(context, params["id"], int(params["version_id"]))
    return await _advance(
        context,
        params["id"],
        int(params["expected_revision"]),
        str(old.get("body", "")),
    )


async def handle_prompts_versions(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """List every version of one prompt, oldest first."""
    prompt_id = params["id"]
    head, _revision = await _head(context, prompt_id)
    versions = []
    for number in range(1, int(head.get("current", 0)) + 1):
        record = await _version(context, prompt_id, number)
        versions.append(
            {
                "version_id": number,
                "created_at": record.get("created_at"),
                "author": record.get("author"),
            }
        )
    return {"value": {"id": prompt_id, "versions": versions}}


async def handle_prompts_diff(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Unified diff of two versions' bodies."""
    prompt_id, a, b = params["id"], int(params["a"]), int(params["b"])
    left = str((await _version(context, prompt_id, a)).get("body", ""))
    right = str((await _version(context, prompt_id, b)).get("body", ""))
    diff = "".join(
        difflib.unified_diff(
            left.splitlines(keepends=True),
            right.splitlines(keepends=True),
            fromfile=f"v{a}",
            tofile=f"v{b}",
        )
    )
    return {"value": {"id": prompt_id, "a": a, "b": b, "diff": diff}}


def _read(op_id: str, summary: str, example: str, params: type[Params]) -> OpSpec:
    return build_read_op(
        op_id=op_id,
        summary=summary,
        examples=(example,),
        params=params,
        result=PromptResult,
        handler=f"arbora.api.ops.prompts.handle_{op_id.replace('.', '_')}",
        scope=_READ_SCOPE,
    )


def _write(op_id: str, summary: str, example: str, params: type[Params]) -> OpSpec:
    return build_write_op(
        op_id=op_id,
        summary=summary,
        examples=(example,),
        params=params,
        result=PromptResult,
        handler=f"arbora.api.ops.prompts.handle_{op_id.replace('.', '_')}",
        scope=_WRITE_SCOPE,
    )


def operations() -> tuple[OpSpec, ...]:
    return (
        _read(
            "prompts.list",
            "List the caller's prompts",
            "list my prompts",
            PromptListParams,
        ),
        _read(
            "prompts.get",
            "Read one prompt and its current body",
            "show this prompt",
            PromptIdParams,
        ),
        _read(
            "prompts.versions",
            "List one prompt's versions",
            "prompt history",
            PromptIdParams,
        ),
        _read(
            "prompts.diff",
            "Diff two versions of one prompt",
            "diff prompt versions",
            PromptDiffParams,
        ),
        _write(
            "prompts.create",
            "Create a prompt at version 1",
            "save a new prompt",
            PromptCreateParams,
        ),
        _write(
            "prompts.update",
            "Append a new prompt version",
            "edit this prompt",
            PromptUpdateParams,
        ),
        _write(
            "prompts.rollback",
            "Append a version copying an older one",
            "roll back this prompt",
            PromptRollbackParams,
        ),
    )


specs = operations

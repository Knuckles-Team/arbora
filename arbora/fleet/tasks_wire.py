"""The client-side wire of the MCP Tasks extension: ids, delegation proofs and request shapes.

The multiplexer signs and shapes every Tasks request it forwards to a child, and the
served Tasks extension (``arbora.mcp_server.backing.tasks_extension``) reads the same
shapes, so both import this module from below. ``RequestParams`` and ``Result`` bind
the installed MCP types, or a pydantic stand-in on an image that predates them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import unicodedata
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import ConfigDict, Field, field_validator

from arbora.security.run_token import RunTokenSigner

if TYPE_CHECKING:
    from mcp_types import RequestParams, Result
else:
    try:
        from mcp_types import RequestParams, Result
    except ImportError:
        from pydantic import BaseModel as _CompatBaseModel

        class RequestParams(_CompatBaseModel):
            """Stand-in for ``mcp_types.RequestParams`` (fastmcp<4)."""

        class Result(_CompatBaseModel):
            """Stand-in for ``mcp_types.Result`` (fastmcp<4)."""


TASKS_EXTENSION_ID = "io.modelcontextprotocol/tasks"
# The revision is the wire contract revision used by this extension.  It is
# intentionally separate from the MCP protocol version: the protocol version
# gates method registration, while this value lets a multiplexer reject a
# route from a server that implements a different task projection.
TASKS_EXTENSION_REVISION = "2c1425d9a288b9b1f489430fe1e00bb392b47e48"


# Native MCP task requests are control-plane messages, not a bulk payload
# channel.  Keep these limits aligned with the fleet's general tool-payload
# policy (64 KiB / 4,096 items / depth 24), while applying the tighter 512-byte
# identifier bound that is sufficient for every durable WorkItem namespace.
_MAX_TASK_ID_BYTES = 512
_MAX_TASK_INPUT_BYTES = 64 * 1024
_MAX_TASK_INPUT_ITEMS = 4_096
_MAX_TASK_INPUT_DEPTH = 24
_MAX_TASK_INPUT_STRING_BYTES = 16 * 1024


def channel_proof(secret: str, token: str) -> str:
    """Bind one task proof to the private per-child-generation channel."""

    return hmac.new(
        secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _delegation_payload(
    method: str,
    params: Mapping[str, Any],
    *,
    server: str,
    revision: str,
    caller: Mapping[str, Any],
) -> bytes:
    """Canonical bytes bound by the multiplexer service bearer.

    The task request body is hashed without its ``_meta`` envelope; the
    method, owning server, exact extension revision, and normalized caller are
    all covered by the resulting run-token binding. This prevents replaying a
    valid route for another task or owner while keeping secrets out of the MCP
    metadata itself.
    """

    body = {
        "caller": {
            "owner": str(caller.get("owner") or ""),
            "scopes": sorted(str(scope) for scope in caller.get("scopes", ())),
            "tenant": str(caller.get("tenant") or ""),
        },
        "method": method,
        "params": {str(key): value for key, value in params.items() if key != "_meta"},
        "revision": revision,
        "server": server,
    }
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def delegation_binding(
    method: str,
    params: Mapping[str, Any],
    *,
    server: str,
    revision: str,
    caller: Mapping[str, Any],
) -> str:
    return hashlib.sha256(
        _delegation_payload(
            method,
            params,
            server=server,
            revision=revision,
            caller=caller,
        )
    ).hexdigest()


def bound_signer(signer: RunTokenSigner | None) -> RunTokenSigner:
    """The host's signer, or a refusal when this server was built without one."""
    if signer is None:
        raise RuntimeError("no run-token signer is bound to this server")
    return signer


def mint_delegation_token(
    signer: RunTokenSigner,
    method: str,
    params: Mapping[str, Any],
    *,
    server: str,
    revision: str,
    caller: Mapping[str, Any],
) -> str:
    """Mint a short-lived, shared-secret run token for one task request."""

    binding = delegation_binding(
        method,
        params,
        server=server,
        revision=revision,
        caller=caller,
    )
    return signer.mint(
        f"mcp-task:{binding}",
        project=server,
        endpoints=(server,),
        operations=(method,),
        ttl_seconds=30.0,
        actor_id=str(caller.get("owner") or ""),
        tenant_id=str(caller.get("tenant") or ""),
    )


def _is_scalar_task_input(current: Any) -> bool:
    """``True`` (having validated it) for None/bool/int/float/str; raises on
    an invalid scalar value; ``False`` when ``current`` is a container
    (dict/list) or something else the caller must reject."""
    if current is None or isinstance(current, bool):
        return True
    if isinstance(current, int | float):
        if isinstance(current, float) and not math.isfinite(current):
            raise ValueError("task input contains a non-JSON value")
        return True
    if isinstance(current, str):
        if len(current.encode("utf-8")) > _MAX_TASK_INPUT_STRING_BYTES:
            raise ValueError("task input string exceeds the size limit")
        return True
    return False


def _enter_task_input_container(current: Any, items: int, seen: set[int]) -> None:
    """Reject a cycle or an item-count overflow before walking a container."""
    identity = id(current)
    if identity in seen:
        raise ValueError("task input contains a cycle")
    seen.add(identity)
    if len(current) > _MAX_TASK_INPUT_ITEMS - items:
        raise ValueError("task input exceeds the maximum item count")


def _push_task_input_dict_children(
    current: dict[Any, Any],
    depth: int,
    items: int,
    seen: set[int],
    stack: list[tuple[Any, int]],
) -> None:
    _enter_task_input_container(current, items, seen)
    for key, child in current.items():
        if not isinstance(key, str):
            raise ValueError("task input object keys must be strings")
        stack.append((child, depth + 1))
        stack.append((key, depth + 1))


def _push_task_input_list_children(
    current: list[Any],
    depth: int,
    items: int,
    seen: set[int],
    stack: list[tuple[Any, int]],
) -> None:
    _enter_task_input_container(current, items, seen)
    stack.extend((child, depth + 1) for child in current)


def _validate_task_input_node(
    current: Any,
    depth: int,
    items: int,
    seen: set[int],
    stack: list[tuple[Any, int]],
) -> None:
    if _is_scalar_task_input(current):
        return
    if isinstance(current, dict):
        _push_task_input_dict_children(current, depth, items, seen, stack)
        return
    if isinstance(current, list):
        _push_task_input_list_children(current, depth, items, seen, stack)
        return
    raise ValueError("task input contains a non-JSON value")


def _validate_task_input_encoded_size(value: Any) -> None:
    try:
        encoded_bytes = len(
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        raise ValueError("task input contains a non-JSON value") from None
    if encoded_bytes > _MAX_TASK_INPUT_BYTES:
        raise ValueError("task input exceeds the size limit")


def _validate_task_input_bounds(value: Any) -> None:
    """Reject oversized, deep, cyclic, or non-JSON task input values."""

    items = 0
    seen: set[int] = set()
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > _MAX_TASK_INPUT_DEPTH:
            raise ValueError("task input exceeds the maximum nesting depth")
        items += 1
        if items > _MAX_TASK_INPUT_ITEMS:
            raise ValueError("task input exceeds the maximum item count")
        _validate_task_input_node(current, depth, items, seen, stack)

    _validate_task_input_encoded_size(value)


def _validate_task_id(value: str) -> str:
    if not value or not value.strip():
        raise ValueError("taskId must not be blank")
    if value != value.strip():
        raise ValueError("taskId must be trimmed")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("taskId contains a control character")
    if len(value.encode("utf-8")) > _MAX_TASK_ID_BYTES:
        raise ValueError("taskId exceeds the size limit")
    return value


class GetTaskParams(RequestParams):
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(alias="taskId", max_length=_MAX_TASK_ID_BYTES)

    @field_validator("task_id")
    @classmethod
    def _bounded_task_id(cls, value: str) -> str:
        return _validate_task_id(value)


CancelTaskParams = GetTaskParams


class UpdateTaskParams(RequestParams):
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(alias="taskId", max_length=_MAX_TASK_ID_BYTES)
    input_responses: dict[str, Any] = Field(
        alias="inputResponses", max_length=_MAX_TASK_INPUT_ITEMS
    )

    @field_validator("task_id")
    @classmethod
    def _bounded_task_id(cls, value: str) -> str:
        return _validate_task_id(value)

    @field_validator("input_responses")
    @classmethod
    def _bounded_input_responses(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_task_input_bounds(value)
        return value


class GetTaskResult(Result):
    """``GetTaskResult`` (SEP-2663): flat task fields plus exactly one of
    ``result``/``error``/``input_requests`` depending on ``status``."""

    model_config = ConfigDict(populate_by_name=True)

    result_type: str = Field(default="complete", serialization_alias="resultType")
    task_id: str = Field(serialization_alias="taskId")
    status: str
    created_at: str = Field(serialization_alias="createdAt")
    last_updated_at: str = Field(serialization_alias="lastUpdatedAt")
    ttl_ms: float | None = Field(default=None, serialization_alias="ttlMs")
    status_message: str | None = Field(
        default=None, serialization_alias="statusMessage"
    )
    poll_interval_ms: float | None = Field(
        default=1_000, serialization_alias="pollIntervalMs"
    )
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    input_requests: dict[str, Any] | None = Field(
        default=None, serialization_alias="inputRequests"
    )


class AckResult(Result):
    """Acknowledgement shape for ``tasks/update``/``tasks/cancel``."""

    model_config = ConfigDict(populate_by_name=True)

    result_type: str = Field(default="complete", serialization_alias="resultType")
    task_id: str | None = Field(default=None, serialization_alias="taskId")
    status: str | None = None
    status_message: str | None = Field(
        default=None, serialization_alias="statusMessage"
    )
    created_at: str | None = Field(default=None, serialization_alias="createdAt")
    last_updated_at: str | None = Field(
        default=None, serialization_alias="lastUpdatedAt"
    )
    ttl_ms: float | None = Field(default=None, serialization_alias="ttlMs")
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")

"""Assistant profiles and document sets served from the engine (ARBORA-OPS-R897.6).

``assistant.*`` forwards the engine ``AssistantProfile`` method. The engine
serves every operation of the method under the one scope ``profile:write``, so
the reads carry it as well. Arbora fills the tenant and the owner from the
verified caller and reads no profile content; the engine checks the profile-use
grant and the compare-and-set revision.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field, model_validator

from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops.engine_calls import engine_send
from arbora.api.ops.knowledge import EngineReply
from arbora.api.registry import OpSpec, Verb

SCOPE = "profile:write"
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "assistant.profile.put": "medulla:AssistantProfile",
    "assistant.document_set.put": "medulla:AssistantProfile",
}
_SCHEMA_VERSION = 1
_Id = Field(min_length=1, max_length=256)
_Share = dict[str, str]


class ProfilePutParams(Params):
    profile_id: str = _Id
    name: str = Field(min_length=1, max_length=256)
    instructions: str = Field(max_length=65536)
    document_set_ids: list[str] = Field(default_factory=list, max_length=64)
    tool_allowlist: list[str] = Field(default_factory=list, max_length=256)
    preferred_model_profile_refs: list[str] = Field(default_factory=list, max_length=16)
    max_context_tokens: int | None = Field(default=None, ge=1)
    shares: list[_Share] = Field(default_factory=list, max_length=64)
    #: Compare-and-set: write only when the stored profile is at this revision.
    expected_revision: int = Field(default=0, ge=0)


class ProfileRefParams(Params):
    profile_id: str = _Id


class DocumentSetPutParams(Params):
    set_id: str = _Id
    name: str = Field(min_length=1, max_length=256)
    members: list[str] | None = Field(default=None, max_length=1024)
    saved_query: str | None = Field(default=None, min_length=1, max_length=8192)
    shares: list[_Share] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def _one_kind(self) -> DocumentSetPutParams:
        if (self.members is None) == (self.saved_query is None):
            raise ValueError("give exactly one of members or saved_query")
        return self


class DocumentSetRefParams(Params):
    set_id: str = _Id


class ListParams(Params):
    pass


async def _profile(context: Any, op: dict[str, Any], *, write: bool) -> dict[str, Any]:
    answer = await engine_send(
        context, "AssistantProfile", {"op": op}, idempotent=write
    )
    return {"value": answer}


async def handle_profile_put(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    caller = context.caller
    profile = {
        "profile_id": params["profile_id"],
        "tenant": str(caller.tenant),
        "name": params["name"],
        "instructions": params["instructions"],
        "document_set_ids": list(params.get("document_set_ids") or ()),
        "tool_allowlist": list(params.get("tool_allowlist") or ()),
        "model_hints": {
            "preferred_model_profile_refs": list(
                params.get("preferred_model_profile_refs") or ()
            ),
            "max_context_tokens": params.get("max_context_tokens"),
        },
        "owner_principal": str(caller.principal),
        "shares": list(params.get("shares") or ()),
        "revision": params.get("expected_revision", 0),
        "schema_version": _SCHEMA_VERSION,
    }
    return await _profile(
        context, {"op": "put_profile", "profile": profile}, write=True
    )


async def handle_profile_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    body = {"op": "get_profile", "profile_id": params["profile_id"]}
    return await _profile(context, body, write=False)


async def handle_profile_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _profile(context, {"op": "list_profiles"}, write=False)


async def handle_document_set_put(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    caller = context.caller
    kind = (
        {"members": list(params["members"])}
        if params.get("members") is not None
        else {"saved_query": params["saved_query"]}
    )
    document_set = {
        "set_id": params["set_id"],
        "tenant": str(caller.tenant),
        "name": params["name"],
        "kind": kind,
        "owner_principal": str(caller.principal),
        "shares": list(params.get("shares") or ()),
        "schema_version": _SCHEMA_VERSION,
    }
    body = {"op": "put_document_set", "set": document_set}
    return await _profile(context, body, write=True)


async def handle_document_set_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    body = {"op": "get_document_set", "set_id": params["set_id"]}
    return await _profile(context, body, write=False)


async def handle_document_set_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _profile(context, {"op": "list_document_sets"}, write=False)


def operations() -> tuple[OpSpec, ...]:
    h = "arbora.api.ops.assistants.handle_"
    write = lambda op_id, summary, model, handler: build_write_op(  # noqa: E731
        verb=Verb.WRITE,
        op_id=op_id,
        summary=summary,
        examples=(summary.lower(),),
        params=model,
        result=EngineReply,
        handler=h + handler,
        scope=SCOPE,
    )
    read = lambda op_id, verb, summary, model, handler: build_read_op(  # noqa: E731
        verb=verb,
        op_id=op_id,
        summary=summary,
        examples=(summary.lower(),),
        params=model,
        result=EngineReply,
        handler=h + handler,
        scope=SCOPE,
    )
    return (
        write(
            "assistant.profile.put",
            "Save one assistant profile",
            ProfilePutParams,
            "profile_put",
        ),
        read(
            "assistant.profile.get",
            Verb.ASK,
            "Read one assistant profile",
            ProfileRefParams,
            "profile_get",
        ),
        read(
            "assistant.profile.list",
            Verb.FIND,
            "List assistant profiles",
            ListParams,
            "profile_list",
        ),
        write(
            "assistant.document_set.put",
            "Save one document set",
            DocumentSetPutParams,
            "document_set_put",
        ),
        read(
            "assistant.document_set.get",
            Verb.ASK,
            "Read one document set",
            DocumentSetRefParams,
            "document_set_get",
        ),
        read(
            "assistant.document_set.list",
            Verb.FIND,
            "List document sets",
            ListParams,
            "document_set_list",
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "handle_document_set_get",
    "handle_document_set_list",
    "handle_document_set_put",
    "handle_profile_get",
    "handle_profile_list",
    "handle_profile_put",
    "operations",
    "specs",
]

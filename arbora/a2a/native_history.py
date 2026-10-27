"""Trusted terminal-only native history composition; no transcript store."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import TYPE_CHECKING, Any

from arbora.api.invoke.pipeline import OperationRefused

if TYPE_CHECKING:
    from arbora.agent_runs import NativeRunContext

MAX_HISTORY_BYTES = 65_536
MAX_HISTORY_TURNS = 128


def unavailable() -> OperationRefused:
    return OperationRefused("UNAVAILABLE", {"capability": "completed_text_history"})


def completed_checkpoint(run_id: str, user: str, assistant: str) -> dict[str, Any]:
    """Encode the native own-turn profile, not accumulated or caller history."""
    if not all(
        isinstance(value, str) and value.strip() for value in (run_id, user, assistant)
    ):
        raise unavailable()
    try:
        if any(
            len(value.encode("utf-8")) > MAX_HISTORY_BYTES
            for value in (run_id, user, assistant)
        ):
            raise unavailable()
    except UnicodeError as exc:
        raise unavailable() from exc
    content = json.dumps(
        {
            "version": 1,
            "phase": "completed",
            "run_id": run_id,
            "messages": [
                {"role": "user", "text": user},
                {"role": "assistant", "text": assistant},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(content) > MAX_HISTORY_BYTES:
        raise unavailable()
    return {
        "version": 1,
        "user_text": user,
        "assistant_text": assistant,
        "content_digest": hashlib.sha256(content).hexdigest(),
    }


def _require_owner(request: Any, owner: NativeRunContext) -> None:
    from arbora.agent_runs import current_native_run

    if owner is None or current_native_run(request) is not owner:
        raise unavailable()
    owner.session.ensure_authority_current()
    owner.session.require_scope("kg:read")
    expiry = owner.claim.get("lease_expires_at_ms")
    if (
        owner.claim.get("fresh_execution_grant") is not True
        or type(expiry) is not int
        or expiry <= time.time_ns() // 1_000_000
        or owner.claim.get("work_item_id") != owner.work_item_id
        or not owner.owner_graph
    ):
        raise unavailable()


async def native_read(
    engine: Any, session: Any, graph: str, host: Any, method: str, args: dict[str, Any]
) -> Any:
    """Use generated native calls under the existing operation and caller gates."""
    from arbora.api.mcp.caller import caller_from_session
    from arbora.current_plan_publication import _session_client_scope
    from arbora.mcp_server.task_plan_runtime import _admit

    if host is None:
        raise unavailable()
    try:
        from epistemic_graph.generated import coordination

        send = getattr(coordination, f"send_{method}")
        decode = getattr(coordination, f"decode_{method}")
        if method == "get_run_work_item":
            from epistemic_graph.generated.models import (
                MethodGetRunWorkItemParams,
                RunWorkItemView,
            )

            if (
                "completed_text_history" not in MethodGetRunWorkItemParams.model_fields
                or "checkpoint_selection" not in RunWorkItemView.model_fields
            ):
                raise AttributeError("completed history read opt-in")
            args = {**args, "completed_text_history": True}
    except (ImportError, AttributeError) as exc:
        raise OperationRefused(
            "UPGRADE_REQUIRED", {"capability": "completed_text_history"}
        ) from exc
    operation = (
        "eg.coordination."
        + {
            "get_run_work_item": "GetRunWorkItem",
            "get_conversation_head": "GetConversationHead",
        }[method]
    )
    caller = caller_from_session(session, credential_kind="session")
    session.ensure_authority_current()
    await _admit(host, caller, operation)
    client = engine.graph_compute.for_graph(graph).async_client
    with _session_client_scope(client, session):
        response = await send(client, args, graph)
    session.ensure_authority_current()
    await _admit(host, caller, operation)
    value = decode(response)
    return None if value is None else value.model_dump(mode="json")


def _owned_view(view: Any, owner: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(view, dict):
        raise unavailable()
    admission = view.get("admission")
    item = view.get("work_item")
    if not isinstance(admission, dict) or not isinstance(item, dict):
        raise unavailable()
    context = admission.get("context")
    if not isinstance(context, dict):
        raise unavailable()
    conversation = admission.get("conversation")
    if (
        admission.get("run_id") != owner.run_id
        or item.get("work_item_id") != owner.work_item_id
        or admission.get("kind") != "orchestrator_task"
        or item.get("kind") != "orchestrator_task"
        or context.get("tenant_id") != owner.session.tenant
        or context.get("subject_id") != owner.session.actor.actor_id
        or context.get("graph") != owner.owner_graph
        or not isinstance(conversation, dict)
        or conversation.get("context_id") != owner.context_id
        or not isinstance(conversation.get("history_profile"), dict)
        or type(conversation["history_profile"].get("version")) is not int
        or conversation["history_profile"].get("version") != 1
        or not _digest_valid(conversation["history_profile"].get("user_text_digest"))
    ):
        raise unavailable()
    return admission, conversation


def _scope_matches(scope: Any, owner: Any) -> bool:
    return (
        isinstance(scope, dict)
        and scope.get("tenant") == "__shard__"
        and scope.get("scope") == {"kind": "graph", "graph": owner.owner_graph}
        and isinstance(scope.get("incarnation_id"), str)
        and bool(scope["incarnation_id"])
        and _digest_valid(scope.get("identity_digest"))
    )


def _digest_valid(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _references_valid(references: Any) -> bool:
    return (
        isinstance(references, list)
        and len(references) <= MAX_HISTORY_TURNS
        and all(
            isinstance(ref, dict)
            and set(ref) == {"document_id", "revision", "content_digest"}
            and isinstance(ref["document_id"], str)
            and bool(ref["document_id"])
            and type(ref["revision"]) is int
            and ref["revision"] == 1
            and _digest_valid(ref["content_digest"])
            for ref in references
        )
    )


def project_selection(view: Any, owner: Any) -> Any:
    from agent_utilities.harness.conversation_history import decode_completed_history
    from agent_utilities.host_ports import RunCheckpointSelection

    _, conversation = _owned_view(view, owner)
    selection = view.get("checkpoint_selection")
    if not isinstance(selection, dict) or set(selection) != {
        "version",
        "run_id",
        "work_item_id",
        "context_id",
        "admission_revision",
        "scope",
        "snapshot_version",
        "current_run_checkpoint",
        "completed_history",
        "checkpoints",
    }:
        raise unavailable()
    revision = conversation.get("expected_revision")
    if (
        type(selection["version"]) is not int
        or selection["version"] != 1
        or selection["run_id"] != owner.run_id
        or selection["work_item_id"] != owner.work_item_id
        or selection["context_id"] != owner.context_id
        or type(revision) is not int
        or revision < 0
        or type(selection["admission_revision"]) is not int
        or selection["admission_revision"] != revision + 1
        or type(selection["snapshot_version"]) is not int
        or selection["snapshot_version"] < 1
        or not _scope_matches(selection["scope"], owner)
    ):
        raise unavailable()
    # Presence wins before parsing any prior content, including malformed content.
    current = selection["current_run_checkpoint"]
    if current is not None:
        raise unavailable()
    content = selection["completed_history"]
    references = selection["checkpoints"]
    if not _references_valid(references) or len(references) != revision:
        raise unavailable()
    if revision == 0:
        if (
            content is not None
            or references
            or conversation.get("predecessor_run_id") is not None
        ):
            raise unavailable()
    else:
        if (
            not isinstance(content, str)
            or len(content.encode("utf-8")) > MAX_HISTORY_BYTES
        ):
            raise unavailable()
        decoded = decode_completed_history(content, run_id=owner.run_id)
        envelope = json.loads(content)
        if (
            envelope["run_id"] != conversation.get("predecessor_run_id")
            or len(decoded) > MAX_HISTORY_TURNS * 2
            or len(decoded) != len(references) * 2
        ):
            raise unavailable()
    return RunCheckpointSelection(owner.run_id, None, content)


async def select_run_history(request: Any) -> Any:
    from agent_utilities.host_ports import RunCheckpointSelection

    from arbora import agent_runs

    owner = agent_runs.current_native_run(request)
    if owner is None or not owner.completed_text_history:
        return RunCheckpointSelection(request.run_id, None, None)
    agent_runs.consume_native_history(request)
    _require_owner(request, owner)
    engine = agent_runs._engine()
    if engine is None:
        raise unavailable()
    graph = owner.owner_graph
    if not isinstance(graph, str):
        raise unavailable()
    view = await native_read(
        engine,
        owner.session,
        graph,
        owner.hosted_invocation,
        "get_run_work_item",
        {
            "tenant": owner.session.tenant,
            "run_id": owner.run_id,
            "selection": {
                "version": 1,
                "worker_id": owner.claim["lease_holder_ref"],
                "lease_epoch": owner.claim["lease_epoch"],
                "fencing_token": owner.claim["fencing_token"],
            },
        },
    )
    _require_owner(request, owner)
    _, conversation = _owned_view(view, owner)
    if (
        conversation["history_profile"].get("user_text_digest")
        != hashlib.sha256(request.task.encode("utf-8")).hexdigest()
    ):
        raise unavailable()
    return project_selection(view, owner)


async def prepare_admission(
    authority: Any,
    engine: Any,
    work_engine: Any,
    *,
    session: Any,
    run_id: str,
    context_id: str,
    request_digest: str,
    user_text: str,
    resume: bool,
) -> Any:
    """Resolve an immutable retry or one head candidate; never infer ordering."""
    from types import SimpleNamespace

    from arbora import work_items

    try:
        from epistemic_graph.generated.models import ConversationTurnAdmission

        if "history_profile" not in ConversationTurnAdmission.model_fields:
            raise AttributeError("history profile")
    except (ImportError, AttributeError) as exc:
        raise OperationRefused(
            "UPGRADE_REQUIRED", {"capability": "completed_text_history"}
        ) from exc
    graph = work_items.native_owner_graph(work_engine)
    binding = SimpleNamespace(
        session=session, owner_graph=graph, run_id=run_id, context_id=context_id
    )
    existing = await native_read(
        engine,
        session,
        graph,
        authority.hosted_invocation,
        "get_run_work_item",
        {"tenant": session.tenant, "run_id": run_id},
    )
    if authority._session("kg:write") != session:
        raise unavailable()
    if existing is not None:
        binding.work_item_id = existing.get("work_item", {}).get("work_item_id")
        admitted, conversation = _owned_view(existing, binding)
        if (
            admitted.get("metadata", {}).get("a2a_request_digest") != request_digest
            or conversation["history_profile"].get("user_text_digest")
            != hashlib.sha256(user_text.encode("utf-8")).hexdigest()
        ):
            raise unavailable()
        return ConversationTurnAdmission.model_validate_json(
            json.dumps(conversation), strict=True
        )
    head = await native_read(
        engine,
        session,
        graph,
        authority.hosted_invocation,
        "get_conversation_head",
        {"tenant": session.tenant, "context_id": context_id},
    )
    if authority._session("kg:write") != session:
        raise unavailable()
    if head is not None:
        if (
            not isinstance(head, dict)
            or type(head.get("version")) is not int
            or head["version"] != 1
            or head.get("context_id") != context_id
            or type(head.get("revision")) is not int
            or head["revision"] < 1
            or not isinstance(head.get("run_id"), str)
            or not head["run_id"]
            or not _scope_matches(head.get("scope"), binding)
        ):
            raise unavailable()
        revision, predecessor = head["revision"], head["run_id"]
    else:
        if resume or await asyncio.to_thread(
            authority._context_exists,
            session,
            context_id,
            authority._owner_ref(session),
        ):
            raise unavailable()
        if authority._session("kg:write") != session:
            raise unavailable()
        revision, predecessor = 0, None
    if (
        not isinstance(user_text, str)
        or not user_text.strip()
        or len(user_text.encode()) > MAX_HISTORY_BYTES
    ):
        raise unavailable()
    return ConversationTurnAdmission.model_validate_json(
        json.dumps(
            {
                "schema_version": "1",
                "context_id": context_id,
                "expected_revision": revision,
                "predecessor_run_id": predecessor,
                "history_profile": {
                    "version": 1,
                    "user_text_digest": hashlib.sha256(
                        user_text.encode("utf-8")
                    ).hexdigest(),
                },
            }
        ),
        strict=True,
    )


def completed_reply(
    value: Any, *, session: Any, task: Any, item: Any, owner_graph: str
) -> Any:
    """Read a private own-turn projection; this never grants execution authority."""
    from types import SimpleNamespace

    from .models import A2AArtifact, A2ATextPart

    owner = SimpleNamespace(
        run_id=item["metadata"]["a2a_run_id"],
        work_item_id=task.id,
        context_id=task.context_id,
        session=session,
        owner_graph=owner_graph,
    )
    _, conversation = _owned_view(value, owner)
    content = value.get("completed_checkpoint")
    if not isinstance(content, str):
        raise unavailable()
    try:
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_HISTORY_BYTES:
            raise unavailable()
        envelope = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise unavailable() from exc
    if not isinstance(envelope, dict) or set(envelope) != {
        "version",
        "phase",
        "run_id",
        "messages",
    }:
        raise unavailable()
    messages = envelope["messages"]
    if (
        type(envelope["version"]) is not int
        or envelope["version"] != 1
        or envelope["phase"] != "completed"
        or envelope["run_id"] != owner.run_id
        or not isinstance(messages, list)
        or len(messages) != 2
        or any(
            not isinstance(message, dict) or set(message) != {"role", "text"}
            for message in messages
        )
        or messages[0]["role"] != "user"
        or messages[1]["role"] != "assistant"
    ):
        raise unavailable()
    own = completed_checkpoint(owner.run_id, messages[0]["text"], messages[1]["text"])
    receipt = value.get("run_completion")
    if not isinstance(receipt, dict) or not isinstance(receipt.get("payload"), dict):
        raise unavailable()
    payload = receipt["payload"]
    reference = payload.get("completed_checkpoint")
    completion = payload.get("completion")
    if not isinstance(completion, dict):
        raise unavailable()
    if (
        value["work_item"].get("status") != "succeeded"
        or item.get("status") != "succeeded"
        or payload.get("outcome") != "succeeded"
        or payload.get("tenant") != session.tenant
        or payload.get("work_item_id") != task.id
        or completion.get("run_id") != owner.run_id
        or type(completion.get("schema_version")) is not int
        or completion.get("schema_version") != 1
        or not _references_valid([reference])
        or reference["document_id"] != f"inline-checkpoint:{owner.work_item_id}"
        or reference.get("content_digest") != own["content_digest"]
        or own["content_digest"] != hashlib.sha256(content.encode()).hexdigest()
        or conversation["history_profile"]["user_text_digest"]
        != hashlib.sha256(messages[0]["text"].encode()).hexdigest()
    ):
        raise unavailable()
    return [
        A2AArtifact(
            artifact_id=f"{task.id}:reply",
            name="reply",
            parts=[A2ATextPart(text=messages[1]["text"])],
        )
    ]

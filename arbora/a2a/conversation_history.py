"""Bounded previews from immutable native admissions, never client history."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from arbora import work_items

if TYPE_CHECKING:
    from .authority import WorkItemA2AAuthority


@dataclass(frozen=True)
class ConversationTurn:
    run_id: str
    task_id: str
    context_id: str
    revision: int
    user_text: str
    assistant_text: str | None


@dataclass(frozen=True)
class ConversationPreview:
    selected: ConversationTurn
    completed_prefix: tuple[ConversationTurn, ...]
    digest: str


def _unavailable(
    reason: str = "ordered native conversation history is unavailable",
) -> Exception:
    from .authority import A2AContextUnavailable

    return A2AContextUnavailable(reason)


def _admitted_turn(
    value: Any, *, run_id: str, session: Any, owner_ref: str, owner_graph: str
) -> Any:
    from epistemic_graph.generated.models import RunWorkItemView

    if value is None:
        raise _unavailable()
    view = RunWorkItemView.model_validate_json(json.dumps(value), strict=True)
    _validate_admission_scope(
        view,
        run_id=run_id,
        session=session,
        owner_ref=owner_ref,
        owner_graph=owner_graph,
    )
    admission = view.admission
    metadata = admission.metadata
    turn = admission.conversation
    if turn is None or metadata.get("a2a_context_id") != turn.context_id:
        raise _unavailable()
    if (turn.expected_revision == 0) != (turn.predecessor_run_id is None):
        raise _unavailable()
    if not isinstance(metadata.get("description"), str) or not metadata["description"]:
        raise _unavailable()
    return view


def _validate_admission_scope(
    view: Any, *, run_id: str, session: Any, owner_ref: str, owner_graph: str
) -> None:
    """Bind the preview read to the immutable native caller and run admission."""
    admission = view.admission
    metadata = admission.metadata
    if not (
        admission.run_id == run_id
        and admission.context.tenant_id == session.tenant
        and admission.context.graph == owner_graph
        and admission.kind == view.work_item.kind == "orchestrator_task"
        and metadata.get("a2a_owner_ref") == owner_ref
        and metadata.get("a2a_run_id") == run_id
        and metadata.get("a2a_schema") == "arbora-a2a-unary-v1"
    ):
        raise PermissionError("conversation crossed its native admission scope")


def _completed(view: Any, session: Any) -> None:
    receipt = view.run_completion
    if receipt is None:
        raise _unavailable()
    payload = receipt.payload
    if not (
        view.work_item.status == payload.outcome == "succeeded"
        and payload.tenant == session.tenant
        and payload.work_item_id == view.work_item.work_item_id
        and payload.completion.run_id == view.admission.run_id
        and payload.completion.schema_version == 1
    ):
        raise _unavailable()


async def _completed_reply(
    authority: WorkItemA2AAuthority, view: Any, session: Any
) -> str:
    """Read exactly one persisted reply only after validating its terminal receipt."""
    _completed(view, session)
    item = view.work_item.model_dump(mode="json")
    item["metadata"] = view.admission.metadata
    artifacts = await asyncio.to_thread(
        authority._persisted_artifacts, item["work_item_id"], item=item
    )
    if not artifacts or len(artifacts) != 1 or len(artifacts[0].parts) != 1:
        raise _unavailable()
    return artifacts[0].parts[0].text


def _validate_prefix_position(turn: Any, revision: int, context_id: str | None) -> None:
    """Require contiguous predecessors in the originally selected conversation."""
    if turn.expected_revision != revision:
        raise _unavailable()
    if context_id is not None and turn.context_id != context_id:
        raise _unavailable()


async def preview_completed_prefix(
    authority: WorkItemA2AAuthority, *, run_id: str, expected_revision: int
) -> ConversationPreview:
    """Read one selected own turn and its explicit predecessor chain.

    No endpoint or client schema is implied by this internal projection. A
    branch submit must re-read this preview and bind its digest independently.
    """
    session = authority._session("kg:read")
    owner_ref = authority._owner_ref(session)
    engine, work_engine = authority._engine()
    owner_graph = work_items.native_owner_graph(work_engine)
    client = engine.graph_compute.for_graph(owner_graph).async_client
    turns: list[ConversationTurn] = []
    seen: set[str] = set()
    current: str | None = run_id
    context_id = None
    revision = expected_revision
    while current is not None:
        if current in seen or len(seen) >= 128:
            raise _unavailable()
        seen.add(current)
        value = await client.work_items.get_run(tenant=session.tenant, run_id=current)
        if authority._session("kg:read") != session:
            raise PermissionError("A2A caller changed during conversation read")
        view = _admitted_turn(
            value,
            run_id=current,
            session=session,
            owner_ref=owner_ref,
            owner_graph=owner_graph,
        )
        admission = view.admission
        turn = admission.conversation
        _validate_prefix_position(turn, revision, context_id)
        context_id = turn.context_id
        reply = None
        if turns:
            reply = await _completed_reply(authority, view, session)
        if authority._session("kg:read") != session:
            raise PermissionError("A2A caller changed during conversation read")
        turns.append(
            ConversationTurn(
                current,
                view.work_item.work_item_id,
                context_id,
                revision,
                admission.metadata["description"],
                reply,
            )
        )
        # Check during traversal, not after accumulating an unbounded transcript.
        encoded = json.dumps(
            [asdict(entry) for entry in turns],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        if len(encoded) > 65_536:
            raise _unavailable("conversation preview exceeds the 64 KiB bound")
        current = turn.predecessor_run_id
        revision -= 1
    return ConversationPreview(
        turns[0], tuple(reversed(turns[1:])), hashlib.sha256(encoded).hexdigest()
    )

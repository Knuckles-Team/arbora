"""Opt-in point reads of EG's committed terminal event through AU."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)


class A2ATerminalEventUnavailable(RuntimeError):
    """The committed terminal reader could not supply a valid projection."""


def _bounded_text(value: str) -> str:
    if (
        not value.strip()
        or len(value.encode("utf-8")) > 512
        or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value)
    ):
        raise ValueError("invalid bounded text")
    return value


Text = Annotated[str, AfterValidator(_bounded_text)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Graph(_Record):
    kind: Literal["graph"]
    graph: Text


class _Scope(_Record):
    tenant: Text
    scope: _Graph
    incarnation_id: Text
    identity_digest: Annotated[
        list[Annotated[int, Field(strict=True, ge=0, le=255)]],
        Field(min_length=32, max_length=32),
    ]


class _Event(_Record):
    status: Literal["available"]
    tenant: Text
    scope: _Scope
    batch_id: Text
    commit_sequence: int = Field(gt=0, le=2**64 - 1)
    ordinal: int = Field(ge=0, le=2**32 - 1)
    run_id: Text
    kind: Literal["outcome", "tool_call", "degraded", "reconciled"]
    outcome: Literal["succeeded", "failed", "cancelled"]
    result_ref: Annotated[str, Field(min_length=1, max_length=4096)] | None
    payload_digest: Digest

    @field_validator("result_ref")
    @classmethod
    def bounded_reference(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.strip()
            or len(value.encode("utf-8")) > 4096
            or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value)
        ):
            raise ValueError("invalid result reference")
        return value


def project_terminal_event(
    value: Any, *, session: Any, task_id: str, owner_ref: str
) -> dict[str, Any]:
    """Refuse malformed/cross-bound records; never derive an event from a row."""
    unavailable = {"status": "unavailable", "reason": "producer_unavailable"}
    if value is None:
        return unavailable
    if not isinstance(value, dict):
        raise A2ATerminalEventUnavailable("committed terminal event is unavailable")
    item = value.get("work_item")
    metadata = item.get("metadata") if isinstance(item, dict) else None
    if not (
        isinstance(item, dict)
        and item.get("work_item_id") == f"workitem:orchestrator:{task_id}"
        and item.get("kind") == "orchestrator_task"
        and isinstance(metadata, dict)
        and metadata.get("a2a_owner_ref") == owner_ref
        and metadata.get("a2a_schema") == "arbora-a2a-unary-v1"
    ):
        raise A2ATerminalEventUnavailable("committed terminal event is unavailable")
    raw = value.get("terminal_event")
    if raw is None:
        return unavailable
    if raw == {"status": "unavailable", "reason": "legacy_unbound"}:
        return dict(raw)
    try:
        event = _Event.model_validate(raw)
    except ValidationError as exc:
        raise A2ATerminalEventUnavailable(
            "committed terminal event is unavailable"
        ) from exc
    if not (
        event.tenant == session.tenant
        and event.run_id == task_id
        and event.outcome
        == ("failed" if item.get("status") == "dead_letter" else item.get("status"))
    ):
        raise A2ATerminalEventUnavailable("committed terminal event is unavailable")
    return event.model_dump(mode="json")


async def read_terminal_event(
    engine: Any, *, session: Any, task_id: str, owner_ref: str
) -> dict[str, Any]:
    """Read the committed WorkItem outcome over the session-routed engine client."""
    if not isinstance(session.graph, str) or not session.graph.strip():
        raise A2ATerminalEventUnavailable("committed terminal event is unavailable")
    try:
        client = engine.graph_compute.for_graph(session.graph).async_client
        value = await client.work_items.get_outcome(
            tenant=session.tenant,
            work_item_id=f"workitem:orchestrator:{task_id}",
        )
    except PermissionError:
        raise
    except Exception as exc:
        raise A2ATerminalEventUnavailable(
            "committed terminal event is unavailable"
        ) from exc
    return project_terminal_event(
        value, session=session, task_id=task_id, owner_ref=owner_ref
    )

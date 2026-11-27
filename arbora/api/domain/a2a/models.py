"""Typed public contracts for the Arbora unary A2A facade."""

from __future__ import annotations

import base64
import binascii
import builtins
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

__all__ = [
    "A2AAgentCard",
    "A2AAgentCapabilities",
    "A2AContextBudget",
    "A2AListResult",
    "A2AMediaLimits",
    "A2AMediaRefFile",
    "A2AMessage",
    "A2APushNotificationAuthority",
    "A2APushTarget",
    "A2AOperationInvokeParams",
    "A2APlanConfirmParams",
    "A2ARouteDecision",
    "A2AReplyUnavailable",
    "A2ASkill",
    "A2AStreamingAuthority",
    "A2ATask",
    "A2ATaskState",
    "A2ATaskStatus",
    "A2ATextPart",
    "A2ATransition",
    "A2ATransitionHistoryAuthority",
    "A2ATransitionPage",
]


class WireModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class A2ATextPart(WireModel):
    kind: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=65_536)


class A2AInlineTextFile(WireModel):
    bytes: str = Field(min_length=1, max_length=87_384)
    mime_type: Literal["text/plain", "text/markdown"]
    name: str | None = Field(default=None, max_length=256)

    @field_validator("bytes")
    @classmethod
    def _valid_text(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
            decoded.decode("utf-8", errors="strict")
        except (ValueError, binascii.Error, UnicodeError) as exc:
            raise ValueError("file must contain base64 UTF-8 text") from exc
        if not decoded or len(decoded) > 65_536:
            raise ValueError("file exceeds the inline text size bound")
        return value

    def decoded_bytes(self) -> builtins.bytes:
        return base64.b64decode(self.bytes, validate=True)


#: An inline media file's decoded size bound; the modality seam's own
#: ``ARBORA_MEDIA_MAX_BYTES`` and ``MAX_UPLOAD_SIZE`` bound it further.
MEDIA_FILE_MAX_BYTES = 25 * 1024 * 1024


class A2AInlineMediaFile(WireModel):
    """An inline audio, image, video or document file (ARBORA-OPS-R899.5).

    The modality seam turns it into prompt text (audio: a transcript); the
    harness never receives the raw bytes.
    """

    bytes: str = Field(min_length=4, max_length=(MEDIA_FILE_MAX_BYTES * 4) // 3 + 4)
    mime_type: str = Field(
        pattern=r"^(audio|image|video|application|text)/[A-Za-z0-9.+-]{1,96}$"
    )
    name: str | None = Field(default=None, max_length=256)

    @field_validator("mime_type")
    @classmethod
    def _not_inline_text(cls, value: str) -> str:
        if value in ("text/plain", "text/markdown"):
            raise ValueError("inline text files use the text file model")
        return value

    @field_validator("bytes")
    @classmethod
    def _valid_base64(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("file must contain base64 bytes") from exc
        if not decoded:
            raise ValueError("file is empty")
        return value

    def decoded_bytes(self) -> builtins.bytes:
        return base64.b64decode(self.bytes, validate=True)


class A2AMediaRefFile(WireModel):
    """An image carried by upload reference (ARBORA-A2A-R001.5.5)."""

    uri: str = Field(pattern=r"^media:sha256:[0-9a-f]{64}$")
    mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    name: str | None = Field(default=None, max_length=256)


class A2ADataPart(WireModel):
    """A structured JSON part; the tool-task extension reads ``toolTask`` from it."""

    kind: Literal["data"]
    data: dict[str, Any]


class A2AFilePart(WireModel):
    kind: Literal["file"]
    file: A2AInlineTextFile | A2AInlineMediaFile | A2AMediaRefFile


class A2AMessage(WireModel):
    """Text, bounded inline text files, and inline media files.

    Media files (audio, image, video, documents) never reach the harness as
    bytes: the A2A service normalises them to text first (ARBORA-OPS-R899.5).
    """

    role: Literal["user"]
    parts: list[A2ATextPart | A2AFilePart | A2ADataPart] = Field(
        min_length=1, max_length=32
    )
    kind: Literal["message"] = "message"
    message_id: str = Field(min_length=1, max_length=512)
    context_id: str | None = Field(default=None, min_length=1, max_length=512)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _bounded_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 32:
            raise ValueError("message metadata has too many fields")
        return value

    @model_validator(mode="after")
    def _bounded_task(self) -> A2AMessage:
        # File count and aggregate text bounds live in one place:
        # ``arbora.api.domain.a2a.parts.admit_file_parts`` (ARBORA-A2A-R001.5.1).
        if len(self.task_text()) > 100_000:
            raise ValueError("message exceeds the execution task size bound")
        return self

    def attachment_size(self) -> int:
        return sum(
            len(part.file.decoded_bytes())
            for part in self.parts
            if isinstance(part, A2AFilePart)
            and not isinstance(part.file, A2AMediaRefFile)
        )

    def task_text(self) -> str:
        chunks: list[str] = []
        for part in self.parts:
            if isinstance(part, A2ATextPart):
                chunks.append(part.text)
            elif isinstance(part, A2AFilePart) and isinstance(
                part.file, A2AInlineTextFile
            ):
                chunks.append(part.file.decoded_bytes().decode("utf-8"))
        return "\n".join(chunks)

    def data_parts(self) -> list[dict[str, Any]]:
        """The structured data parts, in order."""
        return [part.data for part in self.parts if isinstance(part, A2ADataPart)]

    @staticmethod
    def _is_media(part: A2ATextPart | A2AFilePart | A2ADataPart) -> bool:
        return isinstance(part, A2AFilePart) and isinstance(
            part.file, (A2AInlineMediaFile, A2AMediaRefFile)
        )

    def media_files(
        self, exclude: frozenset[str] = frozenset()
    ) -> list[tuple[int, A2AInlineMediaFile]]:
        """The inline media files with their part index, less the ``exclude`` types."""
        return [
            (index, part.file)
            for index, part in enumerate(self.parts)
            if isinstance(part, A2AFilePart)
            and isinstance(part.file, A2AInlineMediaFile)
            and part.file.mime_type not in exclude
        ]

    def with_normalized_text(
        self, text: str, attachments: list[dict[str, Any]]
    ) -> A2AMessage:
        """This message with every part replaced by the one normalised text part.

        The kept originals are named in ``metadata.arboraAttachments`` (digest,
        type, size and the derived transcript id); their bytes are not carried on.
        """
        metadata = {**self.metadata, "arboraAttachments": attachments}
        kept = [
            part
            for part in self.parts
            if isinstance(part, A2AFilePart) and part.file.mime_type in _KEPT_TYPES
        ]
        return self.model_copy(
            update={"parts": [A2ATextPart(text=text), *kept], "metadata": metadata}
        )


#: Parts a model reads as input; normalisation leaves them on the message.
_KEPT_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})


class A2AContextBudget(WireModel):
    """Evaluate-only context bound used by assembly/tool-subset selection."""

    tokens: int = Field(ge=256, le=1_000_000)


class A2ARouteDecision(WireModel):
    """A routing result; references are opaque authority-owned identifiers."""

    agent_name: str = Field(min_length=1, max_length=256)
    selected_tools: tuple[str, ...] = Field(default=(), max_length=64)
    selection_mode: str = Field(min_length=1, max_length=128)
    agent_graph_ref: str | None = Field(default=None, max_length=512)
    run_spec_ref: str | None = Field(default=None, max_length=512)
    decision_record_ref: str | None = Field(default=None, max_length=512)
    #: Read from the routed agent's capability record (ARBORA-A2A-R016.5).
    model_profile_ref: str | None = Field(default=None, min_length=1, max_length=512)
    task_class: str | None = Field(default=None, min_length=1, max_length=512)
    #: The routed agent's library tool refs, declared as the harness candidates
    #: (ARBORA-A2A-R017.1). Empty until the route answer carries them.
    tool_refs: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("selected_tools")
    @classmethod
    def _unique_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(name.strip() for name in value)
        if any(not name for name in normalized) or len(set(normalized)) != len(
            normalized
        ):
            raise ValueError("selected tool identities must be non-empty and unique")
        return normalized


class A2AOperationInvokeParams(WireModel):
    """``arbora.op/invoke`` params (ARBORA-A2A-R006.1).

    Mirrors the shared hosted-operation registry's ``invoke`` call shape so
    the eventual bridge passes these fields through unchanged.
    """

    op: str = Field(min_length=1, max_length=256)
    params: dict[str, Any] = Field(default_factory=dict)
    plan_ref: str | None = Field(default=None, max_length=512)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)


class A2APlanConfirmParams(WireModel):
    """``arbora.plan/confirm`` params (ARBORA-A2A-R005.1/R006.1).

    The signed human approval exchange this carries is served by
    ``arbora.a2a.confirm.confirm_dispatch`` (ARBORA-A2A-R005.2.5).
    """

    task_id: str = Field(min_length=1, max_length=80)
    pending_call_id: str = Field(min_length=1, max_length=256)
    plan_ref: str = Field(min_length=1, max_length=512)
    signature: str = Field(min_length=1, max_length=4096)


def parse_confirmation(raw: Any) -> A2APlanConfirmParams:
    """The closed ``confirmation_kind`` union: ``tool_action`` or ``held_item``.

    A missing, unknown or mixed kind (one kind's fields on the other) is refused with
    ``CONFIRMATION_KIND_INVALID`` before any grant is read (ARBORA-A2A-R005.3).
    """
    from arbora.api.domain.a2a.held_item_admission import HeldItemAdmissionRequest
    from arbora.api.domain.a2a.tool_action_admission import (
        ConfirmationKindInvalid,
        ToolActionAdmissionRequest,
    )

    kind = (
        raw.get("confirmationKind", raw.get("confirmation_kind"))
        if isinstance(raw, dict)
        else None
    )
    members: dict[str, type[A2APlanConfirmParams]] = {
        "tool_action": ToolActionAdmissionRequest,
        "held_item": HeldItemAdmissionRequest,
    }
    member = members.get(kind) if isinstance(kind, str) else None
    if member is None:
        raise ConfirmationKindInvalid(ConfirmationKindInvalid.code)
    try:
        return member.model_validate(raw)
    except ValidationError as error:
        if any(e["type"] == "extra_forbidden" for e in error.errors()):
            raise ConfirmationKindInvalid(ConfirmationKindInvalid.code) from None
        raise


A2ATaskState = Literal[
    "submitted",
    "working",
    "input-required",
    "completed",
    "canceled",
    "failed",
    "rejected",
]


class A2AArtifact(WireModel):
    """One output of a task: the agent's reply text."""

    artifact_id: str = Field(min_length=1, max_length=512)
    name: str | None = Field(default=None, max_length=256)
    parts: list[A2ATextPart] = Field(min_length=1, max_length=32)


class A2AReplyUnavailable(WireModel):
    """A completed task can have an explicitly unavailable reply artifact."""

    status: Literal["unavailable"] = "unavailable"
    reason: Literal[
        "payload_too_large", "payload_serialization_failed", "reply_too_large"
    ]


class A2ATaskStatus(WireModel):
    state: A2ATaskState
    timestamp: str | None = None


#: At most this many transitions answer one ``tasks/get`` page (ARBORA-A2A-R011).
TRANSITION_PAGE_LIMIT = 200


class A2ATransition(WireModel):
    """One WorkItem state change from the engine's transition ledger, unchanged."""

    state: str = Field(min_length=1, max_length=64)
    at_ms: int = Field(ge=0)
    revision: int = Field(ge=0)


class A2ATransitionPage(WireModel):
    """One page of the ledger in commit order, and the cursor to continue."""

    entries: list[A2ATransition] = Field(max_length=TRANSITION_PAGE_LIMIT)
    next_cursor: str | None = Field(default=None, max_length=512)


class A2ATask(WireModel):
    id: str = Field(pattern=r"^(?:a2a-|work-item:)[0-9a-f]{64}$")
    context_id: str = Field(pattern=r"^a2a-context-[0-9a-f]{64}$")
    kind: Literal["task"] = "task"
    status: A2ATaskStatus
    artifacts: list[A2AArtifact] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    #: Only present when ``tasks/get`` asked for ``includeTransitionHistory``.
    transition_history: list[A2ATransition] | None = None
    transition_history_next_cursor: str | None = None


class A2AListResult(WireModel):
    tasks: list[A2ATask]
    next_cursor: str | None = None


@runtime_checkable
class A2AStreamingAuthority(Protocol):
    """Durable, restart-safe event-cursor-backed task-streaming authority.

    ``resume_from_cursor`` opens a stream of the task's durable events after the
    last ``event_seq`` the caller saw (``ARBORA-A2A-R001.2``); an absent or
    foreign task answers ``None``.
    """

    async def resume_from_cursor(self, task_id: str, after_seq: int = 0) -> Any: ...


class A2APushTarget(WireModel):
    """One webhook a task's terminal state is pushed to (ARBORA-A2A-R013.1).

    ``token_ref`` is a secrets-backend reference, never a token value.
    """

    task_id: str = Field(min_length=1, max_length=512)
    url: str = Field(min_length=1, max_length=2048)
    token_ref: str | None = Field(default=None, min_length=1, max_length=512)


@runtime_checkable
class A2APushNotificationAuthority(Protocol):
    """Durable push-notification delivery authority for task state changes."""

    def register_push_target(
        self, task_id: str, target_url: str, token_ref: str | None = None
    ) -> Any: ...


@runtime_checkable
class A2ATransitionHistoryAuthority(Protocol):
    """Durable, append-only WorkItem transition-history authority.

    Owned by medulla, not Arbora; see ARBORA-A2A-R011.
    """

    async def transition_history(
        self,
        task_id: str,
        *,
        cursor: str | None = None,
        limit: int = TRANSITION_PAGE_LIMIT,
    ) -> A2ATransitionPage | None: ...


class A2AExtension(WireModel):
    uri: str = Field(min_length=1, max_length=512)
    description: str = Field(max_length=1024)
    required: bool = False
    params: dict[str, Any] = Field(default_factory=dict)


class A2AAgentCapabilities(WireModel):
    """Capabilities advertised on the Agent Card.

    Each field defaults to ``False`` and can only become ``True`` through
    :meth:`from_wired_authorities`, which requires an actual durable
    authority object implementing the matching protocol. A bare boolean
    can never force a capability true, so the card cannot advertise more
    than Arbora can actually serve.
    """

    streaming: bool = False
    push_notifications: bool = False
    state_transition_history: bool = False
    extensions: list[A2AExtension] = Field(default_factory=list)

    @classmethod
    def from_wired_authorities(
        cls,
        *,
        streaming_authority: A2AStreamingAuthority | None = None,
        push_notification_authority: A2APushNotificationAuthority | None = None,
        transition_history_authority: A2ATransitionHistoryAuthority | None = None,
    ) -> A2AAgentCapabilities:
        """Derive capabilities truthfully from the authorities actually wired.

        Raises ``TypeError`` (a refusal, not a silent downgrade) if a
        supplied authority does not implement its required protocol.
        """
        for label, authority, protocol in (
            ("streaming_authority", streaming_authority, A2AStreamingAuthority),
            (
                "push_notification_authority",
                push_notification_authority,
                A2APushNotificationAuthority,
            ),
            (
                "transition_history_authority",
                transition_history_authority,
                A2ATransitionHistoryAuthority,
            ),
        ):
            if authority is not None and not isinstance(authority, protocol):
                raise TypeError(
                    f"{label} must implement {protocol.__name__} or be None"
                )
        return cls(
            streaming=streaming_authority is not None,
            push_notifications=push_notification_authority is not None,
            state_transition_history=transition_history_authority is not None,
        )


class A2ASkill(WireModel):
    id: str
    name: str
    description: str
    tags: list[str]
    input_modes: list[str]
    output_modes: list[str]


class A2AMediaLimits(WireModel):
    """The admission limit table, published as enforced (ARBORA-A2A-R001.5.6)."""

    allowed_types: list[str]
    max_file_part_bytes: int
    max_request_media_bytes: int
    max_image_parts: int
    inline_max_bytes: int


class A2AAgentCard(WireModel):
    name: str
    description: str
    url: str
    version: str
    protocol_version: Literal["0.3.0"] = "0.3.0"
    preferred_transport: Literal["JSONRPC"] = "JSONRPC"
    capabilities: A2AAgentCapabilities = Field(default_factory=A2AAgentCapabilities)
    security: list[dict[str, list[str]]] = Field(
        default_factory=lambda: [{"bearerAuth": ["kg:read", "kg:write"]}]
    )
    security_schemes: dict[str, dict[str, str]] = Field(
        default_factory=lambda: {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "description": "Server-validated tenant bearer identity",
            }
        }
    )
    default_input_modes: list[str] = Field(default_factory=lambda: ["text/plain"])
    default_output_modes: list[str] = Field(default_factory=lambda: ["text/plain"])
    skills: list[A2ASkill]
    media: A2AMediaLimits | None = None

"""The Web UI identity machines (ARBORA-HOST-R041.2).

``ConversationIdentity``, ``NotificationIdentity`` and ``SupportIdentity`` each
move only through their own ``transition``; the repository refuses a replacement
that changes the state along an edge the table does not list.
"""

from __future__ import annotations

from itertools import product

import pytest

from arbora.control_plane.webui import (
    AccessContext,
    ContentReference,
    ConversationEvent,
    ConversationIdentity,
    ConversationState,
    InMemoryWebUiRepository,
    NotificationEvent,
    NotificationIdentity,
    NotificationState,
    SupportEvent,
    SupportIdentity,
    SupportState,
    WebUiTransitionRefused,
)
from tests.architecture.single_writer import field_writers
from tests.control_plane.test_webui import _context, _conversation, _digest


def _content(context: AccessContext) -> ContentReference:
    return ContentReference(
        tenant_ref=context.tenant_ref,
        workspace_ref=context.workspace_ref,
        content_ref="content:one",
        artifact_ref="artifact:one",
        content_digest=_digest("content"),
        media_type="text/plain",
        size_bytes=3,
        version=1,
        digest=_digest("identity:content"),
    )


def _notification(
    state: NotificationState = NotificationState.UNREAD,
) -> NotificationIdentity:
    context = _context()
    return NotificationIdentity(
        tenant_ref=context.tenant_ref,
        workspace_ref=context.workspace_ref,
        notification_ref="notification:one",
        recipient_ref="user:one",
        content_ref=_content(context),
        state=state,
        version=1,
        digest=_digest("notification"),
    )


def _support(state: SupportState = SupportState.OPEN) -> SupportIdentity:
    context = _context()
    return SupportIdentity(
        tenant_ref=context.tenant_ref,
        workspace_ref=context.workspace_ref,
        support_ref="support:one",
        requester_ref="user:one",
        content_ref=_content(context),
        category="category:one",
        state=state,
        version=1,
        digest=_digest("support"),
    )


def _conversation_in(state: ConversationState) -> ConversationIdentity:
    return _conversation(_context(), "conversation:one").model_copy(
        update={"state": state}
    )


@pytest.mark.spec("ARBORA-HOST-R041.2")
@pytest.mark.parametrize(
    ("build", "source", "event", "target"),
    [
        pytest.param(
            _conversation_in,
            ConversationState.ACTIVE,
            ConversationEvent.ARCHIVE,
            ConversationState.ARCHIVED,
            id="active-archive",
        ),
        pytest.param(
            _conversation_in,
            ConversationState.ARCHIVED,
            ConversationEvent.RESTORE,
            ConversationState.ACTIVE,
            id="archived-restore",
        ),
        pytest.param(
            _notification,
            NotificationState.UNREAD,
            NotificationEvent.MARK_READ,
            NotificationState.READ,
            id="unread-mark_read",
        ),
        pytest.param(
            _support,
            SupportState.OPEN,
            SupportEvent.CLOSE,
            SupportState.CLOSED,
            id="open-close",
        ),
        pytest.param(
            _support,
            SupportState.CLOSED,
            SupportEvent.REOPEN,
            SupportState.OPEN,
            id="closed-reopen",
        ),
    ],
)
def test_identity_legal_edge_succeeds(build, source, event, target) -> None:  # type: ignore[no-untyped-def]
    entity = build(source)
    after = entity.transition(event)
    assert after.state is target
    assert after.version == 2
    assert after.digest != entity.digest
    assert entity.state is source and entity.version == 1


@pytest.mark.spec("ARBORA-HOST-R041.2")
@pytest.mark.parametrize(
    ("build", "states", "events"),
    [
        pytest.param(
            _conversation_in, ConversationState, ConversationEvent, id="conversation"
        ),
        pytest.param(
            _notification, NotificationState, NotificationEvent, id="notification"
        ),
        pytest.param(_support, SupportState, SupportEvent, id="support"),
    ],
)
def test_identity_every_illegal_pair_is_refused_and_unchanged(
    build, states, events
) -> None:  # type: ignore[no-untyped-def]
    legal = set(build(next(iter(states))).EDGES)
    for state, event in product(states, events):
        if (state, event) in legal:
            continue
        entity = build(state)
        with pytest.raises(WebUiTransitionRefused) as raised:
            entity.transition(event)
        assert (raised.value.from_state, raised.value.event) == (state, event)
        assert entity.state is state and entity.version == 1


@pytest.mark.spec("ARBORA-HOST-R041.2")
def test_identity_stored_values_are_the_wire_values() -> None:
    assert [s.value for s in ConversationState] == ["active", "archived"]
    assert [s.value for s in NotificationState] == ["unread", "read"]
    assert [s.value for s in SupportState] == ["open", "closed"]
    assert (
        _conversation(_context(), "conversation:one").model_dump(mode="json")["state"]
        == "active"
    )
    assert NotificationIdentity.TERMINALS == (NotificationState.READ,)
    assert ConversationIdentity.INITIAL is ConversationState.ACTIVE


@pytest.mark.spec("ARBORA-HOST-R041.2")
def test_repository_refuses_a_replacement_along_an_unlisted_edge() -> None:
    repository = InMemoryWebUiRepository(clock=lambda: 1)
    context = _context()
    first = repository.put(_notification(), context=context)
    read = first.transition(NotificationEvent.MARK_READ)
    stored = repository.put(read, context=context, expected_version=1)
    assert stored.state is NotificationState.READ
    unread_again = _notification().model_copy(update={"version": 3})
    with pytest.raises(WebUiTransitionRefused) as raised:
        repository.put(unread_again, context=context, expected_version=2)
    assert raised.value.event == "replace"
    assert (
        repository.get("notification", "notification:one", context=context).state
        is NotificationState.READ
    )


@pytest.mark.spec("ARBORA-HOST-R041.2")
def test_only_the_identity_transitions_write_the_state_field() -> None:
    writers = field_writers("arbora/control_plane/webui", {"state"})
    assert writers == {("arbora/control_plane/webui/models.py", "transition")}

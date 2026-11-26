"""Operations that wait on an engine scope the pinned catalog lacks (ARBORA-FLEET-R012.2)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.ops._pending import refuse_unavailable
from arbora.api.ops.registry_factory import get_registry
from arbora.api.registry import Composite, OpSpec, Verb
from arbora.api.registry.eg_binding import (
    load_catalog,
    load_generated_scopes,
)
from arbora.api.registry.pending_scopes import (
    PendingScopesError,
    apply_pending_scopes,
    load_pending_engine_scopes,
)
from tests.test_served_operation_api import _call, served  # noqa: F401


def _fixture_op(op_id: str, *scopes: str) -> OpSpec:
    from arbora.api.ops.knowledge import EngineReply, FlagGetParams

    return OpSpec(
        id=op_id,
        verb=Verb.ASK,
        summary="A curated operation",
        examples=("curated",),
        params=FlagGetParams,
        result=EngineReply,
        binding=Composite(handler="arbora.api.ops.knowledge.handle_flag_get"),
        scopes=frozenset(scopes),
    )


@pytest.mark.spec("ARBORA-FLEET-R012.2")
def test_listed_scope_is_served_refusing_unavailable(
    served: Any,  # noqa: F811
) -> None:
    pending = frozenset({"future:scope"})
    present = _fixture_op("t.present", "kg:read")
    waiting = _fixture_op("t.waiting", "kg:read", "future:scope")
    ops, errors = apply_pending_scopes([present, waiting], {"kg:read"}, pending)
    assert errors == []
    assert ops[0] is present
    assert ops[1].scopes == frozenset({"kg:read"})
    assert ops[1].binding == Composite(
        handler="arbora.api.ops._pending.refuse_unavailable"
    )
    # An unlisted missing scope (a misspelling) still stops the build.
    _, errors = apply_pending_scopes(
        [_fixture_op("t.typo", "future:scope", "kg:raed")], {"kg:read"}, pending
    )
    assert errors == ["t.typo: unregistered curated EG scopes: kg:raed"]
    # The real registry serves a pending operation that refuses UNAVAILABLE.
    registry = get_registry()
    op = registry.get("knowledge.flag.raise")
    assert op is not None
    assert isinstance(op.binding, Composite)
    assert op.binding.handler.endswith("refuse_unavailable")
    app, _client = served
    reply = _call(
        app,
        "knowledge.flag.get",
        {"flag_id": "flag-1"},
    )
    assert reply.status_code == 503, reply.text
    assert reply.json()["error"]["code"] == "UNAVAILABLE"


@pytest.mark.spec("ARBORA-FLEET-R012.2")
def test_no_listed_scope_is_in_the_pinned_catalog() -> None:
    listed = load_pending_engine_scopes()
    pinned = load_generated_scopes(load_catalog())
    assert listed
    assert not (listed & pinned.keys()), "remove the scope from the pending list"
    registry = get_registry()
    used: set[str] = set()
    for op in registry:
        used |= op.scopes | op.executor_scopes
    assert not (listed & used), "a served operation still holds a pending scope"


@pytest.mark.spec("ARBORA-FLEET-R012.2")
async def test_refusal_never_reaches_the_engine() -> None:
    from arbora.api.invoke.pipeline import OperationRefused

    with pytest.raises(OperationRefused) as refused:
        await refuse_unavailable(object(), {}, _fixture_op("t.x"))
    assert refused.value.code == "UNAVAILABLE"


@pytest.mark.spec("ARBORA-FLEET-R012.2")
def test_pending_list_must_name_each_scope_once(tmp_path: Any) -> None:
    bad = tmp_path / "pending.yaml"
    bad.write_text("pending_engine_scopes: [a:b, a:b]\n")
    with pytest.raises(PendingScopesError):
        load_pending_engine_scopes.__wrapped__(bad)

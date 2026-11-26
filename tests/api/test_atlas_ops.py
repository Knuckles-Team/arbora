"""Atlas connection, mapping and run operations (ARBORA-OPS-R911, R922.3)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import atlas
from arbora.api.registry import Effect, Verb
from tests.api._knowledge_support import NO_OP, caller_context

_DIGEST = "sha256:" + "a" * 64
_OPS = {op.id: op for op in atlas.operations()}


def test_every_atlas_operation_is_registered_with_ingest_scopes() -> None:
    assert len(_OPS) == 13
    for op_id, op in _OPS.items():
        want = "ingest:read" if op.verb is Verb.ASK else "ingest:write"
        assert op.scopes == frozenset({want}), op_id


@pytest.mark.spec("ARBORA-OPS-R911")
async def test_mapping_approve_needs_current_digest_and_plan_confirmation() -> None:
    approve = _OPS["ingest.sources.mapping.approve"]
    # Approve is an admin act: it previews a plan and runs only once confirmed.
    assert approve.verb is Verb.ACT and approve.effect is Effect.ADMIN
    assert _OPS["ingest.sources.connections.delete"].effect is Effect.ADMIN
    with pytest.raises(ValidationError):
        atlas.MappingApproveParams(connection_id="c")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        atlas.MappingApproveParams(connection_id="c", mapping_digest="stale")
    assert atlas.MappingApproveParams(connection_id="c", mapping_digest=_DIGEST)
    with pytest.raises(OperationRefused) as refused:
        await atlas.handle_unserved(caller_context(), {}, NO_OP)
    assert refused.value.code == "UNAVAILABLE"


@pytest.mark.spec("ARBORA-OPS-R911")
def test_credentials_are_references_only() -> None:
    base: dict[str, Any] = {"provider": "p", "label": "l"}
    for ref in ("openbao://kv/atlas/token", "env://ATLAS_TOKEN"):
        assert atlas.ConnectionCreateParams(credential_ref=ref, **base)
    for raw in ("hunter2", "openbao://", "file:///etc/passwd"):
        with pytest.raises(ValidationError):
            atlas.ConnectionCreateParams(credential_ref=raw, **base)
    with pytest.raises(ValidationError):
        atlas.ConnectionCreateParams(
            credential_ref="env://X", settings={"api_key": "abc"}, **base
        )


@pytest.mark.spec("ARBORA-OPS-R911")
@pytest.mark.parametrize(
    "op_id",
    [
        "ingest.sources.providers.list",
        "ingest.sources.connections.list",
        "ingest.sources.connections.get",
        "ingest.sources.connections.create",
        "ingest.sources.connections.delete",
        "ingest.sources.discover",
        "ingest.sources.mapping.get",
        "ingest.sources.mapping.propose",
        "ingest.sources.sync.preview",
        "ingest.sources.runs.get",
        "ingest.sources.runs.cancel",
    ],
)
async def test_unserved_connection_operations_refuse_unavailable(op_id: str) -> None:
    assert op_id in _OPS
    with pytest.raises(OperationRefused) as refused:
        await atlas.handle_unserved(caller_context(), {}, NO_OP)
    assert refused.value.code == "UNAVAILABLE"


@pytest.mark.spec("ARBORA-OPS-R922.3")
async def test_runs_list_refuses_unavailable_until_the_engine_lists_runs() -> None:
    op = _OPS["ingest.sources.runs.list"]
    assert op.scopes == frozenset({"ingest:read"})
    assert atlas.RunsListParams(state="failed", connection_id="c").limit == 25
    with pytest.raises(ValidationError):
        atlas.RunsListParams(state="stuck")  # type: ignore[arg-type]
    with pytest.raises(OperationRefused) as refused:
        await atlas.handle_unserved(caller_context(), {}, NO_OP)
    assert refused.value.code == "UNAVAILABLE"

"""Operator operations are served names, not placeholders (ARBORA-OPS-R913)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import operator_surface as surface
from tests.api._engine_double import context

pytestmark = pytest.mark.spec("ARBORA-OPS-R913")
_OPS = {op.id: op for op in surface.operations()}


def test_every_named_operation_is_registered_once() -> None:
    assert len(_OPS) == len(surface.table_ids()) == 24
    for expected in (
        "fleet.servers.register",
        "schedule.tasks.read",
        "workflows.run",
        "commands.autocomplete",
        "resources.spawn",
        "roles.templates.list",
        "roles.instances.retire",
    ):
        assert expected in _OPS


def test_every_non_read_operation_names_the_owner_that_replays_it() -> None:
    for op_id, op in _OPS.items():
        if op.effect.value in {"write", "destructive"}:
            assert op_id in surface.NATIVE_IDEMPOTENCY, op_id


def _model(op_id: str) -> Any:
    return _OPS[op_id].params


def test_admin_effects_need_admin_authority_and_params_are_closed() -> None:
    register = _OPS["fleet.servers.register"]
    assert register.effect.value == "admin"
    assert register.principals.value == "human_undelegated"
    with pytest.raises(ValidationError):
        _model("fleet.servers.register")(name="x", config={}, extra=1)
    with pytest.raises(ValidationError):
        _model("code.context.read")(action="other")
    assert _model("schedule.tasks.read").model_validate({"from": "a", "to": "b"})


class Pipeline:
    """The invoke pipeline: each target recorded with the arguments it received."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, _ctx: Any, op_id: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((op_id, dict(arguments)))
        return {"ok": op_id}


@pytest.mark.parametrize(
    ("op_id", "params", "target", "arguments"),
    [
        (
            "fleet.tools.toggle",
            {"id": "t", "enabled": True},
            "fleet.tools.pin",
            {"items": ["t"]},
        ),
        (
            "fleet.skills.toggle",
            {"id": "s", "enabled": False},
            "fleet.tools.unpin",
            {"items": ["s"]},
        ),
        ("ingest.pipeline.status", {}, "ingest.jobs.list", {}),
        (
            "ingest.pipeline.run",
            {"source": "https://example.test/a"},
            "ingest.submissions.create",
            {"kind": "ingest_url", "target": "https://example.test/a", "options": {}},
        ),
        ("resources.list", {}, "capacity.throttle.status", {}),
        (
            "resources.spawn",
            {"kind": "agent", "config": {"text": "go"}},
            "agents.run",
            {"message": {"kind": "agent", "text": "go"}},
        ),
        (
            "roles.instances.retire",
            {"instance_id": "r1"},
            "agents.library.delete",
            {"id": "r1"},
        ),
    ],
)
async def test_a_name_runs_its_target_through_the_invoke_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    params: dict[str, Any],
    target: str,
    arguments: dict[str, Any],
) -> None:
    pipeline = Pipeline()
    monkeypatch.setattr(surface, "invoke_registered", pipeline)
    reply = await surface.handle_alias(context(), params, _OPS[op_id])
    assert pipeline.calls == [(target, arguments)]
    assert reply == {"value": {"ok": target}}


async def test_an_ingest_run_with_no_source_is_refused() -> None:
    with pytest.raises(OperationRefused) as refused:
        await surface.handle_alias(
            context(), {"source": None}, _OPS["ingest.pipeline.run"]
        )
    assert refused.value.code == "INVALID_ARGUMENT"

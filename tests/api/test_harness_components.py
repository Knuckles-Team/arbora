"""Harness component reads: registration, admission, refusal paths
(ARBORA-OPS-R068.1).

Wiring tests only: the served round trip is pending the engine release
carrying the versioned harness component query.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import harness_components
from arbora.api.ops.harness_components import (
    UNAVAILABLE_COMPONENT_GET,
    UNAVAILABLE_COMPONENT_LIST,
    ComponentVersionEntry,
    ComponentVersionGetRead,
    ComponentVersionListRead,
    operations,
)
from arbora.api.ops.registry_factory import get_registry
from arbora.api.registry import Composite
from tests.api._ops_support import harness_context

SERVICE = "harness_component_reader"

DIGEST = "ab" * 32


class _ComponentReader:
    def __init__(
        self,
        list_read: ComponentVersionListRead | None = None,
        get_read: ComponentVersionGetRead | None = None,
    ) -> None:
        self.list_calls: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []
        self.list_read = list_read or UNAVAILABLE_COMPONENT_LIST
        self.get_read = get_read or UNAVAILABLE_COMPONENT_GET

    async def list(self, **kwargs: object) -> ComponentVersionListRead:
        self.list_calls.append(kwargs)
        return self.list_read

    async def get(self, **kwargs: object) -> ComponentVersionGetRead:
        self.get_calls.append(kwargs)
        return self.get_read


def _entry(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "component_id": "comp-one",
        "kind": "guardrails",
        "content_ref": "blob:one",
        "content_digest": DIGEST,
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": None,
        "confidence": 0.9,
        "evidence_run_id": "run-one",
        "supersedes_id": None,
        "applied_to_id": None,
    }
    values.update(changes)
    return values


def _list(
    entries: list[dict[str, object]] | None = None, **changes: object
) -> ComponentVersionListRead:
    values: dict[str, object] = {
        "availability": "available",
        "entries": [ComponentVersionEntry.model_validate(e) for e in entries or []],
        "next_cursor": None,
    }
    values.update(changes)
    return ComponentVersionListRead.model_validate(values)


def _get(
    entry: dict[str, object] | None = None, **changes: object
) -> ComponentVersionGetRead:
    values: dict[str, object] = {"availability": "available", "entry": entry}
    values.update(changes)
    return ComponentVersionGetRead.model_validate(values)


def _op(op_id: str):
    return next(op for op in operations() if op.id == op_id)


def test_component_ops_are_registered_with_read_authority() -> None:
    ops = operations()
    assert {op.id for op in ops} == {
        "harness.components.list",
        "harness.components.get",
    }
    for op in ops:
        assert isinstance(op.binding, Composite)
        assert op.verb.value == "find"
        assert op.scopes == frozenset({"kg:read"})
        assert op.effect.value == "read"
        assert op.idempotency.value == "none"
        assert op.binding.handler.startswith("arbora.api.ops.harness_components.")
    registry = get_registry()
    assert "harness.components.list" in {op.id for op in registry}
    assert "harness.components.get" in {op.id for op in registry}


async def test_absent_reader_refuses_unavailable(op_by_id) -> None:
    op = op_by_id(operations(), "harness.components.list")
    context = harness_context(services={})
    with pytest.raises(OperationRefused) as excinfo:
        await harness_components.handle_harness_components_list(context, {}, op)
    assert excinfo.value.code == "UNAVAILABLE"
    assert excinfo.value.details["reason"] == "HARNESS_READER_UNAVAILABLE"
    assert excinfo.value.details["provenance"] == (
        f"harness.admission:harness.components.list:{SERVICE}-uncomposed"
    )


async def test_denied_scope_refuses_before_the_reader(op_by_id) -> None:
    reader = _ComponentReader()
    op = op_by_id(operations(), "harness.components.get")
    context = harness_context(services={SERVICE: reader}, scopes=())
    with pytest.raises(OperationRefused) as excinfo:
        await harness_components.handle_harness_components_get(
            context, {"component_id": "comp-one"}, op
        )
    assert excinfo.value.code == "SCOPE_REQUIRED"
    assert excinfo.value.details["missing_scopes"] == ["kg:read"]
    assert reader.get_calls == []


@pytest.mark.parametrize(
    ("params", "op_id"),
    [
        ({}, "harness.components.get"),
        ({"component_id": ""}, "harness.components.get"),
        ({"component_id": "comp-one", "tenant": "x"}, "harness.components.get"),
        ({"limit": 0}, "harness.components.list"),
        ({"limit": 201}, "harness.components.list"),
        ({"kind": "prompt"}, "harness.components.list"),
    ],
)
async def test_malformed_params_never_reach_the_reader(op_by_id, params, op_id) -> None:
    reader = _ComponentReader()
    op = op_by_id(operations(), op_id)
    context = harness_context(services={SERVICE: reader})
    handler = (
        harness_components.handle_harness_components_get
        if op_id == "harness.components.get"
        else harness_components.handle_harness_components_list
    )
    with pytest.raises(ValidationError):
        await handler(context, params, op)
    assert reader.list_calls == []
    assert reader.get_calls == []


async def test_list_forwards_caller_and_preserves_the_page(op_by_id) -> None:
    reader = _ComponentReader(list_read=_list([_entry(), _entry(component_id="c2")]))
    op = op_by_id(operations(), "harness.components.list")
    context = harness_context(services={SERVICE: reader})
    result = await harness_components.handle_harness_components_list(
        context, {"cursor": "next-1", "limit": 5, "kind": "skill"}, op
    )
    assert reader.list_calls == [
        {"context": context, "cursor": "next-1", "limit": 5, "kind": "skill"}
    ]
    value = result["value"]
    assert value["availability"] == "available"
    assert [e["component_id"] for e in value["entries"]] == ["comp-one", "c2"]
    assert value["entries"][0]["content_digest"] == DIGEST
    assert value["entries"][0]["valid_from"] == "2026-01-01T00:00:00Z"


async def test_list_empty_page_stays_available(op_by_id) -> None:
    reader = _ComponentReader(list_read=_list())
    op = op_by_id(operations(), "harness.components.list")
    result = await harness_components.handle_harness_components_list(
        harness_context(services={SERVICE: reader}), {}, op
    )
    assert result["value"] == {
        "availability": "available",
        "entries": [],
        "next_cursor": None,
    }


async def test_list_malformed_entry_refuses_the_whole_page(op_by_id) -> None:
    # A raw, unprojectable entry bypasses the reader's own validation so
    # the handler's projection is what under test.
    reader = _ComponentReader()
    reader.list_read = ComponentVersionListRead.model_construct(
        availability="available",
        entries=(_entry(confidence=1.5),),
        next_cursor=None,
    )
    op = op_by_id(operations(), "harness.components.list")
    result = await harness_components.handle_harness_components_list(
        harness_context(services={SERVICE: reader}), {}, op
    )
    assert result["value"] == UNAVAILABLE_COMPONENT_LIST.model_dump(mode="json")


async def test_get_projects_a_valid_version(op_by_id) -> None:
    entry = _entry(valid_to="2026-02-01T00:00:00Z")
    reader = _ComponentReader(get_read=_get(entry))
    op = op_by_id(operations(), "harness.components.get")
    context = harness_context(services={SERVICE: reader})
    result = await harness_components.handle_harness_components_get(
        context, {"component_id": "comp-one"}, op
    )
    assert reader.get_calls == [
        {"context": context, "component_id": "comp-one", "as_of": None}
    ]
    value = result["value"]
    assert value["availability"] == "available"
    assert value["entry"]["content_digest"] == DIGEST
    assert value["entry"]["valid_to"] == "2026-02-01T00:00:00Z"


@pytest.mark.parametrize(
    ("entry_changes", "why"),
    [
        ({"valid_to": "2025-12-31T00:00:00Z"}, "invalid interval"),
        ({"confidence": 1.5}, "confidence out of range"),
        ({"evidence_run_id": ""}, "version without evidence"),
        ({"kind": "prompt"}, "unknown kind"),
        ({"content_digest": "zz" * 32}, "invalid digest"),
        ({"supersedes_id": None, "extra_field": True}, "unknown field"),
    ],
)
async def test_get_malformed_answer_is_unavailable(
    op_by_id, entry_changes, why
) -> None:
    # A raw, unprojectable entry bypasses the reader's own validation so
    # the handler's projection is what under test.
    reader = _ComponentReader()
    reader.get_read = ComponentVersionGetRead.model_construct(
        availability="available", entry=_entry(**entry_changes)
    )
    op = op_by_id(operations(), "harness.components.get")
    result = await harness_components.handle_harness_components_get(
        harness_context(services={SERVICE: reader}),
        {"component_id": "comp-one"},
        op,
    )
    assert result["value"] == UNAVAILABLE_COMPONENT_GET.model_dump(mode="json"), why


async def test_get_unknown_component_is_indistinguishable(op_by_id) -> None:
    reader = _ComponentReader(get_read=_get(None, availability="available"))
    op = op_by_id(operations(), "harness.components.get")
    result = await harness_components.handle_harness_components_get(
        harness_context(services={SERVICE: reader}),
        {"component_id": "someone-elses-comp"},
        op,
    )
    assert result["value"] == UNAVAILABLE_COMPONENT_GET.model_dump(mode="json")
    assert reader.get_calls[0]["component_id"] == "someone-elses-comp"


def test_entry_model_refuses_unknown_fields_and_bad_intervals() -> None:
    with pytest.raises(ValidationError):
        ComponentVersionEntry.model_validate(_entry(unwanted=True))
    with pytest.raises(ValidationError):
        ComponentVersionEntry.model_validate(_entry(valid_to="2025-01-01T00:00:00Z"))

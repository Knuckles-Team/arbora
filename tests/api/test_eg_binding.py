"""EG method-catalog coverage and authority derivation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arbora.api.registry.eg_binding import (
    EgContractError,
    load_catalog,
    load_eg_bindings,
)
from arbora.api.registry.spec import (
    AuditClass,
    Effect,
    EgMethod,
    EgSchemaRef,
    Idempotency,
    OpSpec,
    Stability,
    Verb,
    eg_method_ref,
)
from tests.api._catalog import catalog, method_row, scope_row


def _fixture(root: Path) -> tuple[dict[str, Any], Path]:
    document = catalog(
        (
            method_row("Read", "query:read"),
            method_row(
                "Admin", "admin:cluster", mutates=True, replay_class="OperationIdentity"
            ),
            method_row(
                "Service", "service:control", mutates=True, replay_class="NonceOnly"
            ),
        ),
        (
            scope_row("query:read", "domain"),
            scope_row("admin:cluster", "admin"),
            scope_row("service:control", "service-only"),
        ),
    )
    exclusions = root / "exclusions.yaml"
    exclusions.write_text("exclusions: []\n")
    return document, exclusions


def _curated(method: str, op_id: str) -> OpSpec:
    scope = "admin:cluster" if method == "Admin" else "query:read"
    return OpSpec(
        id=op_id,
        verb=Verb.ASK,
        summary="Curated operation",
        examples=("curated",),
        params=eg_method_ref("Read", "request"),
        result=eg_method_ref("Read", "result"),
        binding=EgMethod(service=method, op=method),
        scopes=frozenset({scope}),
    )


@pytest.mark.spec("ARBORA-FLEET-R012")
def test_generates_exact_caller_bound_ops(tmp_path: Path) -> None:
    document, exclusions = _fixture(tmp_path)
    ops = load_eg_bindings(catalog=document, exclusions_path=exclusions)
    assert [op.id for op in ops] == [
        "eg.query.Admin",
        "eg.query.Read",
        "eg.query.Service",
    ]
    assert all(
        op.binding
        == EgMethod(service=op.id.rsplit(".", 1)[1], op=op.id.rsplit(".", 1)[1])
        for op in ops
    )
    admin, read, service = ops
    assert (
        admin.effect is Effect.ADMIN and admin.idempotency is Idempotency.KEY_REQUIRED
    )
    assert admin.audit is AuditClass.EVENT
    assert read.effect is Effect.READ and read.idempotency is Idempotency.NATURAL
    assert service.principals.value == "service_only"
    assert all(op.executor.value == "caller" for op in ops)


def test_curated_binding_suppresses_generated_method(tmp_path: Path) -> None:
    document, exclusions = _fixture(tmp_path)
    ops = load_eg_bindings(
        catalog=document,
        exclusions_path=exclusions,
        curated_ops=iter((_curated("Read", "query.read"),)),
    )
    assert {op.id for op in ops} == {"eg.query.Admin", "eg.query.Service"}


def test_curated_binding_cannot_weaken_engine_scope(tmp_path: Path) -> None:
    document, exclusions = _fixture(tmp_path)
    unsafe = _curated("Admin", "query.admin").model_copy(
        update={"scopes": frozenset({"query:read"})}
    )
    with pytest.raises(EgContractError, match="curated EG authority differs"):
        load_eg_bindings(
            catalog=document, exclusions_path=exclusions, curated_ops=(unsafe,)
        )


@pytest.mark.spec("ARBORA-FLEET-R012")
def test_exclusion_needs_current_method_and_valid_reason(tmp_path: Path) -> None:
    document, exclusions = _fixture(tmp_path)
    exclusions.write_text("exclusions:\n  - method: Read\n    reason: service-only\n")
    assert "eg.query.Read" not in {
        op.id for op in load_eg_bindings(catalog=document, exclusions_path=exclusions)
    }
    exclusions.write_text(
        "exclusions:\n  - method: Gone\n    reason: engine-internal\n"
    )
    with pytest.raises(EgContractError, match="stale EG exclusion"):
        load_eg_bindings(catalog=document, exclusions_path=exclusions)
    exclusions.write_text("exclusions:\n  - method: Read\n    reason: arbitrary\n")
    with pytest.raises(EgContractError, match="invalid exclusion reason"):
        load_eg_bindings(catalog=document, exclusions_path=exclusions)


def test_superseded_exclusion_requires_registered_op(tmp_path: Path) -> None:
    document, exclusions = _fixture(tmp_path)
    exclusions.write_text(
        "exclusions:\n  - method: Read\n    reason: superseded-by:query.read\n"
    )
    with pytest.raises(EgContractError, match="absent superseding op"):
        load_eg_bindings(catalog=document, exclusions_path=exclusions)
    load_eg_bindings(
        catalog=document,
        exclusions_path=exclusions,
        curated_ops=(_curated("Admin", "query.read"),),
    )


def test_missing_wire_flag_or_scope_fails_closed(tmp_path: Path) -> None:
    document, exclusions = _fixture(tmp_path)
    del document["methods"][0]["is_wire_callable"]
    with pytest.raises(EgContractError, match="missing is_wire_callable"):
        load_eg_bindings(catalog=document, exclusions_path=exclusions)
    document["methods"][0]["is_wire_callable"] = True
    document["methods"][0]["policy"]["authz_action"] = "unknown:scope"
    with pytest.raises(EgContractError, match="unregistered EG scope"):
        load_eg_bindings(catalog=document, exclusions_path=exclusions)


@pytest.mark.spec("ARBORA-FLEET-R012")
def test_generated_ops_reference_the_catalog_method(tmp_path: Path) -> None:
    document, exclusions = _fixture(tmp_path)
    ops = load_eg_bindings(catalog=document, exclusions_path=exclusions)
    for op in ops:
        method = op.id.rsplit(".", 1)[1]
        assert isinstance(op.params, EgSchemaRef)
        assert isinstance(op.result, EgSchemaRef)
        assert op.params.path == f"medulla:method/{method}#request"
        assert op.result.path == f"medulla:method/{method}#result"


def test_descriptor_stability_maps_to_operation_stability(tmp_path: Path) -> None:
    document, exclusions = _fixture(tmp_path)
    document["methods"][0]["stability"] = "deprecated"
    ops = {
        op.id: op
        for op in load_eg_bindings(catalog=document, exclusions_path=exclusions)
    }
    assert ops["eg.query.Read"].stability is Stability.EXPERIMENTAL
    assert ops["eg.query.Admin"].stability is Stability.STABLE
    document["methods"][0]["stability"] = "internal"
    with pytest.raises(EgContractError, match="unsupported wire stability"):
        load_eg_bindings(catalog=document, exclusions_path=exclusions)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc.pop("catalog_digest"),
        lambda doc: doc.update(methods=[]),
        lambda doc: doc.update(scopes=None),
    ],
)
def test_malformed_catalog_fails_closed(tmp_path: Path, mutate: Any) -> None:
    document, exclusions = _fixture(tmp_path)
    mutate(document)
    with pytest.raises(EgContractError, match="method catalog"):
        load_eg_bindings(catalog=document, exclusions_path=exclusions)


def test_catalog_must_be_an_object() -> None:
    with pytest.raises(EgContractError, match="must be an object"):
        load_catalog([])  # type: ignore[arg-type]


@pytest.mark.spec("ARBORA-OPS-R006")
def test_method_not_marked_wire_callable_is_not_bound(tmp_path: Path) -> None:
    document, exclusions = _fixture(tmp_path)
    for row in document["methods"]:
        if row["id"] == "Service":
            row["is_wire_callable"] = False
    ops = load_eg_bindings(catalog=document, exclusions_path=exclusions)
    assert {op.id for op in ops} == {"eg.query.Admin", "eg.query.Read"}


@pytest.mark.spec("ARBORA-OPS-R008")
@pytest.mark.spec("ARBORA-FLEET-R012")
def test_engine_catalog_serves_every_wire_callable_method_as_generated_op() -> None:
    """The engine's run-time catalog yields one ``eg.<domain>.<Method>`` op per callable method."""
    rows = load_catalog()["methods"]
    expected = {
        f"eg.{row['domain']}.{row['id']}" for row in rows if row["is_wire_callable"]
    }
    ops = load_eg_bindings()
    ids = [op.id for op in ops]
    assert expected
    assert len(ids) == len(set(ids))
    assert set(ids) == expected
    assert all(isinstance(op.binding, EgMethod) for op in ops)

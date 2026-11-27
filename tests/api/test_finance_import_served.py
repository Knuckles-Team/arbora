"""CSV import preview and commit with pin-lag guards (ARBORA-DATA-MARKET-R009.5, R009.6, R009.7)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import msgpack
import pytest

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import finance_import as imp
from arbora.api.ops import market_detail as detail
from arbora.pin_lag import scion_sdk as pin
from tests.mcp_server._read_port_double import FakeReadPort

NO_OP: Any = None
CONTENT = b"when,what\n1,buy\n2,sell\n"
DIGEST = f"sha256:{hashlib.sha256(CONTENT).hexdigest()}"
MAPPING = {"when": "event_time_utc", "what": "action"}


@dataclass
class Issue:
    row: int | None
    column: str | None
    code: str


class StructuralCsv(SimpleNamespace):
    """The SDK shape relay 2 asked for: issues reported, never raised, no cell content."""


def sdk(row_errors: tuple[Issue, ...] = ()) -> StructuralCsv:
    def preview(content: bytes, mapping: Any) -> Any:
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        return SimpleNamespace(
            file_digest=digest,
            rows_total=2,
            rows_ok=2 - len(row_errors),
            mapping_errors=(),
            row_errors=row_errors,
        )

    return StructuralCsv(
        CsvIssue=Issue,
        ColumnMapping=lambda header_to_field: SimpleNamespace(
            header_to_field=header_to_field
        ),
        AccountContext=lambda **kw: SimpleNamespace(**kw),
        preview_csv_mapping=preview,
        import_activities=lambda content, mapping, ctx: tuple(
            f"{ctx.account_id}:{i}" for i in range(2)
        ),
    )


class Engine:
    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}

    async def send(
        self, _c: Any, method: str, params: dict[str, Any], **_k: Any
    ) -> Any:
        if method == "CreateNodeIfAbsent":
            fields = msgpack.unpackb(params["properties_msgpack"], raw=False)
            self.nodes.setdefault(params["node_id"], fields)
            return True
        return [
            [n, msgpack.packb(self.nodes[n], use_bin_type=True)]
            for n in params["node_ids"]
            if n in self.nodes
        ]


class Source:
    async def read(self, _c: Any, _artifact: str) -> bytes:
        return CONTENT


class Sink:
    def __init__(self) -> None:
        self.ingested: list[Any] = []

    async def ingest(self, _c: Any, activities: Any) -> int:
        self.ingested.append(activities)
        return len(activities)


def context(sink: Sink | None = None, bindings: dict[str, str] | None = None) -> Any:
    services: dict[str, Any] = {
        "finance_read_port": FakeReadPort(bindings=bindings),
        "finance_import_source": Source(),
    }
    if sink is not None:
        services["finance_activity_sink"] = sink
    return SimpleNamespace(
        caller=SimpleNamespace(tenant="tenant:one", principal="p"), services=services
    )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Engine:
    engine = Engine()
    monkeypatch.setattr(imp, "engine_send", engine.send)
    monkeypatch.setattr(detail, "engine_send", engine.send)
    monkeypatch.setattr(imp, "require_csv_preview", lambda: sdk())
    return engine


PREVIEW = {"artifact": "up-1", "file_digest": DIGEST, "mapping": MAPPING}


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R009.5")
async def test_preview_reports_mapping_and_row_errors_without_cell_contents(
    world: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        imp, "require_csv_preview", lambda: sdk((Issue(2, "when", "CURRENCY_INVALID"),))
    )
    out = (await imp.handle_import_preview(context(), PREVIEW, NO_OP))["value"]
    assert out["rows_total"] == 2 and out["rows_ok"] == 1
    assert out["row_errors"] == [
        {"row": 2, "column": "when", "code": "CURRENCY_INVALID"}
    ]
    assert out["reimport"] is False and out["file_digest"] == DIGEST
    assert "buy" not in str(out) and "sell" not in str(out)
    with pytest.raises(EngineRefusal) as bad:
        await imp.handle_import_preview(
            context(), {**PREVIEW, "file_digest": "sha256:" + "0" * 64}, NO_OP
        )
    assert bad.value.code == "DIGEST_MISMATCH"


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R009.5")
async def test_the_pinned_sdk_refuses_the_preview_naming_the_issue_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    monkeypatch.setattr(
        pin.importlib,
        "import_module",
        lambda name: (
            SimpleNamespace()
            if name.endswith("csv_import")
            else importlib.import_module(name)
        ),
    )
    with pytest.raises(OperationRefused) as refused:
        await imp.handle_import_preview(context(), PREVIEW, NO_OP)
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == pin.CSV_PREVIEW_SYMBOL


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R009.6")
async def test_commit_is_idempotent_by_digest_and_refuses_errors(
    world: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = Sink()
    ctx = context(sink)
    first_preview = (await imp.handle_import_preview(ctx, PREVIEW, NO_OP))["value"]
    body = {"preview_id": first_preview["preview_id"], "account": "acct-1"}
    first = (await imp.handle_import_commit(ctx, body, NO_OP))["value"]
    assert first["imported"] == 2 and first["skipped_duplicate"] == 0
    again = (await imp.handle_import_commit(ctx, body, NO_OP))["value"]
    assert again["receipt_id"] == first["receipt_id"] and again["imported"] == 0
    assert len(sink.ingested) == 1
    with pytest.raises(EngineRefusal) as unknown:
        await imp.handle_import_commit(
            ctx, {"preview_id": "sha256:" + "1" * 64, "account": "acct-1"}, NO_OP
        )
    assert unknown.value.code == "PREVIEW_NOT_FOUND"
    with pytest.raises(EngineRefusal) as unverified:
        await imp.handle_import_commit(context(sink, bindings={}), body, NO_OP)
    assert unverified.value.code == "ACCOUNT_BINDING_UNVERIFIED"
    monkeypatch.setattr(
        imp, "require_csv_preview", lambda: sdk((Issue(1, None, "RAGGED_ROW"),))
    )
    flawed = (
        await imp.handle_import_preview(
            context(), {**PREVIEW, "mapping": {"when": "event_time_utc"}}, NO_OP
        )
    )["value"]
    with pytest.raises(EngineRefusal) as errors:
        await imp.handle_import_commit(
            ctx, {"preview_id": flawed["preview_id"], "account": "acct-1"}, NO_OP
        )
    assert errors.value.code == "PREVIEW_HAS_ERRORS"


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R009.6")
async def test_an_unbound_sink_refuses_unavailable_and_imports_nothing(
    world: Engine,
) -> None:
    ctx = context()
    shown = (await imp.handle_import_preview(ctx, PREVIEW, NO_OP))["value"]
    with pytest.raises(OperationRefused) as refused:
        await imp.handle_import_commit(
            ctx, {"preview_id": shown["preview_id"], "account": "acct-1"}, NO_OP
        )
    assert refused.value.code == "UNAVAILABLE"
    assert not [n for n in world.nodes if n.startswith("finance-import:")]


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R009.6")
async def test_import_journey_previews_commits_and_replays(world: Engine) -> None:
    sink = Sink()
    ctx = context(sink)
    shown = (await imp.handle_import_preview(ctx, PREVIEW, NO_OP))["value"]
    committed = (
        await imp.handle_import_commit(
            ctx, {"preview_id": shown["preview_id"], "account": "acct-1"}, NO_OP
        )
    )["value"]
    repeated = (
        await imp.handle_import_commit(
            ctx, {"preview_id": shown["preview_id"], "account": "acct-1"}, NO_OP
        )
    )["value"]
    assert (committed["imported"], repeated["imported"]) == (2, 0)
    assert repeated["receipt_id"] == committed["receipt_id"]
    # A re-preview now reports the reimport, and no order request was ever made.
    again = (await imp.handle_import_preview(ctx, PREVIEW, NO_OP))["value"]
    assert again["reimport"] is True
    assert not [n for n in world.nodes if "order" in n]

"""Atlas connection, mapping and run operations (ARBORA-OPS-R911, R922.3)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import atlas
from arbora.api.ops.atlas import AtlasRunState
from arbora.api.registry import Effect, Verb
from tests.api._engine_double import EngineDouble, context

_DIGEST = "sha256:" + "a" * 64
_OPS = {op.id: op for op in atlas.operations()}
_NO_OP: Any = None
_METHODS = (
    "SourceConnectionGet",
    "SourceConnectionList",
    "SourceConnectionControl",
    "SourceMappingGet",
    "SourceMappingControl",
    "ConnectorRuns",
    "ConnectorRunGet",
    "ConnectorRunRecord",
)


def test_every_atlas_operation_is_registered_with_ingest_scopes() -> None:
    assert len(_OPS) == 13
    for op_id, op in _OPS.items():
        want = "ingest:read" if op.verb is Verb.ASK else "ingest:write"
        assert op.scopes == frozenset({want}), op_id
    for op_id in atlas.NATIVE_IDEMPOTENCY:
        assert _OPS[op_id].effect is Effect.WRITE


@pytest.mark.spec("ARBORA-OPS-R911")
def test_mapping_approve_needs_current_digest_and_plan_confirmation() -> None:
    approve = _OPS["ingest.sources.mapping.approve"]
    # Approve is an admin act: it previews a plan and runs only once confirmed.
    assert approve.verb is Verb.ACT and approve.effect is Effect.ADMIN
    assert _OPS["ingest.sources.connections.delete"].effect is Effect.ADMIN
    with pytest.raises(ValidationError):
        atlas.MappingApproveParams(connection_id="c")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        atlas.MappingApproveParams(connection_id="c", mapping_digest="stale")
    assert atlas.MappingApproveParams(connection_id="c", mapping_digest=_DIGEST)


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
    ("op_id", "handler", "params", "method"),
    [
        (
            "ingest.sources.connections.list",
            "handle_connections_list",
            {"limit": 5},
            "SourceConnectionList",
        ),
        (
            "ingest.sources.connections.get",
            "handle_connections_get",
            {"connection_id": "c"},
            "SourceConnectionGet",
        ),
        (
            "ingest.sources.connections.create",
            "handle_connections_create",
            {
                "provider": "p",
                "label": "l",
                "settings": {},
                "credential_ref": "env://K",
            },
            "SourceConnectionGet",
        ),
        (
            "ingest.sources.mapping.get",
            "handle_mapping_get",
            {"connection_id": "c"},
            "SourceMappingGet",
        ),
        (
            "ingest.sources.mapping.approve",
            "handle_mapping_approve",
            {"connection_id": "c", "mapping_digest": _DIGEST},
            "SourceMappingControl",
        ),
        (
            "ingest.sources.runs.get",
            "handle_runs_get",
            {"run_id": "r"},
            "ConnectorRunGet",
        ),
        ("ingest.sources.runs.list", "handle_runs_list", {"limit": 5}, "ConnectorRuns"),
    ],
)
async def test_a_pinned_engine_without_the_method_refuses_naming_it(
    op_id: str, handler: str, params: dict[str, Any], method: str
) -> None:
    with pytest.raises(OperationRefused) as refused:
        await getattr(atlas, handler)(context(), params, _OPS[op_id])
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] in _METHODS


class Atlas:
    """The engine records and the provider connector, as structural doubles."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.connections: dict[str, dict[str, Any]] = {}
        self.mappings: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.connector: list[tuple[str, str, dict[str, Any]]] = []
        self.engine = EngineDouble(monkeypatch)
        for method, handler in {
            "SourceConnectionGet": self._get,
            "SourceConnectionList": self._list,
            "SourceConnectionControl": self._control,
            "SourceMappingGet": lambda p: self.mappings.get(p["connection_id"], {}),
            "SourceMappingControl": self._mapping,
            "ConnectorRuns": self._runs,
            "ConnectorRunGet": lambda p: self.runs.get(p["run_id"], {}),
            "ConnectorRunRecord": self._record,
        }.items():
            self.engine.serve(method, handler)
        self.engine.install(atlas)
        monkeypatch.setattr(atlas, "invoke_registered", self._fleet)

    def _get(self, p: dict[str, Any]) -> Any:
        return self.connections.get(p["connection_id"], {})

    def _list(self, p: dict[str, Any]) -> Any:
        return {"connections": list(self.connections.values()), "next_cursor": None}

    def _control(self, p: dict[str, Any]) -> Any:
        op = p["op"]
        if op["operation"] == "put":
            row = {"state": "active", **op["connection"]}
            self.connections[row["connection_id"]] = row
            return {
                "connection_id": row["connection_id"],
                "deleted": False,
                "replayed": False,
            }
        self.connections.pop(op["connection_id"], None)
        return {
            "connection_id": op["connection_id"],
            "deleted": True,
            "replayed": False,
        }

    def _mapping(self, p: dict[str, Any]) -> Any:
        op = p["op"]
        current = self.mappings.get(op["connection_id"])
        if op["operation"] == "propose":
            digest = "sha256:" + "b" * 64
            self.mappings[op["connection_id"]] = {
                "connection_id": op["connection_id"],
                "digest": digest,
                "mapping": op["mapping"],
                "state": "proposed",
            }
            return {**self.mappings[op["connection_id"]], "replayed": False}
        if current is None:
            raise EngineRefusal("MAPPING_NOT_FOUND")
        if current["digest"] != op["mapping_digest"]:
            raise EngineRefusal("MAPPING_CHANGED")
        current["state"] = "approved"
        return {**current, "replayed": False}

    def _runs(self, p: dict[str, Any]) -> Any:
        rows = [
            r
            for r in self.runs.values()
            if p.get("connection_id") in (None, r["connection_id"])
        ]
        return {"runs": rows[: p["limit"]], "next_cursor": None}

    def _record(self, p: dict[str, Any]) -> Any:
        run = p["run"]
        self.runs[run["run_id"]]["state"] = run["state"]
        return {"run_id": run["run_id"], "state": run["state"], "replayed": False}

    async def _fleet(self, _ctx: Any, op_id: str, arguments: dict[str, Any]) -> Any:
        assert op_id == "fleet.call"
        self.connector.append(
            (arguments["server"], arguments["tool"], arguments["arguments"])
        )
        replies = {
            "providers": {
                "providers": [
                    {"id": "files", "label": "Files", "auth_kinds": ["token"]}
                ]
            },
            "discover": {"job_id": "job-1"},
            "propose_mapping": {"mapping": {"title": "name"}},
            "sync_preview": {"changes": [{"id": "x"}], "counts": {"new": 1}},
        }
        return {"value": replies[arguments["tool"]]}


@pytest.fixture
def served_atlas(monkeypatch: pytest.MonkeyPatch) -> Atlas:
    return Atlas(monkeypatch)


@pytest.mark.spec("ARBORA-OPS-R911")
async def test_connections_are_recorded_in_the_engine_and_credentials_stay_references(
    served_atlas: Atlas,
) -> None:
    made = await atlas.handle_connections_create(
        context(),
        {
            "provider": "files",
            "label": "Docs",
            "settings": {"root": "/d"},
            "credential_ref": "openbao://kv/a/t",
        },
        _NO_OP,
    )
    cid = made["value"]["connection_id"]
    assert made["value"]["state"] == "active"
    retry = await atlas.handle_connections_create(
        context(),
        {
            "provider": "files",
            "label": "Docs",
            "settings": {"root": "/d"},
            "credential_ref": "openbao://kv/a/t",
        },
        _NO_OP,
    )
    assert (
        retry["value"]["connection_id"] == cid
    )  # one record per tenant, provider, label
    other = await atlas.handle_connections_create(
        context("tenant:two"),
        {
            "provider": "files",
            "label": "Docs",
            "settings": {},
            "credential_ref": "env://K",
        },
        _NO_OP,
    )
    assert other["value"]["connection_id"] != cid
    listed = await atlas.handle_connections_list(context(), {"limit": 10}, _NO_OP)
    assert all(
        str(row["credential_ref"]).startswith(("openbao://", "env://"))
        for row in listed["value"]["connections"]
    )
    gone = await atlas.handle_connections_delete(
        context(), {"connection_id": cid}, _NO_OP
    )
    assert gone["value"]["deleted"] is True
    with pytest.raises(OperationRefused) as absent:
        await atlas.handle_connections_get(context(), {"connection_id": cid}, _NO_OP)
    assert absent.value.code == "CONNECTION_NOT_FOUND"


@pytest.mark.spec("ARBORA-OPS-R911")
async def test_discover_returns_a_job_id(served_atlas: Atlas) -> None:
    made = await atlas.handle_connections_create(
        context(),
        {
            "provider": "files",
            "label": "Docs",
            "settings": {},
            "credential_ref": "env://K",
        },
        _NO_OP,
    )
    cid = made["value"]["connection_id"]
    reply = await atlas.handle_discover(context(), {"connection_id": cid}, _NO_OP)
    assert reply == {"value": {"job_id": "job-1"}}
    server, tool, arguments = served_atlas.connector[-1]
    assert (server, tool, arguments["connection_id"]) == ("files", "discover", cid)
    # The job is recorded on the connection through the engine.
    assert served_atlas.connections[cid]["settings"]["discovery_job_id"] == "job-1"
    preview = await atlas.handle_sync_preview(context(), {"connection_id": cid}, _NO_OP)
    assert preview["value"] == {"changes": [{"id": "x"}], "counts": {"new": 1}}
    providers = await atlas.handle_providers_list(context(), {}, _NO_OP)
    assert providers["value"]["providers"][0]["id"] == "files"
    with pytest.raises(OperationRefused) as unknown:
        await atlas.handle_discover(context(), {"connection_id": "nope"}, _NO_OP)
    assert unknown.value.code == "CONNECTION_NOT_FOUND"


@pytest.mark.spec("ARBORA-OPS-R911")
async def test_mapping_is_proposed_then_approved_against_the_digest_read(
    served_atlas: Atlas,
) -> None:
    made = await atlas.handle_connections_create(
        context(),
        {
            "provider": "files",
            "label": "Docs",
            "settings": {},
            "credential_ref": "env://K",
        },
        _NO_OP,
    )
    cid = made["value"]["connection_id"]
    with pytest.raises(OperationRefused) as none_yet:
        await atlas.handle_mapping_get(context(), {"connection_id": cid}, _NO_OP)
    assert none_yet.value.code == "MAPPING_NOT_FOUND"
    proposed = await atlas.handle_mapping_propose(
        context(), {"connection_id": cid}, _NO_OP
    )
    assert proposed["value"]["state"] == "proposed"
    assert proposed["value"]["mapping"] == {"title": "name"}  # the connector derived it
    read = await atlas.handle_mapping_get(context(), {"connection_id": cid}, _NO_OP)
    with pytest.raises(EngineRefusal) as stale:
        await atlas.handle_mapping_approve(
            context(),
            {"connection_id": cid, "mapping_digest": "sha256:" + "c" * 64},
            _NO_OP,
        )
    assert stale.value.code == "MAPPING_CHANGED"
    approved = await atlas.handle_mapping_approve(
        context(),
        {"connection_id": cid, "mapping_digest": read["value"]["digest"]},
        _NO_OP,
    )
    assert approved["value"] == {"state": "approved"}


@pytest.mark.spec("ARBORA-OPS-R922.3")
async def test_runs_are_listed_read_and_cancelled_through_the_run_records(
    served_atlas: Atlas,
) -> None:
    op = _OPS["ingest.sources.runs.list"]
    assert op.scopes == frozenset({"ingest:read"})
    assert (
        atlas.RunsListParams(state=AtlasRunState("failed"), connection_id="c").limit
        == 25
    )
    with pytest.raises(ValidationError):
        atlas.RunsListParams(state="stuck")  # type: ignore[arg-type]
    served_atlas.runs["r1"] = {
        "run_id": "r1",
        "connection_id": "c1",
        "state": "running",
        "started_ms": 1_000,
        "counts": {"seen": 2},
    }
    served_atlas.runs["r2"] = {
        "run_id": "r2",
        "connection_id": "c2",
        "state": "succeeded",
        "started_ms": 2_000,
        "finished_ms": 3_000,
    }
    page = await atlas.handle_runs_list(
        context(),
        {"limit": 1, "connection_id": None, "state": None, "cursor": None},
        _NO_OP,
    )
    assert page["value"]["truncated"] is True and len(page["value"]["runs"]) == 1
    assert page["value"]["runs"][0]["started_at"] == "1970-01-01T00:00:01.000Z"
    one = await atlas.handle_runs_get(context(), {"run_id": "r2"}, _NO_OP)
    assert one["value"]["finished_at"] == "1970-01-01T00:00:03.000Z"
    cancelled = await atlas.handle_runs_cancel(context(), {"run_id": "r1"}, _NO_OP)
    assert cancelled["value"] == {"run_id": "r1", "state": "cancelled"}
    with pytest.raises(OperationRefused) as absent:
        await atlas.handle_runs_get(context(), {"run_id": "zz"}, _NO_OP)
    assert absent.value.code == "CONNECTOR_RUN_NOT_FOUND"


async def test_a_fleet_without_the_atlas_tool_refuses_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def absent(_ctx: Any, _op_id: str, arguments: dict[str, Any]) -> Any:
        return {"value": {"error": f"'{arguments['server']}' is not in the catalog"}}

    monkeypatch.setattr(atlas, "invoke_registered", absent)
    with pytest.raises(OperationRefused) as refused:
        await atlas.handle_providers_list(
            context(), {}, _OPS["ingest.sources.providers.list"]
        )
    assert refused.value.code == "UNAVAILABLE"
    assert atlas.ATLAS_SERVER in refused.value.details["reason"]

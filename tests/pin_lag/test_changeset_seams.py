"""The production seams of the changeset ports (ARBORA-OPS-R894).

A run's target comes from the source grant on its work item; blob staging streams through
the engine blob methods; the plan-gated ``source.commit`` and ``source.land`` run under the
plan reference the pipeline mints. The submit test installs a structural double of
meristem ``main``'s changeset cycle, as the sibling port tests do.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.agent_host import changeset_seams as seams
from arbora.agent_host.host_port_composition import PortBackends, compose_host_ports
from arbora.api.domain.agent_host import host_port_adapters as adapters
from tests.pin_lag.test_changeset_ports import (  # noqa: F401
    TARGET,
    _Pending,
    _seams,
    cycle_shape,
)
from tests.pin_lag.test_host_ports_pin_lag import main_shape  # noqa: F401

GRANT = {"repository": "repo-a", "branch": "feature", "prefixes": ["src", "docs"]}


@pytest.mark.spec("ARBORA-OPS-R894")
def test_target_is_read_from_the_source_grant_on_the_work_item() -> None:
    claim = {"metadata": {seams.SOURCE_GRANT_KEY: GRANT}}
    assert seams.source_target(claim) == adapters.SourceTarget(
        "repo-a", "feature", ("src", "docs")
    )
    for broken in (
        None,
        {},
        {"metadata": {}},
        {"metadata": {seams.SOURCE_GRANT_KEY: {**GRANT, "prefixes": []}}},
        {"metadata": {seams.SOURCE_GRANT_KEY: {**GRANT, "prefixes": "src"}}},
        {"metadata": {seams.SOURCE_GRANT_KEY: {**GRANT, "branch": ""}}},
        {"metadata": {seams.SOURCE_GRANT_KEY: {**GRANT, "repository": 3}}},
        {"metadata": {seams.SOURCE_GRANT_KEY: {**GRANT, "prefixes": ["src", 1]}}},
    ):
        assert seams.source_target(broken) is None


@pytest.mark.spec("ARBORA-OPS-R894")
def test_only_an_isolated_backend_is_the_sandbox() -> None:
    assert seams.is_isolated_backend(SimpleNamespace(name="docker"))
    assert not seams.is_isolated_backend(SimpleNamespace(name="local"))
    assert not seams.is_isolated_backend(object())


@pytest.mark.spec("ARBORA-OPS-R894")
def test_blob_staging_streams_chunks_and_answers_the_engine_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(seams, "BLOB_CHUNK_BYTES", 4)
    sent: list[tuple[str, dict[str, Any], str]] = []

    async def send(method: str, params: dict[str, Any], tenant: str) -> Any:
        sent.append((method, params, tenant))
        return {"BlobBegin": "cur-1", "BlobCommit": "sha256:" + "a" * 64}.get(method)

    stage = seams.engine_blob_stager(send, lambda run: "t1" if run == "run:1" else "")
    staged = asyncio.run(stage("run:1", b"0123456789"))
    assert staged == {"digest": "sha256:" + "a" * 64, "size": 10}
    assert [m for m, _, _ in sent] == [
        "BlobBegin",
        "BlobChunkPut",
        "BlobChunkPut",
        "BlobChunkPut",
        "BlobCommit",
    ]
    assert [p["data"] for m, p, _ in sent if m == "BlobChunkPut"] == [
        b"0123",
        b"4567",
        b"89",
    ]
    assert {t for _, _, t in sent} == {"t1"}
    with pytest.raises(PermissionError):
        asyncio.run(stage("run:2", b"x"))


@pytest.mark.spec("ARBORA-OPS-R894")
def test_source_transport_serves_tree_and_blob_and_refuses_without_a_head() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    tree = {
        "found": True,
        "head_revision": "rev-7",
        "entries": [
            {"path": "src/a.py", "kind": "file", "content_digest": "d" * 64},
            {"path": "src/pkg", "kind": "directory"},
        ],
    }
    blob = {"found": True, "content": "print(1)\n", "content_encoding": "utf-8"}

    async def call(op: str, params: Mapping[str, Any], run: str) -> Mapping[str, Any]:
        calls.append((op, dict(params)))
        value = {"source.tree": tree, "source.blob": blob}[op]
        return {"outcome": "answered", "payload": {"value": value}}

    transport = seams.ServedSourceTransport(call, "run:1")
    page = asyncio.run(
        transport.call(
            "tree", {"repository_id": "repo-a", "ref": "feature", "path_prefix": "src"}
        )
    )
    assert page["status"] == "ok" and page["result"]["revision"] == "rev-7"
    assert page["result"]["entries"][0]["mode"] == "100644"
    assert page["result"]["entries"][1]["mode"] == "040000"
    read = asyncio.run(
        transport.call(
            "blob", {"repository_id": "repo-a", "ref": "rev-7", "path": "src/a.py"}
        )
    )
    assert base64.b64decode(read["result"]["content_base64"]) == b"print(1)\n"
    assert calls[0] == (
        "source.tree",
        {"repository": "repo-a", "ref": "feature", "path": "src"},
    )

    tree.pop("head_revision")
    refused = asyncio.run(
        transport.call("tree", {"repository_id": "r", "ref": "f", "path_prefix": None})
    )
    assert refused["status"] == "refused"
    assert asyncio.run(transport.call("diff", {}))["status"] == "refused"


@pytest.mark.spec("ARBORA-OPS-R894")
def test_production_seams_read_the_run_owner_and_bind_both_ports(
    main_shape: None,  # noqa: F811
) -> None:
    owner = SimpleNamespace(
        run_id="run:1",
        claim={"metadata": {seams.SOURCE_GRANT_KEY: GRANT}},
        session=SimpleNamespace(tenant="t1"),
    )

    def resolve(run: str) -> Any:
        if run == "run:boom":
            raise RuntimeError("no run is executing")
        return owner if run == "run:1" else None

    async def call(op: str, params: Mapping[str, Any], run: str) -> Mapping[str, Any]:
        return {}

    async def send(method: str, params: dict[str, Any], tenant: str) -> Any:
        return "x"

    built = seams.production_changeset_seams(call, send, resolve)
    assert built.target("run:1") == adapters.SourceTarget(
        "repo-a", "feature", ("src", "docs")
    )
    assert built.target("run:2") is None and built.target("run:boom") is None

    async def wait(seconds: float) -> None:
        return None

    bound = compose_host_ports(
        object(),
        wait=wait,
        backends=PortBackends(changeset=built, sandbox_verified=lambda: True),
    )
    assert bound.ports.changeset_submitter("run:1") is not None
    assert bound.ports.changeset_submitter("run:2") is None
    assert bound.ports.gate_runner("run:1") is not None
    assert bound.ports.gate_runner("run:2") is None
    unverified = compose_host_ports(
        object(),
        wait=wait,
        backends=PortBackends(changeset=built, sandbox_verified=lambda: False),
    )
    assert unverified.ports.changeset_submitter("run:1") is None


@pytest.mark.spec("ARBORA-OPS-R894")
def test_commit_and_land_run_under_the_plan_reference_the_pipeline_mints(
    cycle_shape: None,  # noqa: F811
) -> None:
    seen: list[tuple[str, str | None]] = []

    async def call(
        op: str, params: Mapping[str, Any], run: str, plan_ref: str | None = None
    ) -> Mapping[str, Any]:
        seen.append((op, plan_ref))
        if plan_ref is None:
            return {
                "outcome": "answered",
                "code": "CONFIRMATION_REQUIRED",
                "details": {"plan_ref": f"plan:{op}", "op": op},
                "payload": {"value": None},
            }
        value = {"changeset": {"id": "cs-1"}} if op == "source.commit" else {}
        return {"outcome": "answered", "payload": {"value": value}}

    submitter = adapters.ChangesetSubmitter(call, _seams(), "run:1", TARGET)
    result = asyncio.run(submitter.submit(_Pending("h", (), "run:1", (("a", "x"),))))
    assert getattr(result, "changeset_id", None) == "cs-1"
    assert seen == [
        ("source.commit", None),
        ("source.commit", "plan:source.commit"),
        ("source.land", None),
        ("source.land", "plan:source.land"),
    ]


@pytest.mark.spec("ARBORA-OPS-R894")
def test_an_unconfirmed_commit_returns_the_refusal_not_a_submission(
    cycle_shape: None,  # noqa: F811
) -> None:
    async def call(
        op: str, params: Mapping[str, Any], run: str, plan_ref: str | None = None
    ) -> Mapping[str, Any]:
        if plan_ref is None:
            return {
                "outcome": "answered",
                "code": "CONFIRMATION_REQUIRED",
                "details": {"plan_ref": "plan:1"},
                "payload": {"value": None},
            }
        return {"outcome": "refused", "refusal_code": "APPROVAL_REQUIRED"}

    submitter = adapters.ChangesetSubmitter(call, _seams(), "run:1", TARGET)
    result = asyncio.run(submitter.submit(_Pending("h", (), "run:1", (("a", "x"),))))
    assert getattr(result, "paths", None) == ()


@pytest.mark.spec("ARBORA-OPS-R894")
def test_changeset_cycle_commits_on_the_head_the_source_tree_served(
    cycle_shape: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The head the engine's ``source.tree`` states is the ``base_head`` of the commit."""
    from arbora.api.ops.source import SourceTreeOutcome

    state = {"head": "rev:head-9"}
    sent: list[tuple[str, Mapping[str, Any]]] = []

    async def call(
        op: str, params: Mapping[str, Any], run: str, plan_ref: str | None = None
    ) -> Mapping[str, Any]:
        sent.append((op, dict(params)))
        if op == "source.tree":
            outcome = SourceTreeOutcome(
                tenant="t1",
                repository=params["repository"],
                ref=params["ref"],
                path=params["path"],
                found=True,
                head_revision=state["head"],
            )
            return {
                "outcome": "answered",
                "payload": {"value": outcome.model_dump(mode="json")},
            }
        if plan_ref is None:
            return {
                "outcome": "answered",
                "code": "CONFIRMATION_REQUIRED",
                "details": {"plan_ref": f"plan:{op}"},
                "payload": {"value": None},
            }
        value = {"changeset": {"id": "cs-1"}} if op == "source.commit" else {}
        return {"outcome": "answered", "payload": {"value": value}}

    transport = seams.ServedSourceTransport(call, "run:1")
    page = asyncio.run(
        transport.call(
            "tree", {"repository_id": "repo-a", "ref": "feature", "path_prefix": None}
        )
    )
    assert page["status"] == "ok"
    submitter = adapters.ChangesetSubmitter(call, _seams(), "run:1", TARGET)
    result = asyncio.run(
        submitter.submit(
            _Pending(page["result"]["revision"], (), "run:1", (("a", "x"),))
        )
    )
    assert getattr(result, "changeset_id", None) == "cs-1"
    commit = next(params for op, params in sent if op == "source.commit")
    assert commit["base_head"] == "rev:head-9"
    assert [op for op, _ in sent if op != "source.tree"][-1] == "source.land"
    # Behind pin-lag the projection states no head: the transport refuses, no base exists.
    state["head"] = None  # type: ignore[assignment]
    refused = asyncio.run(
        transport.call(
            "tree", {"repository_id": "repo-a", "ref": "feature", "path_prefix": None}
        )
    )
    assert refused == {"status": "refused", "reason": "not_found"}

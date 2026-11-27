"""Prompt library operations over engine KV (ARBORA-OPS-R912)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import prompts
from arbora.api.ops.registry_factory import get_registry

pytestmark = pytest.mark.spec("ARBORA-OPS-R912")


class FakeKv:
    """Versioned KV with the engine's Get/Cas/Scan semantics."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[bytes, int]] = {}

    async def send(
        self, _context: Any, method: str, params: dict[str, Any], _key: Any
    ) -> Any:
        assert params["namespace"] == prompts.EG_NAMESPACE
        if method == "KvGet":
            row = self.rows.get(params["key"])
            return {} if row is None else {"value": row[0], "version": row[1]}
        if method == "KvCas":
            current = self.rows.get(params["key"], (b"", 0))[1]
            if current != params["expected_version"]:
                return {"swapped": False, "version": current}
            self.rows[params["key"]] = (params["new"], current + 1)
            return {"swapped": True, "version": current + 1}
        if method == "KvScan":
            keys = sorted(k for k in self.rows if k.startswith(params["prefix"]))
            entries = [
                {"key": k, "value": self.rows[k][0], "version": self.rows[k][1]}
                for k in keys
            ]
            return {"entries": entries, "next_cursor": None}
        raise AssertionError(method)


@pytest.fixture
def kv(monkeypatch: pytest.MonkeyPatch) -> FakeKv:
    fake = FakeKv()
    monkeypatch.setattr(prompts, "_send", fake.send)
    return fake


def _context(key: str) -> Any:
    return SimpleNamespace(
        caller=SimpleNamespace(principal="user:alice"), idempotency_key=key
    )


def _call(name: str, params: dict[str, Any], key: str = "k") -> dict[str, Any]:
    handler = getattr(prompts, f"handle_prompts_{name}")
    return asyncio.run(handler(_context(key), params, None))["value"]


def test_rollback_appends_a_version_and_diff_matches(kv: FakeKv) -> None:
    created = _call("create", {"id": "p1", "name": "Greeter", "body": "hello\n"})
    assert created["current"] == 1
    updated = _call(
        "update",
        {"id": "p1", "body": "hi\n", "expected_revision": created["revision"]},
        "k2",
    )
    assert updated["current"] == 2
    rolled = _call(
        "rollback",
        {"id": "p1", "version_id": 1, "expected_revision": updated["revision"]},
        "k3",
    )
    assert rolled["current"] == 3
    assert _call("get", {"id": "p1"})["body"] == "hello\n"
    versions = _call("versions", {"id": "p1"})["versions"]
    assert [v["version_id"] for v in versions] == [1, 2, 3]
    assert {v["author"] for v in versions} == {"user:alice"}
    diff = _call("diff", {"id": "p1", "a": 2, "b": 3})["diff"]
    assert "-hi" in diff and "+hello" in diff
    assert _call("diff", {"id": "p1", "a": 1, "b": 3})["diff"] == ""
    listed = _call("list", {})["prompts"]
    assert [(p["id"], p["name"], p["current"]) for p in listed] == [
        ("p1", "Greeter", 3)
    ]
    stored = json.loads(kv.rows["p1/v/1"][0])
    assert stored["body"] == "hello\n"


def test_stale_revision_and_missing_prompt_are_refused(kv: FakeKv) -> None:
    created = _call("create", {"id": "p1", "name": "A", "body": "x"})
    with pytest.raises(OperationRefused) as stale:
        _call("update", {"id": "p1", "body": "y", "expected_revision": 99}, "k2")
    assert stale.value.code == "REVISION_CONFLICT"
    with pytest.raises(OperationRefused) as again:
        _call("create", {"id": "p1", "name": "A", "body": "x"}, "k3")
    assert again.value.code == "REVISION_CONFLICT"
    with pytest.raises(OperationRefused) as missing:
        _call("get", {"id": "nope"})
    assert missing.value.code == "NOT_FOUND"
    assert created["revision"] == 1


def test_prompt_operations_are_registered_with_storage_scopes() -> None:
    registry = get_registry()
    for op_id, scope in (
        ("prompts.list", "kv:read"),
        ("prompts.get", "kv:read"),
        ("prompts.versions", "kv:read"),
        ("prompts.diff", "kv:read"),
        ("prompts.create", "kv:write"),
        ("prompts.update", "kv:write"),
        ("prompts.rollback", "kv:write"),
    ):
        assert registry[op_id].scopes == frozenset({scope})


@pytest.mark.spec("ARBORA-OPS-R922.4")
async def test_active_prompt_resolves_the_session_agent_and_truncates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.api.ops import agent_library

    sent: list[tuple[str, dict[str, Any]]] = []
    body = {"text": b"x" * (prompts._ACTIVE_LIMIT + 5)}

    async def library(
        _context: Any, method: str, params: dict[str, Any], **_k: Any
    ) -> Any:
        sent.append((method, params["op"]))
        if method == "AgentLibrary":
            return {
                "entry": {
                    "agent_id": params["op"]["agent_id"],
                    "role": "researcher",
                    "entry_revision": 4,
                    "system_prompt": {"component_id": "prompt:one"},
                }
            }
        return {"body": body["text"]}

    monkeypatch.setattr(agent_library, "engine_send", library)
    import arbora.api.ops.engine_calls as engine

    monkeypatch.setattr(engine, "engine_send", library)
    context = SimpleNamespace(
        caller=SimpleNamespace(tenant="tenant:one", agent_id="agent:session")
    )
    result = await prompts.handle_prompts_active_get(context, {}, None)  # type: ignore[arg-type]
    assert result["value"]["agent_id"] == "agent:session"
    assert result["value"]["profile"] == "researcher"
    assert result["value"]["revision"] == 4
    assert result["value"]["truncated"] is True
    assert len(result["value"]["system_prompt"]) == prompts._ACTIVE_LIMIT
    assert sent[1] == (
        "AgentComponent",
        {
            "op": "content",
            "request": {"component_id": "prompt:one", "tenant_id": "tenant:one"},
        },
    )
    body["text"] = b"short"
    short = await prompts.handle_prompts_active_get(
        context,
        {"agent_id": "agent:other"},
        None,  # type: ignore[arg-type]
    )
    assert (
        short["value"]["system_prompt"] == "short"
        and short["value"]["truncated"] is False
    )
    with pytest.raises(OperationRefused) as refused:
        await prompts.handle_prompts_active_get(
            SimpleNamespace(caller=SimpleNamespace(tenant="t")),
            {},
            None,  # type: ignore[arg-type]
        )
    assert refused.value.code == "AGENT_NOT_FOUND"

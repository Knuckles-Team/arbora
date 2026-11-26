"""Skill hooks report skill use with no prompt text (ARBORA-HARNESS-PLUGINS-R012)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from arbora.api.otlp_logs import events_from_logs

from arbora.harness_plugins import hook_runner
from arbora.harness_plugins.compose import compose_bundle
from arbora.harness_plugins.login import Credential, CredentialState, TokenStore
from arbora.harness_plugins.profiles import Lifecycle, profile
from arbora.harness_plugins.render import render
from tests.harness_plugins.conftest import ENDPOINT

ALLOWED = {
    "skill.ref",
    "skill.version",
    "skill.chosen_by",
    "session.run_id",
    "skill.outcome",
    "skill.error_code",
}


def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TokenStore:
    monkeypatch.setenv("ARBORA_PLUGIN_FILE_STORE", "1")
    store = TokenStore(tmp_path)
    store.save(
        Credential(
            "claude-code",
            ENDPOINT,
            "https://id/token",
            "r",
            "access-1",
            4e9,
            CredentialState.AUTHORIZED,
        )
    )
    return store


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R012")
def test_hooks_carry_only_the_listed_attributes(
    skill_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both renderers emit one skill-end hook that runs the command runner.
    tree = tmp_path / "cc"
    render(
        compose_bundle(
            profile("claude-code"), base_url=ENDPOINT, skill_roots=[skill_root]
        ),
        tree,
    )
    groups = json.loads((tree / "hooks/hooks.json").read_text())["hooks"]["PostToolUse"]
    assert len(groups) == 1 and groups[0]["matcher"] == "Skill"
    assert groups[0]["hooks"][0]["type"] == "command"
    assert groups[0]["hooks"][0]["command"].endswith("skill_end --harness claude-code")
    oc = tmp_path / "oc"
    render(
        compose_bundle(
            profile("opencode"), base_url=ENDPOINT, skill_roots=[skill_root]
        ),
        oc,
    )
    bundle = json.loads((oc / "bundle.json").read_text())
    assert bundle["hooks"]["tool.execute.after"] == "skill_end"
    assert "tool.execute.after" in (oc / "server.ts").read_text()

    # The runner posts one log record; the body holds the listed attributes only.
    calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def post(
        url: str, body: Mapping[str, Any], headers: Mapping[str, str]
    ) -> httpx.Response:
        calls.append((url, dict(body), dict(headers)))
        return httpx.Response(200, json={"ok": True})

    hook = {
        "session_id": "sess-1",
        "prompt": "my secret prompt",
        "tool_name": "Skill",
        "tool_input": {"skill": "skill:a", "args": "private argument"},
        "tool_response": {"output": "private output"},
    }
    out = hook_runner.run(
        Lifecycle.SKILL_END,
        profile("claude-code"),
        json.dumps(hook),
        base_url=ENDPOINT,
        store=_store(tmp_path, monkeypatch),
        post=post,
        now_ns=lambda: 5_000_000_000,
    )
    assert out == ""
    url, body, headers = calls[0]
    assert url == f"{ENDPOINT}/api/v1/logs" and headers["Authorization"].startswith(
        "Bearer "
    )
    text = json.dumps(body)
    assert "secret" not in text and "private" not in text
    record = body["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert {a["key"] for a in record["attributes"]} <= ALLOWED
    assert not record.get("body")
    # The door's own mapper accepts exactly this body, and the agent chose it.
    (event,) = events_from_logs("tenant:one", body["resourceLogs"])
    assert event["chosen_by"] == "agent" and event["outcome"] == "ok"
    assert event["skill_ref"] == "skill:a" and event["run_id"] == "sess-1"
    # A failed skill reports the error outcome and no message.
    hook_runner.run(
        Lifecycle.SKILL_END,
        profile("claude-code"),
        json.dumps({**hook, "tool_response": {"is_error": True}}),
        base_url=ENDPOINT,
        store=_store(tmp_path, monkeypatch),
        post=post,
        now_ns=lambda: 6_000_000_000,
    )
    (failed,) = events_from_logs("tenant:one", calls[1][1]["resourceLogs"])
    assert failed["outcome"] == {"error": {"code": "SKILL_FAILED"}}


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R012")
def test_a_skill_hook_without_a_credential_sends_nothing_and_never_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARBORA_PLUGIN_FILE_STORE", "1")
    calls: list[Any] = []

    def post(*args: Any) -> httpx.Response:
        calls.append(args)
        raise AssertionError("no credential, no call")

    out = hook_runner.run(
        Lifecycle.SKILL_END,
        profile("claude-code"),
        json.dumps({"session_id": "s", "tool_input": {"skill": "x"}}),
        base_url=ENDPOINT,
        store=TokenStore(tmp_path),
        post=post,
    )
    assert out == "" and calls == []

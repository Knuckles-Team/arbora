"""The command hook runner (ARBORA-HARNESS-PLUGINS-R007, R003.4)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from arbora.harness_plugins import hook_runner
from arbora.harness_plugins.login import Credential, CredentialState, TokenStore
from arbora.harness_plugins.profiles import Lifecycle, profile
from tests.harness_plugins.conftest import ENDPOINT


class Door:
    def __init__(self, status: int = 200, context: str = "last checkpoint") -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.status = status
        self.context = context

    def __call__(
        self, url: str, body: Mapping[str, Any], headers: Mapping[str, str]
    ) -> httpx.Response:
        self.calls.append((url, dict(body), dict(headers)))
        payload = {
            "ok": True,
            "result": {"value": {"status": "attached", "context": self.context}},
        }
        return httpx.Response(self.status, json=payload)


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R007")
def test_runner_maps_events_and_always_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARBORA_PLUGIN_FILE_STORE", "1")
    store = TokenStore(tmp_path)
    store.save(
        Credential(
            "codex",
            ENDPOINT,
            "https://id/token",
            "r",
            "access-1",
            4e9,
            CredentialState.AUTHORIZED,
        )
    )
    door = Door()
    hook = json.dumps({"session_id": "cx-1", "cwd": "/w/p", "trigger": "auto"})
    out = hook_runner.run(
        Lifecycle.FIRST_PROMPT,
        profile("codex"),
        hook,
        base_url=ENDPOINT,
        store=store,
        post=door,
    )
    assert json.loads(out) == {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "last checkpoint",
        }
    }
    hook_runner.run(
        Lifecycle.BEFORE_COMPACTION,
        profile("codex"),
        hook,
        base_url=ENDPOINT,
        store=store,
        post=door,
    )
    hook_runner.run(
        Lifecycle.SESSION_END,
        profile("codex"),
        hook,
        base_url=ENDPOINT,
        store=store,
        post=door,
    )
    assert [c[0] for c in door.calls] == [
        f"{ENDPOINT}/api/v1/ops/workstream.{op}"
        for op in ("attach", "checkpoint", "detach")
    ]
    assert door.calls[0][1] == {
        "harness": "codex",
        "harness_session_id": "cx-1",
        "workspace_root": "/w/p",
    }
    assert door.calls[1][1]["trigger"] == "compact"
    assert all(c[2] == {"Authorization": "Bearer access-1"} for c in door.calls)
    opencode = hook_runner.run(
        Lifecycle.FIRST_PROMPT,
        profile("opencode"),
        hook,
        base_url=ENDPOINT,
        store=store,
        post=door,
    )
    assert json.loads(opencode) == {"context": "last checkpoint"}

    def broken(*_a: object) -> httpx.Response:
        raise httpx.ConnectError("down")

    for stdin in ("not json", hook):
        assert (
            hook_runner.run(
                Lifecycle.FIRST_PROMPT,
                profile("codex"),
                stdin,
                base_url=ENDPOINT,
                store=store,
                post=broken,
            )
            == ""
        )


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R003.4")
def test_missing_credential_is_a_non_blocking_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARBORA_PLUGIN_FILE_STORE", "1")
    store = TokenStore(tmp_path)
    hook = json.dumps({"session_id": "g-1", "cwd": "/w"})
    door = Door(status=401)
    out = hook_runner.run(
        Lifecycle.FIRST_PROMPT,
        profile("grok"),
        hook,
        base_url=ENDPOINT,
        store=store,
        post=door,
    )
    assert out == hook_runner.auth_required_note(profile("grok"))
    assert "AUTH_REQUIRED" in out and "\n" not in out and door.calls[0][2] == {}
    store.save(
        Credential(
            "grok", ENDPOINT, "https://id/token", "r", "", 0, CredentialState.EXPIRED
        )
    )
    out = hook_runner.run(
        Lifecycle.FIRST_PROMPT,
        profile("grok"),
        hook,
        base_url=ENDPOINT,
        store=store,
        post=Door(),
    )
    assert "AUTH_REQUIRED" in out

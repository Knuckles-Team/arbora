"""The effect path reaches the session policy through the registered route (R001.5)."""

from __future__ import annotations

import acp
import pytest

from arbora.acp import adapter
from tests.acp.coding_fixture import coding_connection, coding_server  # noqa: F401


@pytest.mark.spec("ARBORA-ACP-R001.5")
async def test_effect_path_reaches_decide_effect(coding_server, monkeypatch):  # noqa: F811
    http, root = coding_server
    decided: list[tuple[str, str | None]] = []
    original = adapter.CodingSessionAdmission.decide_effect

    def spy(self, session, method, **kwargs):
        decision = original(self, session, method, **kwargs)
        decided.append((method, kwargs.get("permission_answer")))
        return decision

    monkeypatch.setattr(adapter.CodingSessionAdmission, "decide_effect", spy)
    async with coding_connection(http, root, "allow_once") as (conn, client):
        session = await conn.new_session(cwd=str(root))
        await conn.prompt(
            session_id=session.session_id, prompt=[acp.text_block("write")]
        )

    # The write asked the client first, and only then was forwarded.
    assert ("fs/write_text_file", None) in decided
    assert ("fs/write_text_file", "allow_once") in decided
    assert client.events == ["permission", "write"]
    assert client.writes == [(str(root / "out.txt"), "written")]


@pytest.mark.spec("ARBORA-ACP-R001.5")
def test_trusted_roots_come_from_the_config_key(monkeypatch):
    from arbora.acp.coding_session import trusted_roots_from_config

    monkeypatch.delenv("ACP_TRUSTED_ROOTS", raising=False)
    assert trusted_roots_from_config() == frozenset()  # default admits nothing
    monkeypatch.setenv("ACP_TRUSTED_ROOTS", '["/work/a", "/work/b"]')
    assert trusted_roots_from_config() == frozenset({"/work/a", "/work/b"})
    monkeypatch.setenv("ACP_TRUSTED_ROOTS", '["relative"]')
    with pytest.raises(ValueError, match="absolute"):
        trusted_roots_from_config()

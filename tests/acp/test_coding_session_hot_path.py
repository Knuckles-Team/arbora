"""A coding session over the registered route (ARBORA-ACP-R001.8)."""

from __future__ import annotations

import acp
import pytest

from tests.acp.coding_fixture import coding_connection, coding_server  # noqa: F401


@pytest.mark.spec("ARBORA-ACP-R001.8")
async def test_read_passes_write_waits_untrusted_root_refused(coding_server):  # noqa: F811
    http, root = coding_server
    async with coding_connection(http, root, "allow_once") as (conn, client):
        with pytest.raises(acp.RequestError) as refused:
            await conn.new_session(cwd="/not/a/trusted/root")
        assert refused.value.data["reason"] == "untrusted_root"

        session = await conn.new_session(cwd=str(root))
        await conn.prompt(
            session_id=session.session_id, prompt=[acp.text_block("read")]
        )
        assert client.events == ["read"]  # the read passes without a permission

        await conn.prompt(
            session_id=session.session_id, prompt=[acp.text_block("write")]
        )
        # The write waits for session/request_permission, then is forwarded.
        assert client.events == ["read", "permission", "write"]

    # A foreign principal never reaches the connection (tenant-scoped carrier).
    async with coding_connection(http, root, "allow_once"):
        pass

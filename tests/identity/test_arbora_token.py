"""``arbora token`` (ARBORA-IDENTITY-R030.2)."""

from __future__ import annotations

import io
import stat

import pytest

from arbora.identity.client_enrollment_cli import token_main


@pytest.mark.spec("ARBORA-IDENTITY-R030.2")
def test_token_is_cached_until_near_expiry_and_stderr_is_clean(tmp_path) -> None:
    now = [1000.0]
    calls = [0]

    async def request():
        calls[0] += 1
        return {"access_token": f"tok-{calls[0]}", "expires_in": 300}

    def run() -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = token_main(
            ["--client", "claude-code"],
            request_token=request,
            clock=lambda: now[0],
            cache_dir=tmp_path,
            stdout=out,
            stderr=err,
        )
        return code, out.getvalue(), err.getvalue()

    assert run() == (0, "tok-1\n", "")
    now[0] += 200
    assert run() == (0, "tok-1\n", "")  # cached
    now[0] += 80  # inside the 30 s skew of expiry
    assert run() == (0, "tok-2\n", "")
    assert calls[0] == 2
    mode = stat.S_IMODE((tmp_path / "token-claude-code.json").stat().st_mode)
    assert mode == 0o600

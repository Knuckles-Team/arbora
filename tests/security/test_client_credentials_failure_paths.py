"""R039.12: every credential failure path logs its traceback and degrades as before."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from arbora.core.settings import RuntimeConfig
from arbora.security import client_credentials as cc


class _Provider:
    """A token provider whose mint fails after the first success."""

    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls = 0
        self.fail_first = fail_first
        self.access_token_ttl = 300.0

    def get_token(self, force: bool = False) -> str:
        self.calls += 1
        if self.fail_first or force:
            raise OSError("idp unreachable")
        return "tok"


def _request() -> httpx.Request:
    return httpx.Request("GET", "https://child.invalid/mcp")


def _drive_401(flow: Any) -> list[httpx.Request]:
    sent = [next(flow)]
    try:
        flow.send(httpx.Response(401, request=sent[0]))
    except StopIteration:
        return sent
    return sent


def _traced(caplog: pytest.LogCaptureFixture) -> bool:
    return any(record.exc_info for record in caplog.records)


def test_a_failed_force_remint_degrades_to_the_401_and_logs_the_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    auth = cc.ClientCredentialsAuth(cast(Any, _Provider()))
    with caplog.at_level(logging.WARNING):
        sent = _drive_401(auth.auth_flow(_request()))
    assert len(sent) == 1
    assert _traced(caplog)


def test_a_failed_first_mint_raises_a_stable_error_and_logs_the_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    auth = cc.ClientCredentialsAuth(cast(Any, _Provider(fail_first=True)))
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="mint"):
        next(auth.auth_flow(_request()))
    assert _traced(caplog)


async def test_the_async_flow_logs_both_mint_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    auth = cc.ClientCredentialsAuth(cast(Any, _Provider()))
    flow = auth.async_auth_flow(_request())
    request = await flow.__anext__()
    with caplog.at_level(logging.WARNING):
        with pytest.raises(StopAsyncIteration):
            await flow.asend(httpx.Response(401, request=request))
    assert _traced(caplog)

    failing = cc.ClientCredentialsAuth(cast(Any, _Provider(fail_first=True)))
    caplog.clear()
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="mint"):
        await failing.async_auth_flow(_request()).__anext__()
    assert _traced(caplog)


def test_a_vanished_token_file_degrades_to_the_401(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "token"
    path.write_text("abc")
    path.chmod(0o600)
    flow = cc.RotatingFileBearerAuth(path).auth_flow(_request())
    request = next(flow)
    path.unlink()
    with caplog.at_level(logging.WARNING), pytest.raises(StopIteration):
        flow.send(httpx.Response(401, request=request))
    assert request.headers["Authorization"] == "Bearer abc"


async def test_a_vanished_token_file_degrades_in_the_async_flow(
    tmp_path: Path,
) -> None:
    path = tmp_path / "token"
    path.write_text("abc")
    path.chmod(0o600)
    flow = cc.RotatingFileBearerAuth(path).async_auth_flow(_request())
    request = await flow.__anext__()
    path.unlink()
    with pytest.raises(StopAsyncIteration):
        await flow.asend(httpx.Response(401, request=request))


def test_the_oidc_header_refuses_with_a_stable_error_when_the_mint_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(cc, "get_provider", lambda config: _Provider(fail_first=True))
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="mint"):
        cc._oidc_bearer_header(RuntimeConfig())
    assert _traced(caplog)


def test_a_failed_prime_falls_back_to_the_default_session_age(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(cc, "get_provider", lambda config: _Provider(fail_first=True))
    with caplog.at_level(logging.DEBUG, logger=cc.logger.name):
        age = cc.service_session_max_age(None, RuntimeConfig())
    assert age is not None and age > 0
    assert _traced(caplog)

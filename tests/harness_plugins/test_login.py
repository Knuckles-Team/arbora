"""The runner credential (ARBORA-HARNESS-PLUGINS-R003.2, R003.3)."""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from arbora.harness_plugins import login
from arbora.harness_plugins.profiles import profile
from tests.harness_plugins.conftest import ENDPOINT
from tests.harness_plugins.test_install_machine import described_edges

ISSUER = "https://id.example.test/realms/r"


def _idp(tokens: list[dict]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth-protected-resource/mcp"):
            return httpx.Response(200, json={"authorization_servers": [ISSUER]})
        if request.url.path.endswith("/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": ISSUER + "/auth",
                    "token_endpoint": ISSUER + "/token",
                },
            )
        form = parse_qs(request.content.decode())
        assert form["client_id"] == ["arbora-native-mcp"]
        return httpx.Response(200, json=tokens.pop(0))

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R003.2")
def test_login_stores_refresh_token_and_renews(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARBORA_PLUGIN_FILE_STORE", "1")
    store = login.TokenStore(tmp_path / "credentials")
    client = _idp(
        [
            {"access_token": "a1", "refresh_token": "r1", "expires_in": 1},
            {"access_token": "a2", "refresh_token": "r2", "expires_in": 300},
        ]
    )
    opened: list[str] = []
    credential = login.authorize(
        profile("codex"),
        ENDPOINT,
        store=store,
        open_url=opened.append,
        client=client,
        wait_for_code=lambda callback, state, _t: "the-code",
    )
    query = parse_qs(urlsplit(opened[0]).query)
    assert query["redirect_uri"] == [profile("codex").callback]
    assert query["code_challenge_method"] == ["S256"] and query["client_id"] == [
        "arbora-native-mcp"
    ]
    path = tmp_path / "credentials" / "codex.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["refresh_token"] == "r1"
    assert credential.state is login.CredentialState.AUTHORIZED
    renewed = login.renew(store.load("codex"), store=store, client=client)  # type: ignore[arg-type]
    assert renewed.access_token == "a2" and renewed.refresh_token == "r2"
    assert renewed.expires_at > time.time() + 200
    assert login.renew(renewed, store=store, client=client) is renewed


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R003.3")
def test_credential_edges_match_the_ontology() -> None:
    assert described_edges("HarnessCredentialTransition") == {
        (s.value, e.value, t.value) for (s, e), t in login.CREDENTIAL_EDGES.items()
    }
    for state in (login.CredentialState.EXPIRED, login.CredentialState.REVOKED):
        exits = {e for (s, e) in login.CREDENTIAL_EDGES if s is state}
        assert exits == {login.CredentialEvent.BEGIN}
    with pytest.raises(login.IllegalCredentialTransition):
        login.transition(
            login.CredentialState.EXPIRED, login.CredentialEvent.REFRESH_OK
        )

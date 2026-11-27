"""Register, password change and reset served over the real ASGI app (ARBORA-IDENTITY-R005.2).

The engine is a structural double of its ``user``, ``credential`` and ``token`` families:
it holds the accounts, the password verifiers and the one-use reset tokens. Arbora keeps no
account store, so every assertion is about what it sent and what it answered.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.identity.account_ceremonies import (
    PASSWORD_CHANGE,
    REGISTER,
    RESET_CONFIRM,
    RESET_REQUEST,
)
from arbora.identity.browser import SESSION_COOKIE, csrf_token_for
from arbora.identity.browser_runtime import (
    BrowserAuthApplication,
    BrowserCallerResolver,
)
from tests.identity.test_browser_runtime import Authority, auth_scope, request
from tests.identity.test_issuer import SETTINGS  # noqa: F401
from tests.identity.test_issuer import (
    synthetic_signing_key as synthetic_signing_key,  # noqa: F401
)
from tests.identity.test_modes_and_browser import ORIGIN, TOKEN


class _Engine:
    def __init__(self) -> None:
        self.users = {"ann": "old-secret-1"}
        self.tokens: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.acted_as: list[Any] = []
        self.audit: list[dict[str, str]] = []
        self.mailed: list[tuple[str, str]] = []

    async def send(self, client: Any, req: Any, graph: str) -> Any:
        family, op = req["op"]["family"], req["op"]["op"]
        body = req["op"]["request"]
        self.calls.append((family, op, body))
        self.acted_as.append(client)
        handler = getattr(self, f"_{family}_{op}")
        return SimpleNamespace(payload={"kind": "done", "value": handler(body)})

    def _user_create(self, body: dict[str, Any]) -> Any:
        from medulla import EngineResponseError

        if len(body["password"]) < 8:
            raise EngineResponseError("IDENTITY_WEAK_PASSWORD")
        if body["username"] in self.users:
            raise EngineResponseError("IDENTITY_COLLISION")
        self.users[body["username"]] = body["password"]
        return {"ok": True}

    def _credential_change_password(self, body: dict[str, Any]) -> Any:
        from medulla import EngineResponseError

        if body["current"] != self.users["ann"]:
            raise EngineResponseError("IDENTITY_BAD_CREDENTIAL")
        if len(body["new"]) < 8:
            raise EngineResponseError("IDENTITY_WEAK_PASSWORD")
        self.users["ann"] = body["new"]
        return {"ok": True}

    def _token_issue_password_reset(self, body: dict[str, Any]) -> Any:
        from medulla import EngineResponseError

        if body["username"] not in self.users:
            raise EngineResponseError("IDENTITY_NOT_FOUND")
        self.tokens[body["token"]] = body["username"]
        return {"email": f"{body['username']}@example.test"}

    def _token_redeem_one_time(self, body: dict[str, Any]) -> Any:
        from medulla import EngineResponseError

        user = self.tokens.pop(body["token"], None)
        if user is None:
            raise EngineResponseError("IDENTITY_TOKEN_SPENT")
        self.users[user] = body["new_password"]
        return {"ok": True}


class _Issuer:
    async def issue(self, credential: str) -> str:
        return "fixture-jwt"


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from medulla.generated import security

    authority = Authority()
    engine = _Engine()
    monkeypatch.setattr(security, "send_identity", engine.send)
    monkeypatch.setattr(security, "decode_identity", lambda response: None)

    async def mail(address: str, token: str) -> bool:
        engine.mailed.append((address, token))
        return True

    app = BrowserAuthApplication(
        "base-client",
        authority,
        graph="fixture-graph",
        origin=ORIGIN,
        issuer=_Issuer(),
        caller=BrowserCallerResolver(
            authority, authority.verify, graph="fixture-graph"
        ),
        audit=engine.audit.append,
        acting=lambda session: f"acting:{session.actor.actor_id}",
        mail=mail,
    )
    return SimpleNamespace(app=app, engine=engine)


def _post(
    world: SimpleNamespace,
    path: str,
    body: dict[str, Any],
    *,
    session: bool = False,
    csrf: bool = True,
) -> tuple[int, Any]:
    scope = auth_scope()
    scope["path"] = path
    if session:
        scope["headers"].append((b"cookie", f"{SESSION_COOKIE}={TOKEN}".encode()))
    if session and csrf:
        scope["headers"].append((b"x-csrf-token", csrf_token_for(TOKEN).encode()))
    start, response = asyncio.run(request(world.app, scope, json.dumps(body).encode()))
    return start["status"], (json.loads(response) if response else None)


@pytest.mark.spec("ARBORA-IDENTITY-R005.2")
def test_register_login_change_reset_logout(world: SimpleNamespace) -> None:
    engine = world.engine
    # Register: a new and an existing account answer the same 202.
    new = _post(world, REGISTER, {"username": "bob", "password": "long-secret-2"})
    taken = _post(world, REGISTER, {"username": "ann", "password": "long-secret-2"})
    assert new == taken == (202, {"outcome": "accepted"})
    assert (
        engine.users["bob"] == "long-secret-2" and engine.users["ann"] == "old-secret-1"
    )
    weak = _post(world, REGISTER, {"username": "cy", "password": "short"})
    assert weak == (422, {"error": "PASSWORD_POLICY_FAILED"})

    # Change: needs the session and its proof, acts as the session, and keeps the rules.
    body = {"current": "old-secret-1", "new": "fresh-secret-3"}
    assert _post(world, PASSWORD_CHANGE, body)[0] == 401
    assert _post(world, PASSWORD_CHANGE, body, session=True, csrf=False)[0] == 401
    assert _post(world, PASSWORD_CHANGE, body, session=True)[0] == 204
    assert engine.users["ann"] == "fresh-secret-3"
    assert engine.acted_as[-1] == "acting:usr:fixture"
    wrong = _post(world, PASSWORD_CHANGE, body, session=True)
    assert wrong[0] == 401
    weak_change = {"current": "old-secret-1", "new": "x"}
    engine.users["ann"] = "old-secret-1"
    assert _post(world, PASSWORD_CHANGE, weak_change, session=True) == (
        422,
        {"error": "PASSWORD_POLICY_FAILED"},
    )

    # Reset request: the same 202 for a known and an unknown account; only a known one mails.
    known = _post(world, RESET_REQUEST, {"username": "ann"})
    unknown = _post(world, RESET_REQUEST, {"username": "nobody"})
    assert known == unknown == (202, {"outcome": "accepted"})
    assert [address for address, _t in engine.mailed] == ["ann@example.test"]
    token = engine.mailed[0][1]

    # Reset confirm: one body for an unknown, a spent and a wrong token.
    confirm = {"token": token, "new_password": "reset-secret-4"}
    assert _post(world, RESET_CONFIRM, confirm) == (204, None)
    assert engine.users["ann"] == "reset-secret-4"
    spent = _post(world, RESET_CONFIRM, confirm)
    other = _post(
        world, RESET_CONFIRM, {"token": "x", "new_password": "reset-secret-5"}
    )
    assert spent == other == (400, {"error": "RESET_TOKEN_INVALID"})

    # The audit names a digest of the user and an outcome, never a password or a token.
    text = json.dumps(engine.audit)
    for secret in ("old-secret-1", "fresh-secret-3", "reset-secret-4", token):
        assert secret not in text
    assert all(set(event) == {"ceremony", "user", "outcome"} for event in engine.audit)

    # An unknown field is refused uniformly before any engine call.
    before = len(engine.calls)
    assert (
        _post(
            world, REGISTER, {"username": "z", "password": "long-secret-2", "x": "1"}
        )[0]
        == 400
    )
    assert len(engine.calls) == before

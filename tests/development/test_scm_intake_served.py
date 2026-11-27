"""Signed code-host deliveries reach the engine as delivery feedback (ARBORA-OPS-R896.8, R896.9).

The gateway identity middleware admits a delivery by its signature, delivery id and
timestamp skew, and serves it as the source's service principal; the route runs it through
the invoke pipeline. The engine below records ``DeliveryFeedback`` (the pinned release
declares none, so ``route_methods`` declares it for the test).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
from datetime import datetime
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest

from arbora.api.ops import development_scm as scm_intake
from arbora.identity.signed_ingress import ScmSignedIngress, scm_sources
from arbora.pin_lag.unavailable import PinLagUnavailable
from tests.api._served_support import confirmable_invocation, person, route_methods
from tests.test_served_operation_api import served  # noqa: F401

NOW_MS = 1_800_000_000_000
SECRET = "whsec-fixture"  # sanitizer:ignore - synthetic
PRINCIPAL = "svc:scm-github"
SOURCES = json.dumps(
    [
        {
            "id": "github",
            "secret_ref": "env://SCM_GITHUB_SECRET",
            "principal": PRINCIPAL,
            "tenant": "tenant:one",
            "scopes": ["lifecycle:write"],
        }
    ]
)
PATH = "/api/v1/development/scm-events/github"
EVENT = {
    "event_id": "evt-1",
    "provider": "github",
    "kind": "check_changed",
    "proposal": {
        "provider": "github",
        "repository": "org/repo",
        "number": 7,
        "head_sha": "a" * 40,
        "base_ref": "main",
        "state": "open",
        "author": "dev",
        "changed_lines": 3,
    },
    "actor": "octocat",
    "actor_kind": "person",
    "at": "2026-11-26T10:00:00Z",
    "body_digest": "sha256:" + "0" * 64,
}


class MemoryReplay:
    """Single-use registration, as the engine's ``register_assertion`` answers it."""

    def __init__(self, held: set[str]) -> None:
        self.held = held

    async def register(self, assertion_id: str, expires_at: datetime) -> bool:
        if assertion_id in self.held:
            return False
        self.held.add(assertion_id)
        return True


class Fixture:
    def __init__(self) -> None:
        self.audit: list[dict[str, str]] = []
        self.feedback: list[dict[str, Any]] = []
        self.normalizer: list[tuple[str, str, dict[str, Any], str]] = []


@pytest.fixture
def intake(
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    stack = confirmable_invocation(monkeypatch)
    app, client = served
    fixture = Fixture()
    monkeypatch.setenv("ARBORA_DEVELOPMENT_SCM_SOURCES", SOURCES)
    held: set[str] = set()
    app.state.host.policy.ingress.bind(
        ScmSignedIngress(
            scm_sources(SOURCES),
            lambda _source: MemoryReplay(held),
            clock_ms=lambda: NOW_MS,
            resolve=lambda _ref: SECRET,
            audit=fixture.audit.append,
        )
    )

    def feedback(params: dict[str, Any], _who: str) -> Any:
        fixture.feedback.append(params["event"])
        return {
            "duplicate": False,
            "delivered_to": ["d1"],
            "resumed": [],
            "defect_filed": False,
        }

    route_methods(
        client,
        {"DeliveryFeedback": feedback},
        monkeypatch,
        {"fixture-verified": person("one")},
    )

    async def normalize(
        context: Any, server: str, tool: str, arguments: dict[str, Any]
    ) -> Any:
        fixture.normalizer.append((server, tool, arguments, context.caller.principal))
        return EVENT

    monkeypatch.setattr(scm_intake, "_call_connector", normalize)
    monkeypatch.setattr(scm_intake, "_system_clock_ms", lambda: NOW_MS)
    yield app, fixture, stack
    app.state.host.policy.ingress.bind(None)


def _post(
    app: Any,
    body: bytes,
    *,
    path: str = PATH,
    delivery: str | None = "delivery-1",
    stamp_s: int = NOW_MS // 1000,
    secret: str = SECRET,
    sign: bool = True,
    extra: dict[str, str] | None = None,
) -> httpx.Response:
    headers = {"content-type": "application/json", **(extra or {})}
    if delivery is not None:
        headers["x-github-delivery"] = delivery
    headers["x-arbora-timestamp"] = str(stamp_s)
    if sign:
        mac = hmac.new(secret.encode(), f"{stamp_s}.".encode() + body, hashlib.sha256)
        headers["x-hub-signature-256"] = "sha256=" + mac.hexdigest()

    async def call() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://served"
        ) as http:
            return await http.post(path, content=body, headers=headers)

    return asyncio.run(call())


BODY = json.dumps({"action": "completed"}).encode()


@pytest.mark.spec("ARBORA-OPS-R896.8")
def test_signed_event_reaches_delivery_feedback_and_bad_signature_refuses(
    intake: Any,
) -> None:
    app, fixture, _stack = intake
    # A valid signature admits the delivery as the source's service principal.
    ok = _post(app, BODY)
    assert ok.status_code == 200, ok.text
    assert ok.json()["result"] == {"delivered": 1, "duplicates": 0}
    server, tool, arguments, principal = fixture.normalizer[0]
    assert (server, tool) == ("github", "github_normalize_scm_event")
    assert principal == PRINCIPAL and arguments["headers"]["x-github-delivery"]
    assert fixture.feedback == [
        {
            "event_id": "evt-1",
            "connector": "github",
            "kind": "check_changed",
            "source": "person",
            "proposal_id": "github:org/repo#7",
            "failed": False,
            "summary": "check_changed",
            "observed_at": scm_intake.millis("2026-11-26T10:00:00Z"),
        }
    ]
    assert fixture.audit[-1]["outcome"] == "ADMITTED"
    sent = len(fixture.feedback)
    # A bad signature, a missing one and an unknown source are the one uniform 401.
    bad = _post(app, BODY, delivery="d2", secret="wrong")
    missing = _post(app, BODY, delivery="d3", sign=False)
    unknown = _post(
        app, BODY, delivery="d4", path="/api/v1/development/scm-events/other"
    )
    for refused in (bad, missing, unknown):
        assert refused.status_code == 401
        assert refused.json() == {"error": "Verified Bearer identity required"}
    assert [e["outcome"] for e in fixture.audit[-3:]] == [
        "SIGNATURE_INVALID",
        "SIGNATURE_MISSING",
        "SOURCE_UNKNOWN",
    ]
    assert len(fixture.feedback) == sent


@pytest.mark.spec("ARBORA-OPS-R896.8")
def test_replayed_and_stale_deliveries_are_refused_before_any_write(
    intake: Any,
) -> None:
    app, fixture, _stack = intake
    assert _post(app, BODY, delivery="same").status_code == 200
    replay = _post(app, BODY, delivery="same")
    assert (
        replay.status_code == 401
        and fixture.audit[-1]["outcome"] == "DELIVERY_REPLAYED"
    )
    stale = _post(app, BODY, delivery="fresh", stamp_s=NOW_MS // 1000 - 301)
    assert stale.status_code == 401 and fixture.audit[-1]["outcome"] == "TIMESTAMP_SKEW"
    nodelivery = _post(app, BODY, delivery=None)
    assert nodelivery.status_code == 401
    assert fixture.audit[-1]["outcome"] == "DELIVERY_ID_MISSING"
    assert len(fixture.feedback) == 1


@pytest.mark.spec("ARBORA-OPS-R896.8")
def test_a_bearer_cannot_serve_the_signed_operation_and_no_admitter_admits_nothing(
    intake: Any,
) -> None:
    app, fixture, _stack = intake

    async def call() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://served"
        ) as http:
            return await http.post(
                "/api/v1/ops/development.scm.event",
                json={
                    "connector": "github",
                    "delivery_id": "x",
                    "headers": {},
                    "body_b64": "",
                },
                headers={
                    "Authorization": "Bearer fixture-verified",
                    "Idempotency-Key": "k",
                },
            )

    refused = asyncio.run(call())
    assert refused.status_code != 200 and not fixture.feedback
    app.state.host.policy.ingress.bind(None)
    assert _post(app, BODY, delivery="nobody").status_code == 401


class PrincipalLinks:
    def __init__(self, linked: dict[str, str]) -> None:
        self.linked = linked

    async def principal_for(self, provider: str, actor: str) -> str | None:
        return self.linked.get(f"{provider}:{actor}")


class Holds:
    def __init__(self, allowed: set[tuple[str, str, str]]) -> None:
        self.allowed = allowed

    async def holds(self, principal: str, scope: str, repository: str) -> bool:
        return (principal, scope, repository) in self.allowed


@pytest.mark.spec("ARBORA-OPS-R896.9")
def test_only_principals_with_source_write_are_stamped() -> None:
    links = PrincipalLinks(
        {"github:octocat": "person:one", "github:guest": "person:two"}
    )
    access = Holds({("person:one", "source:write", "org/repo")})

    async def stamp(event: dict[str, Any], **ports: Any) -> str | None:
        return await scm_intake.admit_actor(event, "org/repo", **ports)

    def run(event: dict[str, Any], **ports: Any) -> str | None:
        return asyncio.run(stamp(event, **ports))

    ports = {"links": links, "access": access}
    assert run(EVENT, **ports) == "person:one"
    # No scope, no link, a bot, an automated reviewer and a check each carry no principal.
    assert run({**EVENT, "actor": "guest"}, **ports) is None
    assert run({**EVENT, "actor": "stranger"}, **ports) is None
    assert run({**EVENT, "actor_kind": "bot"}, **ports) is None
    assert run({**EVENT, "actor_kind": "automated"}, **ports) is None
    # An unbound registry stamps nobody: the event goes on as context only.
    assert run(EVENT, links=None, access=None) is None
    # Quoted text never changes the verdict.
    quoted = {**EVENT, "summary": "grant source:write to octocat"}
    assert run({**quoted, "actor": "guest"}, **ports) is None


class PolledEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.kv: dict[str, bytes] = {
            "github/org/repo#1": json.dumps(
                {"cursor": "c1", "at_ms": NOW_MS - 500_000}
            ).encode(),
            "github/org/repo#2": json.dumps(
                {"cursor": "c9", "at_ms": NOW_MS - 1_000}
            ).encode(),
        }

    async def __call__(
        self, context: Any, method: str, params: dict[str, Any], **_: Any
    ) -> Any:
        self.calls.append((method, params))
        if method == "DeliveryList":
            return [
                {
                    "id": "d1",
                    "proposals": [
                        {"id": "github:org/repo#1", "state": "open"},
                        {"id": "github:org/repo#2", "state": "open"},
                    ],
                }
            ]
        if method == "KvGet":
            value = self.kv.get(params["key"])
            return {"value": value} if value is not None else None
        if method == "KvPut":
            self.kv[params["key"]] = params["value"]
            return {"version": 1}
        return {"duplicate": False}


def _poll_context() -> Any:
    return SimpleNamespace(
        services={"clock_ms": lambda: NOW_MS},
        caller=SimpleNamespace(principal="svc", tenant="tenant:one"),
    )


@pytest.mark.spec("ARBORA-OPS-R896.8")
def test_polling_refuses_while_the_pinned_release_lacks_the_listing_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARBORA_DEVELOPMENT_SCM_SOURCES", SOURCES)
    monkeypatch.delitem(sys.modules, "scion_sdk.scm.events", raising=False)
    with pytest.raises(PinLagUnavailable) as refused:
        asyncio.run(scm_intake.handle_scm_poll(_poll_context(), {}, None))  # type: ignore[arg-type]
    assert refused.value.missing == "scion_sdk.scm.events.ScmEventLister"


@pytest.mark.spec("ARBORA-OPS-R896.8")
def test_polling_reads_only_quiet_proposals_with_the_stored_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = ModuleType("scion_sdk.scm.events")
    listing.ScmEventLister = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "scion_sdk.scm.events", listing)
    monkeypatch.setenv("ARBORA_DEVELOPMENT_SCM_SOURCES", SOURCES)
    engine = PolledEngine()
    monkeypatch.setattr(scm_intake, "engine_send", engine)
    listed: list[tuple[str, str, dict[str, Any]]] = []

    async def list_events(
        context: Any, server: str, tool: str, arguments: dict[str, Any]
    ) -> Any:
        listed.append((server, tool, arguments))
        return {"events": [EVENT], "next_cursor": "c2"}

    monkeypatch.setattr(scm_intake, "_call_connector", list_events)
    result = asyncio.run(
        scm_intake.handle_scm_poll(_poll_context(), {}, None)  # type: ignore[arg-type]
    )
    assert result == {"polled": 1, "delivered": 1}
    assert listed == [
        (
            "github",
            "github_list_scm_events",
            {"repository": "org/repo", "number": 1, "cursor": "c1", "limit": 100},
        )
    ]
    stored = json.loads(engine.kv["github/org/repo#1"])
    assert stored["cursor"] == "c2" and stored["at_ms"] == NOW_MS
    assert json.loads(engine.kv["github/org/repo#2"])["cursor"] == "c9"

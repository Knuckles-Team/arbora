"""Synthetic native port proofs, not installed tokenless EG qualification."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from arbora.a2a.anonymous import (
    AnonymousNativeReadPort,
    AnonymousReadPolicy,
    AnonymousTaskReadOwner,
    AnonymousTaskStatus,
    create_anonymous_a2a_application,
)
from arbora.a2a.authority import remember_task_output
from arbora.a2a.service import A2AService
from arbora.api.contracts.session import current_session
from arbora.api.invoke.steps import authenticate
from arbora.security.actor_context import IdentityRequiredError, current_actor

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R004.7.1")
TASK = "a2a-" + "a" * 64
HEADERS = {"Origin": "http://127.0.0.1:8080", "X-Arbora-Anonymous": "1"}


class NativeFixture(AnonymousNativeReadPort):
    def __init__(self):
        self.calls = []
        self.changed = {}
        self.revoke = None

    async def get(self, caller):
        self.calls.append(caller)
        assert authenticate(caller).code == "UNAUTHENTICATED"
        assert current_session() is None
        with pytest.raises(IdentityRequiredError):
            current_actor()
        if self.revoke:
            self.revoke()
        await asyncio.sleep(0)
        return AnonymousTaskStatus(
            **{
                "work_item_id": "work-item:" + "d" * 64,
                "run_id": caller.task_id,
                "tenant": caller.tenant,
                "owner_ref": caller.owner_ref,
                "status": "succeeded",
                "authority_mode": "none",
                "context_id": "a2a-context-" + "b" * 64,
                **self.changed,
            }
        )


def stack(operations=frozenset({"tasks/get"})):
    native = NativeFixture()
    owner = AnonymousTaskReadOwner(
        mode="none",
        policy=AnonymousReadPolicy(
            "http://127.0.0.1:8080", "local", "revision-1", operations
        ),
        work_items=native,
    )
    service = A2AService(authority=owner, router=None)
    return native, owner, create_anonymous_a2a_application(owner=owner, service=service)


async def request(app, *, method="tasks/get", params=None, headers=None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8080"
    ) as client:
        return await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": "read-1",
                "method": method,
                "params": params or {"id": TASK},
            },
            headers=HEADERS if headers is None else headers,
        )


@pytest.mark.asyncio
async def test_served_read_uses_same_owner_and_returns_only_status():
    native, owner, app = stack()
    remember_task_output(TASK, "verified secret must not escape")
    response = await request(app)
    assert response.status_code == 200, response.text
    assert response.json()["result"]["status"]["state"] == "completed"
    assert "secret" not in response.text
    assert len(native.calls) == 1
    assert native.calls[0].authenticated is False
    assert response.headers["cache-control"] == "no-store"
    assert owner._binding.get() is None
    assert owner._active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {**HEADERS, "Origin": "https://evil.example"},
        {**HEADERS, "Authorization": "bad"},
        {**HEADERS, "Cookie": "bad"},
        {**HEADERS, "Forwarded": "for=127.0.0.1"},
        {**HEADERS, "Host": "evil.example"},
    ],
)
async def test_ambiguous_or_cross_origin_requests_never_read(headers):
    native, _, app = stack()
    assert (await request(app, headers=headers)).status_code == 403
    assert native.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    [
        "message/send",
        "tasks/cancel",
        "tasks/list",
        "arbora.op/invoke",
        "arbora.plan/confirm",
    ],
)
async def test_unqualified_methods_never_read(method):
    native, _, app = stack()
    assert (await request(app, method=method)).status_code == 403
    assert native.calls == []


@pytest.mark.asyncio
async def test_default_policy_and_committed_reply_refuse():
    native, _, app = stack(frozenset())
    assert (await request(app)).status_code == 403
    assert native.calls == []
    native, _, app = stack()
    assert (
        await request(app, params={"id": TASK, "includeCommittedEvent": True})
    ).status_code == 403
    assert native.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"run_id": "a2a-" + "c" * 64},
        {"tenant": "another"},
        {"owner_ref": "verified-owner"},
        {"authority_mode": "verified"},
    ],
)
async def test_native_namespace_and_provenance_mismatch_never_project(change):
    native, _, app = stack()
    native.changed = change
    assert (await request(app)).status_code == 404


@pytest.mark.asyncio
async def test_revocation_during_await_discards_result_and_cleans_capacity():
    native, owner, app = stack()
    native.revoke = owner.revoke
    assert (await request(app)).status_code == 403
    assert owner._active == 0
    assert owner._binding.get() is None


def test_generic_signed_client_and_wrong_mode_are_not_native_contracts():
    policy = AnonymousReadPolicy("http://127.0.0.1:8080", "local", "revision-1")
    with pytest.raises(ValueError):
        AnonymousTaskReadOwner(
            mode="none",
            policy=policy,
            work_items=SimpleNamespace(get=lambda **kw: None),
        )
    with pytest.raises(ValueError):
        AnonymousTaskReadOwner(mode="eg", policy=policy, work_items=NativeFixture())


@pytest.mark.asyncio
async def test_no_binding_no_read():
    native, owner, _ = stack()
    with pytest.raises(PermissionError):
        await owner.get(TASK)
    assert native.calls == []


@pytest.mark.asyncio
async def test_real_eg_caller_view_is_not_ownership_evidence():
    class ExistingView(NativeFixture):
        async def get(self, caller):
            return {
                "work_item_id": "work-item:" + "d" * 64,
                "run_id": caller.task_id,
                "kind": "orchestrator_task",
                "status": "succeeded",
                "input_ref": "",
                "metadata": {},
                "version": 1,
                "updated_at_ms": 0,
            }

    _, owner, app = stack()
    owner._work_items = ExistingView()
    assert (await request(app)).status_code == 404


def raw_request():
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/a2a",
            "root_path": "",
            "query_string": b"",
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8080),
            "headers": [
                (b"host", b"127.0.0.1:8080"),
                (b"origin", b"http://127.0.0.1:8080"),
                (b"content-type", b"application/json"),
                (b"x-arbora-anonymous", b"1"),
            ],
        }
    )


@pytest.mark.asyncio
async def test_exact_request_task_target_and_one_use_fences():
    native, owner, _ = stack()
    req = raw_request()
    with owner.request_scope(req), owner.admission(req, TASK):
        with pytest.raises(PermissionError):
            await asyncio.create_task(owner.get(TASK))
        with pytest.raises(PermissionError):
            await owner.get("a2a-" + "c" * 64)
        assert await owner.get(TASK) is not None
        with pytest.raises(PermissionError):
            await owner.get(TASK)
    assert len(native.calls) == 1
    assert owner._active == 0


@pytest.mark.asyncio
async def test_changed_request_snapshot_refuses_before_native_read():
    native, owner, _ = stack()
    req = raw_request()
    with owner.request_scope(req), owner.admission(req, TASK):
        req.scope["path"] = "/changed"
        with pytest.raises(PermissionError):
            await owner.get(TASK)
    assert native.calls == []


@pytest.mark.asyncio
async def test_cancelled_native_call_releases_only_after_unwind():
    entered = asyncio.Event()
    unwound = asyncio.Event()

    class Blocking(NativeFixture):
        async def get(self, caller):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                unwound.set()

    _, owner, app = stack()
    owner._work_items = Blocking()
    task = asyncio.create_task(request(app))
    await entered.wait()
    assert owner._active == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert unwound.is_set()
    assert owner._active == 0


@pytest.mark.asyncio
async def test_oversized_and_duplicate_payloads_do_not_read():
    native, _, app = stack()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8080"
    ) as client:
        headers = {**HEADERS, "Content-Type": "application/json"}
        assert (
            await client.post("/a2a", content=b"x" * 16385, headers=headers)
        ).status_code == 413
        assert (
            await client.post("/a2a", content='{"id":1,"id":2}', headers=headers)
        ).status_code == 400
    assert native.calls == []


@pytest.mark.asyncio
async def test_equal_but_replaced_scope_is_not_same_request():
    _, owner, app = stack()

    class ReplacesScope(NativeFixture):
        async def get(self, caller):
            binding = owner._binding.get()
            binding.request.scope = dict(binding.request.scope)
            return await super().get(caller)

    owner._work_items = ReplacesScope()
    assert (await request(app)).status_code == 403


@pytest.mark.asyncio
async def test_swallowed_external_cancellation_never_returns_success():
    entered = asyncio.Event()
    _, owner, app = stack()

    class SwallowsCancel(NativeFixture):
        async def get(self, caller):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return await super().get(caller)

    owner._work_items = SwallowsCancel()
    task = asyncio.create_task(request(app))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert owner._active == 0


@pytest.mark.asyncio
async def test_swallowed_timeout_never_returns_success(monkeypatch):
    _, owner, app = stack()
    original = owner._deadline

    def short_deadline():
        return min(original(), asyncio.get_running_loop().time() + 0.01)

    monkeypatch.setattr(owner, "_deadline", short_deadline)

    class SwallowsTimeout(NativeFixture):
        async def get(self, caller):
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                return await super().get(caller)

    owner._work_items = SwallowsTimeout()
    assert (await request(app)).status_code == 503
    assert owner._active == 0


def test_native_evidence_rejects_mutable_fields():
    with pytest.raises(ValueError):
        AnonymousTaskStatus(
            "work-item:" + "d" * 64,
            TASK,
            [],
            "owner",
            "succeeded",
            "none",
            "a2a-context-" + "b" * 64,
        )

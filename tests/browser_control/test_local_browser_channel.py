"""Native-backed socket provenance grants no browser operation authority."""

import asyncio

import pytest

from arbora.access.action_policy import ActionPolicy
from arbora.identity.local_browser_channel import LocalBrowserChannelOwner
from tests.browser_control.test_local_browser_transport import _scope, _transport
from tests.security.test_anonymous_mode import local_mode as local_mode
from tests.test_served_engine_native_labels import native_engine as native_engine

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R004.6")


def _owner(engine, tmp_path):
    path = tmp_path / "explicit-policy.yml"
    path.write_text("rules: []\n")
    policy = ActionPolicy(engine, policy_path=path)
    return LocalBrowserChannelOwner(engine, policy, _transport())


def _socket():
    scope = _scope(
        type="websocket",
        scheme="ws",
        path="/ws/local-browser-admission",
        raw_path=b"/ws/local-browser-admission",
        query_string=b"",
    )
    scope.pop("method")
    return scope


def test_native_channel_keeps_private_socket_provenance(native_engine, tmp_path):
    engine, session = native_engine
    owner, scope = _owner(engine, tmp_path), _socket()

    async def run():
        with pytest.raises(PermissionError):
            await owner.validate(scope)
        async with owner.channel(scope, session, ()) as grant:
            assert grant is None
            assert session.actor.authenticated is False
            await owner.validate(scope)
            with pytest.raises(PermissionError):
                await owner.validate(dict(scope))
            with pytest.raises(PermissionError):
                await asyncio.create_task(owner.validate(scope))
        with pytest.raises(PermissionError):
            await owner.validate(scope)

    asyncio.run(run())


def test_native_json_cannot_supply_owner_binding(native_engine, tmp_path):
    engine, _session = native_engine
    owner, scope = _owner(engine, tmp_path), _socket()
    scope["state"] = {
        "local_browser_admitted": True,
        "grant": {"operation_authority": True},
    }
    with pytest.raises(PermissionError):
        asyncio.run(owner.validate(scope))


def test_default_policy_cannot_admit_socket(native_engine):
    engine, session = native_engine
    owner = LocalBrowserChannelOwner(engine, ActionPolicy(engine), _transport())

    async def run():
        with pytest.raises(RuntimeError):
            async with owner.channel(_socket(), session, ()):
                raise AssertionError("default policy must not admit")
        assert not owner._sockets

    asyncio.run(run())


def test_native_channel_disconnect_revokes_its_lease(native_engine, tmp_path):
    engine, session = native_engine
    owner, scope = _owner(engine, tmp_path), _socket()

    async def run():
        async with owner.channel(scope, session, ()):
            record = owner._sockets[id(scope)]
        lease = await owner._native(
            lambda client: client.control_leases.get(
                tenant=session.tenant, lease_id=record.lease_id
            ),
            write=False,
        )
        assert lease["status"] == "revoked"
        assert not owner._sockets

    asyncio.run(run())


def test_native_revocation_invalidates_private_socket(native_engine, tmp_path):
    engine, session = native_engine
    owner, scope = _owner(engine, tmp_path), _socket()

    async def run():
        async with owner.channel(scope, session, ()):
            record = owner._sockets[id(scope)]
            lease = await owner._native(
                lambda client: client.control_leases.get(
                    tenant=session.tenant, lease_id=record.lease_id
                ),
                write=False,
            )
            result = await owner._native(
                lambda client: client.control_leases.transition(
                    tenant=session.tenant,
                    lease_id=record.lease_id,
                    expected_revision=lease["revision"],
                    to="revoked",
                    idempotency_key="external-revoke",
                ),
                write=True,
            )
            assert result["outcome"] == "applied"
            with pytest.raises(PermissionError):
                await owner.validate(scope)

    asyncio.run(run())


def test_matching_native_lease_never_admits_another_owner(native_engine, tmp_path):
    engine, session = native_engine
    first, second = _owner(engine, tmp_path), _owner(engine, tmp_path)
    scope = _socket()

    async def run():
        async with first.channel(scope, session, ()):
            record = first._sockets[id(scope)]
            stored = await first._native(
                lambda client: client.control_leases.get(
                    tenant=session.tenant, lease_id=record.lease_id
                ),
                write=False,
            )
            assert stored["status"] == "active"
            forged = _socket()
            forged["state"] = {
                "lease": stored,
                "document_ref": record.grant["document_reference"],
            }
            with pytest.raises(PermissionError):
                await second.validate(forged)
            with pytest.raises(PermissionError):
                await second.validate(scope)

    asyncio.run(run())


def test_native_issuance_collision_is_not_adopted(native_engine, tmp_path, monkeypatch):
    engine, session = native_engine
    from epistemic_graph.client import EngineResponseError

    first, second = _owner(engine, tmp_path), _owner(engine, tmp_path)
    monkeypatch.setattr(
        "arbora.identity.local_browser_channel.secrets.token_hex", lambda _n: "c" * 48
    )

    async def run():
        async with first.channel(_socket(), session, ()):
            with pytest.raises(EngineResponseError, match="IDEMPOTENCY_CONFLICT"):
                async with second.channel(_socket(), session, ()):
                    raise AssertionError(
                        "existing lease must not confer owner provenance"
                    )
            assert not second._sockets

    asyncio.run(run())


def test_policy_change_after_native_read_refuses(native_engine, tmp_path, monkeypatch):
    engine, session = native_engine
    owner, scope = _owner(engine, tmp_path), _socket()

    async def run():
        async with owner.channel(scope, session, ()):
            original = owner._native

            async def change_after_read(operation, *, write):
                result = await original(operation, write=write)
                if not write:
                    owner._policy.policy_path.write_text(
                        "rules: [{kind: observe, tier: forbidden}]\n"
                    )
                return result

            monkeypatch.setattr(owner, "_native", change_after_read)
            with pytest.raises(PermissionError, match="policy changed"):
                await owner.validate(scope)
            monkeypatch.setattr(owner, "_native", original)

    asyncio.run(run())


def test_catalog_mutation_during_policy_read_refuses(
    native_engine, tmp_path, monkeypatch
):
    from tests.browser_control.test_browser_control_service import _tool

    engine, session = native_engine
    owner, scope = _owner(engine, tmp_path), _socket()
    tool = _tool()
    original = owner._policy.qualified_source_revision_async

    async def mutate():
        revision = await original()
        tool.input_schema["additionalProperties"] = True
        return revision

    monkeypatch.setattr(owner._policy, "qualified_source_revision_async", mutate)

    async def run():
        with pytest.raises((ValueError, PermissionError)):
            async with owner.channel(scope, session, (tool,)):
                raise AssertionError("changed schema must not be admitted")
        assert not owner._sockets

    asyncio.run(run())


def test_changed_native_revision_is_not_same_owner_snapshot(
    native_engine, tmp_path, monkeypatch
):
    engine, session = native_engine
    owner, scope = _owner(engine, tmp_path), _socket()

    async def run():
        async with owner.channel(scope, session, ()):
            original = owner._native

            async def change_revision(operation, *, write):
                result = await original(operation, write=write)
                if not write:
                    result = {**result, "revision": result["revision"] + 1}
                return result

            monkeypatch.setattr(owner, "_native", change_revision)
            with pytest.raises(PermissionError):
                await owner.validate(scope)
            monkeypatch.setattr(owner, "_native", original)

    asyncio.run(run())


def test_expiry_and_owner_mode_change_leave_no_private_binding(
    native_engine, tmp_path, monkeypatch
):
    from arbora.api.contracts.session import SessionExpiredError
    from arbora.security import anonymous

    engine, session = native_engine
    owner, scope = _owner(engine, tmp_path), _socket()
    original_clock = owner._clock

    async def run():
        async with owner.channel(scope, session, ()):
            monkeypatch.setattr(owner, "_clock", lambda: original_clock() + 61)
            with pytest.raises(PermissionError, match="expired"):
                await owner.validate(scope)
        monkeypatch.setattr(owner, "_clock", original_clock)
        with pytest.raises(SessionExpiredError):
            async with owner.channel(scope, session, ()):
                monkeypatch.setattr(anonymous, "_ACTIVE_MODE", "required")
                await owner.validate(scope)
        assert not owner._sockets
        monkeypatch.setattr(anonymous, "_ACTIVE_MODE", "none")

    asyncio.run(run())


def test_cancellation_invalidates_private_binding(native_engine, tmp_path):
    engine, session = native_engine
    owner, scope = _owner(engine, tmp_path), _socket()

    async def run():
        with pytest.raises(asyncio.CancelledError):
            async with owner.channel(scope, session, ()):
                raise asyncio.CancelledError
        assert not owner._sockets
        with pytest.raises(PermissionError):
            await owner.validate(scope)

    asyncio.run(run())


def test_one_scope_is_reserved_before_policy_await(
    native_engine, tmp_path, monkeypatch
):
    engine, session = native_engine
    owner, scope = _owner(engine, tmp_path), _socket()
    entered, release = asyncio.Event(), asyncio.Event()
    original = owner._policy.qualified_source_revision_async

    async def paused_source():
        entered.set()
        await asyncio.wait_for(release.wait(), 5)
        return await original()

    monkeypatch.setattr(owner._policy, "qualified_source_revision_async", paused_source)

    async def first():
        async with owner.channel(scope, session, ()):
            pass

    async def run():
        task = asyncio.create_task(first())
        await asyncio.wait_for(entered.wait(), 5)
        try:
            with pytest.raises(PermissionError):
                async with owner.channel(scope, session, ()):
                    raise AssertionError("duplicate scope must not be admitted")
        finally:
            release.set()
            await task
        assert not owner._sockets

    asyncio.run(run())


@pytest.mark.parametrize("blanket", [False, True])
def test_cancel_during_native_issue_retires_exact_completed_lease(
    native_engine, tmp_path, monkeypatch, blanket
):
    engine, session = native_engine
    owner, scope = _owner(engine, tmp_path), _socket()

    async def run():
        committed, release = asyncio.Event(), asyncio.Event()
        original = owner._native
        first_write = True
        issuance_task = None

        async def pause_after_issue(operation, *, write):
            nonlocal first_write, issuance_task
            result = await original(operation, write=write)
            if write and first_write:
                first_write = False
                issuance_task = asyncio.current_task()
                committed.set()
                await release.wait()
            return result

        monkeypatch.setattr(owner, "_native", pause_after_issue)

        async def open_channel():
            async with owner.channel(scope, session, ()):
                raise AssertionError("cancelled channel must not be admitted")

        task = asyncio.create_task(open_channel())
        await committed.wait()
        record = owner._sockets[id(scope)]
        targets = [task, issuance_task] if blanket else [task]
        for target in targets:
            target.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not owner._sockets
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not owner._sockets and not owner._pending
        lease = await original(
            lambda client: client.control_leases.get(
                tenant=session.tenant, lease_id=record.lease_id
            ),
            write=False,
        )
        assert lease["status"] == "revoked"

    asyncio.run(run())


def test_lost_native_issue_response_is_reconciled_not_reissued(
    native_engine, tmp_path, monkeypatch
):
    engine, session = native_engine
    owner, scope = _owner(engine, tmp_path), _socket()

    async def run():
        original = owner._native
        first_write = True
        record = None

        async def lose_issue_response(operation, *, write):
            nonlocal first_write, record
            result = await original(operation, write=write)
            if write and first_write:
                first_write = False
                record = owner._sockets[id(scope)]
                raise ConnectionError("native response lost after commit")
            return result

        monkeypatch.setattr(owner, "_native", lose_issue_response)
        with pytest.raises(ConnectionError):
            async with owner.channel(scope, session, ()):
                raise AssertionError("uncertain issuance must not admit")
        assert not owner._sockets and not owner._pending
        lease = await original(
            lambda client: client.control_leases.get(
                tenant=session.tenant, lease_id=record.lease_id
            ),
            write=False,
        )
        assert lease["status"] == "revoked"

    asyncio.run(run())


def test_native_revoke_during_final_policy_read_refuses(
    native_engine, tmp_path, monkeypatch
):
    engine, session = native_engine
    owner, scope = _owner(engine, tmp_path), _socket()

    async def run():
        async with owner.channel(scope, session, ()):
            record = owner._sockets[id(scope)]
            original = owner._policy.qualified_source_revision_async
            reads = 0

            async def revoke_during_second_policy_read():
                nonlocal reads
                revision = await original()
                reads += 1
                if reads == 2:

                    async def revoke(client):
                        lease = await client.control_leases.get(
                            tenant=session.tenant, lease_id=record.lease_id
                        )
                        return await client.control_leases.transition(
                            tenant=session.tenant,
                            lease_id=record.lease_id,
                            expected_revision=lease["revision"],
                            to="revoked",
                            idempotency_key="revoke-during-policy",
                        )

                    await owner._state._call_async(revoke, required_scope="kg:write")
                return revision

            monkeypatch.setattr(
                owner._policy,
                "qualified_source_revision_async",
                revoke_during_second_policy_read,
            )
            with pytest.raises(PermissionError):
                await owner.validate(scope)

    asyncio.run(run())


def test_idle_channel_deadline_closes_private_and_native_lifetime(
    native_engine, tmp_path, monkeypatch
):
    engine, session = native_engine
    owner, scope = _owner(engine, tmp_path), _socket()
    monkeypatch.setattr(
        "arbora.identity.local_browser_channel._CHANNEL_LIFETIME_SECONDS", 1
    )

    async def run():
        record = None
        with pytest.raises(TimeoutError):
            async with owner.channel(scope, session, ()):
                record = owner._sockets[id(scope)]
                await asyncio.sleep(2)
        assert record is not None
        assert not owner._sockets and not owner._pending
        lease = await owner._native(
            lambda client: client.control_leases.get(
                tenant=session.tenant, lease_id=record.lease_id
            ),
            write=False,
        )
        assert lease["status"] in {"revoked", "expired"}

    asyncio.run(run())


def test_cancelled_validation_revokes_private_binding_before_file_completes(
    native_engine, tmp_path, monkeypatch
):
    import threading

    from arbora.access import action_policy_source

    engine, session = native_engine
    owner, scope = _owner(engine, tmp_path), _socket()
    block, started, release = threading.Event(), threading.Event(), threading.Event()
    original = action_policy_source._validated_document

    def document(path):
        if block.is_set():
            started.set()
            assert release.wait(10)
        return original(path)

    monkeypatch.setattr(action_policy_source, "_validated_document", document)

    async def channel():
        async with owner.channel(scope, session, ()):
            block.set()
            await owner.validate(scope)

    async def run():
        task = asyncio.create_task(channel())
        try:
            while not started.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            for _ in range(5):
                await asyncio.sleep(0)
            assert not owner._sockets
            assert id(scope) in owner._pending
            assert not task.done()
            task.cancel()
            await asyncio.sleep(0)
            assert id(scope) in owner._pending
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not owner._pending
        await engine.pool.close()

    asyncio.run(run())


def test_cancelled_file_work_keeps_all_owner_capacity_reserved(
    native_engine, tmp_path, monkeypatch
):
    import threading

    from arbora.access import action_policy_source

    engine, session = native_engine
    owner = _owner(engine, tmp_path)
    started, release = threading.Event(), threading.Event()
    original = action_policy_source._validated_document

    def document(path):
        started.set()
        assert release.wait(10)
        return original(path)

    monkeypatch.setattr(action_policy_source, "_validated_document", document)

    async def open_channel(scope):
        async with owner.channel(scope, session, ()):
            raise AssertionError("cancelled file work must not admit a channel")

    async def run():
        tasks = [asyncio.create_task(open_channel(_socket())) for _ in range(16)]
        try:
            while len(owner._pending) < 16 or not started.is_set():
                await asyncio.sleep(0)
            for task in tasks:
                task.cancel()
            for _ in range(5):
                await asyncio.sleep(0)
            assert len(owner._pending) == 16
            assert not owner._sockets
            with pytest.raises(PermissionError, match="capacity"):
                async with owner.channel(_socket(), session, ()):
                    raise AssertionError("retained work must retain capacity")
        finally:
            release.set()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
        assert not owner._pending

    asyncio.run(run())


def test_repeated_native_owner_path_keeps_one_serving_loop(
    native_engine, tmp_path, caplog
):
    import gc

    engine, session = native_engine
    owner = _owner(engine, tmp_path)

    async def run():
        try:
            for _ in range(8):
                scope = _socket()
                async with owner.channel(scope, session, ()):
                    await owner.validate(scope)
                gc.collect()
            assert not owner._pending
            assert not owner._sockets
        finally:
            await engine.pool.close()

    asyncio.run(run())
    gc.collect()
    assert "Task was destroyed but it is pending" not in caplog.text


def test_policy_owner_substitution_during_final_native_read_refuses(
    native_engine, tmp_path, monkeypatch
):
    engine, session = native_engine
    owner, scope = _owner(engine, tmp_path), _socket()

    async def run():
        async with owner.channel(scope, session, ()):
            original = owner._native
            reads = 0

            async def substitute(operation, *, write):
                nonlocal reads
                result = await original(operation, write=write)
                if not write:
                    reads += 1
                    if reads == 2:
                        owner._policy.engine = object()
                return result

            monkeypatch.setattr(owner, "_native", substitute)
            with pytest.raises(PermissionError, match="policy owner"):
                await owner.validate(scope)
            assert not owner._sockets

    asyncio.run(run())


def test_second_cancellation_reports_unresolved_retirement(
    native_engine, tmp_path, monkeypatch
):
    engine, session = native_engine
    owner, scope = _owner(engine, tmp_path), _socket()

    async def run():
        retiring = asyncio.Event()

        async def interrupted_retire(record):
            retiring.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(owner, "_retire", interrupted_retire)

        async def channel():
            async with owner.channel(scope, session, ()):
                raise asyncio.CancelledError

        task = asyncio.create_task(channel())
        await retiring.wait()
        assert not owner._sockets
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert any("unresolved" in note for note in caught.value.__notes__)
        assert not owner._sockets and not owner._pending
        await engine.pool.close()

    asyncio.run(run())

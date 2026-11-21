"""Identity administration operations over a recording engine port (ARBORA-IDENTITY-R018)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import identity
from arbora.api.registry import Effect, PrincipalRule, Registry, Verb
from arbora.identity.engine_admin import IdentityRefused

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R018.1")


class RecordingPort:
    """Answers each engine op from a table and records every request."""

    def __init__(self, replies: dict[tuple[str, str], Any]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, str, Any]] = []

    async def identity(self, client, claims, family, op, request=None, *, expect):
        self.calls.append((family, op, request))
        reply = self.replies[(family, op)]
        if isinstance(reply, str):
            raise IdentityRefused(reply)
        return reply

    async def audit_verify(self, client, claims):
        return self.replies[("audit", "verify")]


def _context(port: RecordingPort | None) -> SimpleNamespace:
    services = {} if port is None else {"identity_admin": port}
    return SimpleNamespace(
        caller=SimpleNamespace(principal="usr:me", engine_claims={}),
        client=object(),
        services=services,
    )


def _op(op_id: str):
    return Registry(identity.operations())[op_id]


def test_module_declares_every_operation_with_scopes_and_effects() -> None:
    registry = Registry(identity.operations())
    assert len(registry) == len(identity._TABLE)
    for op in registry:
        assert op.id.startswith("identity.")
        assert op.examples
        if op.id.startswith("identity.self."):
            assert op.scopes == frozenset({"identity:self"})
            assert op.effect is Effect.READ
            continue
        assert op.scopes == frozenset({"identity:admin"})
        assert op.principals is PrincipalRule.HUMAN_UNDELEGATED
        if op.effect is Effect.ADMIN:
            assert op.verb is Verb.MANAGE
            assert op.audit.value != "none"
        else:
            assert op.verb is Verb.ASK


async def test_handler_refuses_when_the_port_is_not_bound() -> None:
    with pytest.raises(RuntimeError, match="identity admin port is not bound"):
        await identity.handle_users_get(
            _context(None), {"user_id": "u1"}, _op("identity.users.get")
        )


async def test_self_get_reads_only_the_callers_own_record() -> None:
    port = RecordingPort({("user", "get"): {"principal_id": "usr:me"}})
    result = await identity.handle_self_get(
        _context(port), {}, _op("identity.self.get")
    )
    assert port.calls == [("user", "get", {"id": "usr:me"})]
    assert result == {"value": {"user": {"principal_id": "usr:me"}}}


async def test_users_list_filters_by_text_and_pages_by_last_principal() -> None:
    rows = [
        {"principal_id": "a", "username": "alice", "email": None},
        {"principal_id": "b", "username": "bob", "email": "b@example.test"},
    ]
    port = RecordingPort({("user", "list"): rows})
    result = await identity.handle_users_list(
        _context(port),
        {"query": "BOB", "limit": 2, "cursor": None},
        _op("identity.users.list"),
    )
    assert result["value"]["users"] == [rows[1]]
    assert result["value"]["next_cursor"] == "b"


async def test_user_create_names_a_taken_username() -> None:
    port = RecordingPort({("user", "create"): "IDENTITY_COLLISION"})
    with pytest.raises(OperationRefused) as refused:
        await identity.handle_users_create(
            _context(port),
            {"username": "alice", "roles": [], "groups": []},
            _op("identity.users.create"),
        )
    assert refused.value.code == "USER_EXISTS"


async def test_disable_names_the_last_administrator_and_unknown_user() -> None:
    op = _op("identity.users.disable")
    for engine, expected in (
        ("IDENTITY_PRECONDITION_FAILED", "LAST_ADMIN_PROTECTED"),
        ("IDENTITY_NOT_FOUND", "USER_NOT_FOUND"),
    ):
        port = RecordingPort({("user", "set_status"): engine})
        with pytest.raises(OperationRefused) as refused:
            await identity.handle_users_disable(_context(port), {"user_id": "u"}, op)
        assert refused.value.code == expected


async def test_deprovision_and_enable_use_the_engine_status_edges() -> None:
    port = RecordingPort({("user", "set_status"): {"changed": True}})
    await identity.handle_users_deprovision(
        _context(port), {"user_id": "u"}, _op("identity.users.deprovision")
    )
    await identity.handle_users_enable(
        _context(port), {"user_id": "u"}, _op("identity.users.enable")
    )
    assert [call[2]["status"] for call in port.calls] == ["deprovisioned", "active"]


async def test_force_logout_counts_the_live_sessions_it_ends() -> None:
    port = RecordingPort(
        {
            ("session", "list"): [
                {"revoked": False},
                {"revoked": True},
                {"revoked": False},
            ],
            ("session", "revoke_all"): {"changed": True},
        }
    )
    result = await identity.handle_users_force_logout(
        _context(port), {"user_id": "u"}, _op("identity.users.force_logout")
    )
    assert result == {"value": {"revoked": 2}}
    assert port.calls[-1] == ("session", "revoke_all", {"id": "u"})


async def test_role_assign_sends_one_binding_change_and_names_unknown_role() -> None:
    port = RecordingPort({("access", "change_user_role"): {"changed": True}})
    await identity.handle_roles_assign(
        _context(port), {"user_id": "u", "role_id": "r"}, _op("identity.roles.assign")
    )
    assert port.calls == [
        (
            "access",
            "change_user_role",
            {"principal_id": "u", "role_id": "r", "change": "add"},
        )
    ]
    missing = RecordingPort({("access", "change_user_role"): "IDENTITY_NOT_FOUND"})
    with pytest.raises(OperationRefused) as refused:
        await identity.handle_roles_remove(
            _context(missing),
            {"user_id": "u", "role_id": "r"},
            _op("identity.roles.remove"),
        )
    assert refused.value.code == "ROLE_UNKNOWN"


async def test_group_members_apply_each_change_and_count_real_changes() -> None:
    port = RecordingPort({("access", "change_membership"): {"changed": True}})
    result = await identity.handle_groups_set_members(
        _context(port),
        {"group_id": "g", "add": ["a", "b"], "remove": ["c"]},
        _op("identity.groups.set_members"),
    )
    assert result == {"value": {"group_id": "g", "changed": 3}}
    assert [call[2]["change"] for call in port.calls] == ["add", "add", "remove"]


async def test_mapping_dry_run_is_the_engine_union_and_writes_nothing() -> None:
    provider = {
        "idp_id": "corp",
        "rules": [
            {"rule_id": "r1", "claim_path": "groups", "match_kind": "equals",
             "value": "eng", "target": "role:builder"},
            {"rule_id": "r2", "claim_path": "groups", "match_kind": "prefix",
             "value": "e", "target": "group:staff"},
            {"rule_id": "r3", "claim_path": "groups", "match_kind": "equals",
             "value": "ops", "target": "role:operator"},
        ],
    }  # fmt: skip
    port = RecordingPort({("idp", "list"): [provider]})
    result = await identity.handle_providers_mapping_dry_run(
        _context(port),
        {"provider_id": "corp", "claims": {"groups": ["eng"]}},
        _op("identity.providers.mapping_dry_run"),
    )
    assert result["value"] == {
        "roles": ["builder"],
        "groups": ["staff"],
        "matched_rules": ["r1", "r2"],
    }
    assert [call[1] for call in port.calls] == ["list"]


async def test_audit_export_filters_the_range_and_returns_the_chain_digest() -> None:
    rows = [
        {"seq": 1, "at_ms": 10, "chain": "c1"},
        {"seq": 2, "at_ms": 50, "chain": "c2"},
    ]
    port = RecordingPort({("config", "audit"): rows})
    result = await identity.handle_audit_export(
        _context(port),
        {"from_ms": 20, "to_ms": 60, "cursor": None, "limit": 2},
        _op("identity.audit.export"),
    )
    assert result["value"]["events"] == [rows[1]]
    assert result["value"]["chain_digest"] == "c2"
    assert result["value"]["next_cursor"] == "2"


async def test_audit_verify_refuses_a_broken_chain() -> None:
    port = RecordingPort({("audit", "verify"): {"ok": False, "first_broken_seq": 7}})
    with pytest.raises(OperationRefused) as refused:
        await identity.handle_audit_verify(
            _context(port), {}, _op("identity.audit.verify")
        )
    assert refused.value.code == "AUDIT_CHAIN_BROKEN"


async def test_mode_transition_names_an_out_of_order_step() -> None:
    port = RecordingPort({("config", "transition"): "IDENTITY_EPOCH_CONFLICT"})
    with pytest.raises(OperationRefused) as refused:
        await identity.handle_mode_transition_step(
            _context(port),
            {"to": "external", "expected_epoch": 1, "issuer_kid": "k"},
            _op("identity.mode.transition_step"),
        )
    assert refused.value.code == "STEP_OUT_OF_ORDER"

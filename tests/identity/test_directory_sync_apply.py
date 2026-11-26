"""Scheduled directory sync apply (ARBORA-IDENTITY-R010.2.3.2)."""

from __future__ import annotations

import asyncio

import pytest

from arbora.identity.directory_sync import SyncConfig, SyncRefused, run_sync
from arbora.identity.engine import IdentityUnavailable
from arbora.identity.ldap import DirectoryUser, LdapBindConfig

BIND = LdapBindConfig(host="ldap.test", port=636, bind_dn="cn=svc")
MAPPING = {"cn=eng": ["engineer"], "cn=ops": ["operator"]}


class Directory:
    def __init__(self, users, fail=False):
        self.users, self.fail = users, fail

    def bind(self, config):
        if self.fail:
            raise OSError("down")

    def snapshot(self):
        return self.users


class Port:
    def __init__(self, existing):
        self.existing, self.calls = existing, []

    async def identity(self, client, claims, family, op, request=None, *, expect):
        self.calls.append((family, op, request))
        return self.existing if (family, op) == ("user", "list") else {}


def _users(*names, disabled=()):
    return [DirectoryUser(n, ("cn=eng",), disabled=n in disabled) for n in names]


def _run(port, directory, **changes):
    config = SyncConfig(bind=BIND, mapping=MAPPING, **changes)
    events: list = []
    report = asyncio.run(
        run_sync(port, None, {}, directory, config, emit=events.append)
    )
    return report, events


@pytest.mark.spec("ARBORA-IDENTITY-R010.2.3.2")
def test_apply_orders_create_update_then_deprovision() -> None:
    existing = [
        {"username": "bob", "principal_id": "p-bob", "roles": ["operator"]},
        {"username": "eve", "principal_id": "p-eve", "roles": ["engineer"]},
    ]
    port = Port(existing)
    directory = Directory(_users("ann", "bob", "eve", "zed", "yan", disabled=("eve",)))
    report, events = _run(port, directory, max_deprovision_fraction=0.5)
    assert (report.created, report.updated, report.deprovisioned) == (3, 1, 1)
    ops = [(f, o) for f, o, _ in port.calls]
    assert ops.index(("user", "create")) < ops.index(("access", "change_user_role"))
    assert ops.index(("access", "change_user_role")) < ops.index(("user", "set_status"))
    assert ops[-1] == ("session", "revoke_all")
    assert events[-1]["outcome"] == "ok"


@pytest.mark.spec("ARBORA-IDENTITY-R010.2.3.2")
def test_bind_failure_and_deprovision_fraction_write_nothing() -> None:
    port = Port([{"username": "eve", "principal_id": "p", "roles": []}])
    with pytest.raises(SyncRefused) as bind:
        _run(port, Directory(_users("ann"), fail=True))
    assert bind.value.code == "DIRECTORY_BIND_FAILED"
    many = Directory(_users("a", "b", "c", "d", disabled=("a", "b")))
    with pytest.raises(SyncRefused) as fraction:
        _run(port, many)
    assert fraction.value.code == "DEPROVISION_FRACTION_EXCEEDED"
    assert port.calls == []
    with pytest.raises(ValueError):
        SyncConfig(bind=BIND, mapping=MAPPING, interval_seconds=5)
    assert IdentityUnavailable  # an empty snapshot is refused by the plan itself

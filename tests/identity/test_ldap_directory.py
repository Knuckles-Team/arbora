"""The LDAPS directory transport behind the sync job (ARBORA-IDENTITY-R010.2.2)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from arbora.identity.engine import IdentityUnavailable
from arbora.identity.ldap import LdapBindConfig
from arbora.identity.ldap_directory import LdapDirectory

BIND = LdapBindConfig(host="ldap.test", port=636, bind_dn="cn=svc")


class _Connection:
    def __init__(self) -> None:
        self.searched: list[tuple] = []
        self.entries = [
            SimpleNamespace(
                entry_attributes_as_dict={
                    "sAMAccountName": ["ann"],
                    "memberOf": ["cn=eng,dc=x"],
                    "userAccountControl": [512],
                }
            ),
            SimpleNamespace(
                entry_attributes_as_dict={
                    "uid": ["eve"],
                    "memberOf": [],
                    "userAccountControl": [514],
                }
            ),
            SimpleNamespace(entry_attributes_as_dict={"memberOf": []}),
        ]

    def search(self, base: str, query: str, attributes: list[str]) -> None:
        self.searched.append((base, query, tuple(attributes)))


@pytest.mark.spec("ARBORA-IDENTITY-R010.2.2")
def test_snapshot_reads_groups_and_the_disabled_flag() -> None:
    connection = _Connection()
    seen: list[tuple] = []

    def factory(config: LdapBindConfig, password: str) -> _Connection:
        seen.append((config.host, password))
        return connection

    directory = LdapDirectory(
        base_dn="dc=x", password=lambda: "pw-value", connection=factory
    )
    with pytest.raises(IdentityUnavailable):
        directory.snapshot()  # an unbound directory reads nothing
    directory.bind(BIND)
    users = directory.snapshot()
    assert seen == [("ldap.test", "pw-value")]
    assert [(u.username, u.group_dns, u.disabled) for u in users] == [
        ("ann", ("cn=eng,dc=x",), False),
        ("eve", (), True),
    ]
    assert connection.searched[0][0] == "dc=x"


@pytest.mark.spec("ARBORA-IDENTITY-R010.2.2")
def test_a_host_without_the_transport_refuses_the_bind() -> None:
    directory = LdapDirectory(base_dn="dc=x", password=lambda: "pw")
    try:
        import ldap3  # noqa: F401
    except ImportError:
        with pytest.raises(IdentityUnavailable):
            directory.bind(BIND)
    with pytest.raises(IdentityUnavailable):
        LdapDirectory(base_dn="", password=lambda: "pw")

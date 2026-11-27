"""Live directory sync against an LDAPS server (ARBORA-IDENTITY-R010.2.4, level L6).

Variables: ``ARBORA_QUAL_LDAP_HOST``, ``ARBORA_QUAL_LDAP_BIND_DN``,
``ARBORA_QUAL_LDAP_PASSWORD``, ``ARBORA_QUAL_LDAP_BASE_DN``, ``ARBORA_QUAL_LDAP_GROUP_ROLES``
(JSON of group DN to roles), ``ARBORA_QUAL_LDAP_EXPECT_USER`` with
``ARBORA_QUAL_LDAP_EXPECT_ROLES`` (comma list) and ``ARBORA_QUAL_LDAP_EXPECT_DISABLED``.
"""

from __future__ import annotations

import json

import pytest
from _identity_live import require

from arbora.identity.ldap import LdapBindConfig, plan_directory_sync
from arbora.identity.ldap_directory import LdapDirectory


@pytest.mark.spec("ARBORA-IDENTITY-R010.2.4")
def test_live_directory_sync_round_trip() -> None:
    env = require(
        "ARBORA_QUAL_LDAP_HOST",
        "ARBORA_QUAL_LDAP_BIND_DN",
        "ARBORA_QUAL_LDAP_PASSWORD",
        "ARBORA_QUAL_LDAP_BASE_DN",
        "ARBORA_QUAL_LDAP_GROUP_ROLES",
        "ARBORA_QUAL_LDAP_EXPECT_USER",
        "ARBORA_QUAL_LDAP_EXPECT_ROLES",
        "ARBORA_QUAL_LDAP_EXPECT_DISABLED",
    )
    directory = LdapDirectory(
        base_dn=env["ARBORA_QUAL_LDAP_BASE_DN"],
        password=lambda: env["ARBORA_QUAL_LDAP_PASSWORD"],
    )
    directory.bind(
        LdapBindConfig(
            host=env["ARBORA_QUAL_LDAP_HOST"],
            port=636,
            bind_dn=env["ARBORA_QUAL_LDAP_BIND_DN"],
        )
    )
    plan = plan_directory_sync(
        directory.snapshot(), json.loads(env["ARBORA_QUAL_LDAP_GROUP_ROLES"])
    )
    expected = {r for r in env["ARBORA_QUAL_LDAP_EXPECT_ROLES"].split(",") if r}
    assert set(plan.assignments[env["ARBORA_QUAL_LDAP_EXPECT_USER"]]) == expected
    assert env["ARBORA_QUAL_LDAP_EXPECT_DISABLED"] in plan.deprovision

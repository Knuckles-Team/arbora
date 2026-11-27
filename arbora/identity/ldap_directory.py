"""The LDAPS transport behind the directory sync job (ARBORA-IDENTITY-R010.2.2).

:class:`LdapDirectory` is the production :class:`~arbora.identity.directory_sync.Directory`:
``bind`` opens one TLS connection with the service account and ``snapshot`` reads every
person entry with its group DNs and disabled flag. The connection library (``ldap3``, a
package dependency) is loaded on first bind, and the bind is always TLS: nothing falls back to
a plaintext bind.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from arbora.identity.engine import IdentityUnavailable
from arbora.identity.ldap import DirectoryUser, LdapBindConfig

__all__ = ["LdapDirectory"]

#: ``userAccountControl`` bit 2: the account is disabled.
_ACCOUNT_DISABLED = 0x2
_ATTRIBUTES = ("sAMAccountName", "uid", "memberOf", "userAccountControl")


def _default_connection(config: LdapBindConfig, password: str) -> Any:
    import ldap3  # type: ignore[import-untyped,unused-ignore]

    server = ldap3.Server(config.host, port=config.port, use_ssl=True)
    return ldap3.Connection(
        server, user=config.bind_dn, password=password, auto_bind=True, read_only=True
    )


class LdapDirectory:
    """Bind once per run, then read the user snapshot beneath ``base_dn``."""

    def __init__(
        self,
        *,
        base_dn: str,
        password: Callable[[], str],
        user_filter: str = "(objectClass=person)",
        connection: Callable[[LdapBindConfig, str], Any] = _default_connection,
    ) -> None:
        if not base_dn:
            raise IdentityUnavailable("LDAP sync requires a base DN")
        self._base_dn = base_dn
        self._password = password
        self._filter = user_filter
        self._connection_factory = connection
        self._connection: Any = None

    def bind(self, config: LdapBindConfig) -> None:
        self._connection = self._connection_factory(config, self._password())

    def snapshot(self) -> list[DirectoryUser]:
        if self._connection is None:
            raise IdentityUnavailable("the directory is not bound")
        self._connection.search(
            self._base_dn, self._filter, attributes=list(_ATTRIBUTES)
        )
        users: list[DirectoryUser] = []
        for entry in self._connection.entries:
            values = entry.entry_attributes_as_dict
            name = _first(values.get("sAMAccountName")) or _first(values.get("uid"))
            if not name:
                continue
            control = _first(values.get("userAccountControl")) or 0
            users.append(
                DirectoryUser(
                    username=str(name),
                    group_dns=tuple(str(dn) for dn in values.get("memberOf") or ()),
                    disabled=bool(int(control) & _ACCOUNT_DISABLED),
                )
            )
        return users


def _first(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value

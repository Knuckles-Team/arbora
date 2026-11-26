"""Preferences served over the engine KV record (ARBORA-OPS-R024.7)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.api._served_support import person, route_methods
from tests.test_served_operation_api import _call, _value, served  # noqa: F401

SCOPES = ("kv:read", "kv:write")
_KEYS = iter(range(1, 100))


def _set(http: Any, params: dict[str, Any], **headers: str) -> Any:
    key = {"Idempotency-Key": f"pref-{next(_KEYS)}"}
    return _call(http, "preferences.set", params, **key, **headers)


class Kv:
    """One engine KV: entries keyed by (principal, namespace, key)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], tuple[bytes, int]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _id(self, principal: str, params: dict[str, Any]) -> tuple[str, str, str]:
        return (principal, params["namespace"], params["key"])

    def get(self, params: dict[str, Any], principal: str) -> Any:
        self.calls.append(("KvGet", params))
        row = self.rows.get(self._id(principal, params))
        return {} if row is None else {"value": row[0], "version": row[1]}

    def put(self, params: dict[str, Any], principal: str) -> Any:
        self.calls.append(("KvPut", params))
        key = self._id(principal, params)
        version = self.rows.get(key, (b"", 0))[1] + 1
        self.rows[key] = (bytes(params["value"]), version)
        return {"version": version}

    def cas(self, params: dict[str, Any], principal: str) -> Any:
        self.calls.append(("KvCas", params))
        key = self._id(principal, params)
        current = self.rows.get(key, (b"", 0))[1]
        if current != params["expected_version"]:
            return {"swapped": False, "version": current}
        self.rows[key] = (bytes(params["new"]), current + 1)
        return {"swapped": True, "version": current + 1}


@pytest.fixture
def kv(served: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Kv]:  # noqa: F811
    http, client = served
    store = Kv()
    route_methods(
        client,
        {"KvGet": store.get, "KvPut": store.put, "KvCas": store.cas},
        monkeypatch,
        {
            "fixture-verified": person("one", extra_scopes=SCOPES),
            "fixture-other": person("two", extra_scopes=SCOPES),
        },
    )
    return http, store


@pytest.mark.spec("ARBORA-OPS-R024.7")
def test_preferences_round_trip_isolate_principals_and_conflict(
    kv: tuple[Any, Kv],
) -> None:
    http, store = kv
    empty = _value(_call(http, "preferences.get", {}))
    assert empty == {"found": False, "value": {}, "version": 0}
    saved = _value(_set(http, {"value": {"theme": "dark"}}))
    assert saved == {"swapped": True, "version": 1}
    assert _value(_call(http, "preferences.get", {})) == {
        "found": True,
        "value": {"theme": "dark"},
        "version": 1,
    }
    stale = _value(_set(http, {"value": {"theme": "x"}, "expected_version": 0}))
    assert stale == {"swapped": False, "version": 1}
    won = _value(_set(http, {"value": {"theme": "light"}, "expected_version": 1}))
    assert won == {"swapped": True, "version": 2}
    assert all(
        c[1]["namespace"] == "preferences" and c[1]["key"] == "user"
        for c in store.calls
    )
    other = {"Authorization": "Bearer fixture-other"}
    assert _value(_call(http, "preferences.get", {}, **other))["found"] is False
    _set(http, {"value": {"theme": "mine"}}, **other)
    assert _value(_call(http, "preferences.get", {}))["value"] == {"theme": "light"}
    huge = _set(http, {"value": {"blob": "x" * 70_000}})
    assert huge.status_code == 400

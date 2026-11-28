"""The runner credential machine (ARBORA-HARNESS-PLUGINS-R003.3).

``login.transition`` is the only function that computes a credential state, and every
assignment of ``state`` in ``login.py`` stores its result.
"""

from __future__ import annotations

import ast
from itertools import product
from pathlib import Path

import pytest

from arbora.harness_plugins import login
from arbora.harness_plugins.login import CredentialEvent as E
from arbora.harness_plugins.login import CredentialState as S

# The edges the design names, written out independently of the table under test.
EXPECTED_EDGES = [
    pytest.param(S.UNAUTHENTICATED, E.BEGIN, S.AUTHORIZING, id="unauthenticated-begin"),
    pytest.param(S.AUTHORIZING, E.GRANT, S.AUTHORIZED, id="authorizing-grant"),
    pytest.param(S.AUTHORIZING, E.DENY, S.UNAUTHENTICATED, id="authorizing-deny"),
    pytest.param(
        S.AUTHORIZED, E.REFRESH_DUE, S.REFRESHING, id="authorized-refresh_due"
    ),
    pytest.param(S.REFRESHING, E.REFRESH_OK, S.AUTHORIZED, id="refreshing-refresh_ok"),
    pytest.param(S.REFRESHING, E.REFRESH_FAIL, S.EXPIRED, id="refreshing-refresh_fail"),
    pytest.param(S.AUTHORIZED, E.REVOKE, S.REVOKED, id="authorized-revoke"),
    pytest.param(S.EXPIRED, E.BEGIN, S.AUTHORIZING, id="expired-begin"),
    pytest.param(S.REVOKED, E.BEGIN, S.AUTHORIZING, id="revoked-begin"),
]
EXPECTED = {(a, e): t for a, e, t in (p.values for p in EXPECTED_EDGES)}


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R003.3")
@pytest.mark.parametrize(("state", "event", "target"), EXPECTED_EDGES)
def test_each_legal_edge_has_a_test(state: S, event: E, target: S) -> None:
    assert login.transition(state, event) is target


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R003.3")
def test_the_table_is_exactly_the_designed_edges() -> None:
    assert dict(login.CREDENTIAL_EDGES) == EXPECTED
    assert len(login.CREDENTIAL_EDGES) == 9


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R003.3")
def test_every_illegal_pair_is_refused_and_names_the_machine() -> None:
    refused = 0
    for state, event in product(S, E):
        if (state, event) in login.CREDENTIAL_EDGES:
            continue
        with pytest.raises(login.IllegalCredentialTransition) as raised:
            login.transition(state, event)
        assert raised.value.code == "CREDENTIAL_TRANSITION_ILLEGAL"
        assert raised.value.machine == "harness_credential"
        assert (raised.value.from_state, raised.value.event) == (state, event)
        refused += 1
    assert refused == len(S) * len(E) - len(login.CREDENTIAL_EDGES) == 33


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R003.3")
def test_the_initial_state_has_no_terminal_and_every_state_is_reachable() -> None:
    assert login.INITIAL is S.UNAUTHENTICATED
    assert login.TERMINALS == ()
    reached = {login.INITIAL}
    frontier = [login.INITIAL]
    while frontier:
        here = frontier.pop()
        for (source, _), target in login.CREDENTIAL_EDGES.items():
            if source is here and target not in reached:
                reached.add(target)
                frontier.append(target)
    assert reached == set(S)
    assert {source for source, _ in login.CREDENTIAL_EDGES} == set(S)


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R003.3")
def test_the_wire_values_are_unchanged() -> None:
    assert [s.value for s in S] == [
        "unauthenticated", "authorizing", "authorized",
        "refreshing", "expired", "revoked",
    ]  # fmt: skip
    assert [e.value for e in E] == [
        "begin", "grant", "deny", "refresh_due", "refresh_ok", "refresh_fail", "revoke",
    ]  # fmt: skip
    credential = login.Credential("codex", "https://door.test", "t", "r")
    assert json_state(credential) == "authorized"


def json_state(credential: login.Credential) -> str:
    import json

    return str(json.loads(credential.to_json())["state"])


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R003.3")
def test_only_login_transition_computes_a_credential_state() -> None:
    source = Path(login.__file__).read_text(encoding="utf-8")
    stores = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Attribute) and t.attr == "state" for t in node.targets
        )
    ]
    assert stores, "the credential stores its state somewhere"
    for node in stores:
        value = node.value
        assert (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "transition"
        ), ast.unparse(node)

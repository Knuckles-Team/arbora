"""The plugin install machine (ARBORA-HARNESS-PLUGINS-R002.1).

``install.transition`` is the only function that computes an install state, and every
assignment of ``state`` outside it stores its result.
"""

from __future__ import annotations

import ast
from itertools import product
from pathlib import Path

import pytest

from arbora.harness_plugins import install
from arbora.harness_plugins.install import InstallEvent as E
from arbora.harness_plugins.install import InstallState as S

# The edges the design names, written out independently of the table under test.
EXPECTED_EDGES = [
    pytest.param(S.ABSENT, E.GENERATE, S.GENERATED, id="absent-generate"),
    pytest.param(S.GENERATED, E.INSTALL, S.INSTALLED, id="generated-install"),
    pytest.param(S.INSTALLED, E.DRIFT, S.STALE, id="installed-drift"),
    pytest.param(S.STALE, E.INSTALL, S.INSTALLED, id="stale-install"),
    pytest.param(S.INSTALLED, E.REMOVE, S.REMOVED, id="installed-remove"),
    pytest.param(S.STALE, E.REMOVE, S.REMOVED, id="stale-remove"),
    pytest.param(S.REMOVED, E.GENERATE, S.GENERATED, id="removed-generate"),
]
EXPECTED = {(a, e): t for a, e, t in (p.values for p in EXPECTED_EDGES)}


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R002.1")
@pytest.mark.parametrize(("state", "event", "target"), EXPECTED_EDGES)
def test_each_legal_edge_has_a_test(state: S, event: E, target: S) -> None:
    assert install.transition(state, event) is target


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R002.1")
def test_the_table_is_exactly_the_designed_edges() -> None:
    assert dict(install.INSTALL_EDGES) == EXPECTED
    assert len(install.INSTALL_EDGES) == 7


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R002.1")
def test_every_illegal_pair_is_refused_and_names_the_machine() -> None:
    refused = 0
    for state, event in product(S, E):
        if (state, event) in install.INSTALL_EDGES:
            continue
        with pytest.raises(install.IllegalInstallTransition) as raised:
            install.transition(state, event)
        assert raised.value.code == "INSTALL_TRANSITION_ILLEGAL"
        assert raised.value.machine == "plugin_install"
        assert (raised.value.from_state, raised.value.event) == (state, event)
        refused += 1
    assert refused == len(S) * len(E) - len(install.INSTALL_EDGES) == 13


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R002.1")
def test_the_initial_state_has_no_terminal_and_every_state_is_reachable() -> None:
    assert install.INITIAL is S.ABSENT
    assert install.TERMINALS == ()
    reached = {install.INITIAL}
    frontier = [install.INITIAL]
    while frontier:
        here = frontier.pop()
        for (source, _), target in install.INSTALL_EDGES.items():
            if source is here and target not in reached:
                reached.add(target)
                frontier.append(target)
    assert reached == set(S)
    assert {source for source, _ in install.INSTALL_EDGES} == set(S)


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R002.1")
def test_the_wire_values_are_unchanged() -> None:
    assert [s.value for s in S] == [
        "absent", "generated", "installed", "stale", "removed",
    ]  # fmt: skip
    assert [e.value for e in E] == ["generate", "install", "drift", "remove"]


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R002.1")
def test_every_store_of_an_install_state_commits_the_transition_result() -> None:
    root = Path(install.__file__).parent
    stores = []
    for path in sorted(root.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Attribute) and t.attr == "state" for t in node.targets
            ):
                stores.append((path.name, node))
    assert stores, "the install record stores its state somewhere"
    for name, node in stores:
        value = node.value
        assert (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute | ast.Name)
            and getattr(value.func, "attr", getattr(value.func, "id", ""))
            == "transition"
        ), f"{name}: {ast.unparse(node)}"

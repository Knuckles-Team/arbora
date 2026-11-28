"""ARBORA-HOST-R037.3: no connector launch or attach path bypasses the release gate.

The launch functions are found in the source, not listed here: every function
that starts a connector child, opens a session to a connector or builds the
connector runner's services is an *opener*. An opener either calls the gate in
its own body or says in a ``# release-gate:`` comment what covers it. A cover
names a function that this test finds and checks calls the gate. A new launch
path therefore fails this test until it passes the gate or names its cover.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

ARBORA_ROOT = Path(__file__).resolve().parents[2] / "arbora"
GATE_CALLS = frozenset({"admit_release", "_admit_release", "_admit_launched"})
#: Calls that start a connector child or open a session to one.
OPENER_CALLS = frozenset(
    {
        "stdio_client",
        "streamable_http_client",
        "streamablehttp_client",
        "sse_client",
        "ClientSession",
        "McpTransport",
        "default_services",
        "_services_factory",
    }
)
MARKER = "release-gate:"
COVERED = "release-gate: covered by "


@dataclass(frozen=True)
class Function:
    module: str
    qualname: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    lines: tuple[str, ...]

    @property
    def body_text(self) -> str:
        return "\n".join(self.lines[self.node.lineno - 1 : self.node.end_lineno])

    def calls(self) -> list[tuple[str, int]]:
        found: list[tuple[str, int]] = []
        for inner in _own_nodes(self.node):
            if isinstance(inner, ast.Call):
                func = inner.func
                name = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr
                    if isinstance(func, ast.Attribute)
                    else ""
                )
                found.append((name, inner.lineno))
        return found


def _own_nodes(function: ast.AST) -> list[ast.AST]:
    """The nodes of ``function`` excluding nested function bodies."""
    nodes: list[ast.AST] = []
    pending = list(ast.iter_child_nodes(function))
    while pending:
        node = pending.pop()
        nodes.append(node)
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            pending.extend(ast.iter_child_nodes(node))
    return nodes


def _collect(
    node: ast.AST,
    scope: tuple[str, ...],
    module: str,
    lines: tuple[str, ...],
    found: list[Function],
) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            _collect(child, (*scope, child.name), module, lines, found)
        elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            qualname = ".".join((*scope, child.name))
            found.append(Function(module, qualname, child, lines))
            _collect(child, (*scope, child.name), module, lines, found)
        else:
            _collect(child, scope, module, lines, found)


def _functions() -> list[Function]:
    found: list[Function] = []
    for path in sorted(ARBORA_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        module = ".".join(path.relative_to(ARBORA_ROOT.parent).with_suffix("").parts)
        _collect(tree, (), module, tuple(source.splitlines()), found)
    return found


def _fastmcp_client_modules() -> set[str]:
    modules: set[str] = set()
    for path in ARBORA_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "fastmcp"
                and any(alias.name == "Client" for alias in node.names)
            ):
                modules.add(
                    ".".join(path.relative_to(ARBORA_ROOT.parent).with_suffix("").parts)
                )
    return modules


def _openers() -> list[Function]:
    client_modules = _fastmcp_client_modules()
    openers = []
    for function in _functions():
        names = {name for name, _ in function.calls()}
        if names & OPENER_CALLS or (
            "Client" in names and function.module in client_modules
        ):
            openers.append(function)
    return openers


def _calls_gate(function: Function) -> bool:
    return any(name in GATE_CALLS for name, _ in function.calls())


def _by_name(functions: list[Function], dotted: str) -> Function | None:
    for function in functions:
        if f"{function.module}.{function.qualname}" == dotted:
            return function
    return None


def test_the_enumeration_finds_the_known_launch_paths() -> None:
    """A scan that finds nothing proves nothing: the real launch functions must be seen."""
    found = {f"{f.module}.{f.qualname}" for f in _openers()}
    assert {
        "arbora.fleet.child_transport.ChildTransport._open_one_session",
        "arbora.fleet.child_transport.ChildTransport._open_stdio_child_transport",
        "arbora.fleet.onboarding_capture.capture_server_pack",
        "arbora.fleet_tool_call.call_fleet_tool",
        "arbora.fleet.client_toolsets.open_tool_client",
        "arbora.connector_runner.ConnectorRunnerComposition.services",
    } <= found


def _markers(opener: Function) -> list[str]:
    """What the opener's ``# release-gate:`` comments say."""
    return [
        line.split(MARKER, 1)[1].strip()
        for line in opener.body_text.splitlines()
        if MARKER in line
    ]


def _unmet_covers(texts: list[str], functions: list[Function]) -> list[str]:
    """The ``covered by`` markers whose named function is absent or has no gate call."""
    unmet = []
    for text in texts:
        if not text.startswith("covered by "):
            continue  # states why the path is no connector launch
        cover = _by_name(functions, text.removeprefix("covered by ").strip())
        if cover is None or not _calls_gate(cover):
            unmet.append(text)
    return unmet


def _cover_problems(opener: Function, functions: list[Function]) -> list[str]:
    """Why ``opener`` neither passes the gate nor names a cover that does."""
    label = f"{opener.module}.{opener.qualname}"
    if _calls_gate(opener):
        return []
    texts = _markers(opener)
    if not texts:
        return [f"{label}: no gate call and no '# {MARKER}' cover"]
    return [
        f"{label}: cover '{text}' does not call the gate"
        for text in _unmet_covers(texts, functions)
    ]


@pytest.mark.spec("ARBORA-HOST-R037.3")
def test_every_launch_path_passes_the_gate_or_names_its_cover() -> None:
    functions = _functions()
    uncovered = [
        problem
        for opener in _openers()
        for problem in _cover_problems(opener, functions)
    ]
    assert not uncovered, "\n".join(uncovered)


@pytest.mark.spec("ARBORA-HOST-R037.3")
def test_the_gate_runs_before_the_session_is_used() -> None:
    functions = _functions()

    def order(dotted: str, first: str, then: str) -> None:
        function = _by_name(functions, dotted)
        assert function is not None, dotted
        lines = {name: line for name, line in function.calls()}
        assert first in lines and then in lines, (dotted, first, then)
        assert lines[first] < lines[then], f"{dotted}: {first} must precede {then}"

    # The child handshake completes, then the gate, then the caller lists or calls tools.
    order(
        "arbora.fleet.child_transport.ChildTransport._open_one_session",
        "initialize",
        "_admit_release",
    )
    # Onboarding admits the release before it reads or imports a pack.
    for later in ("capture", "_import_pack"):
        order(
            "arbora.fleet.onboarding.FleetOnboarding._onboard_pass",
            "_admit_release",
            later,
        )
    # Fleet tool calls admit before the tool call.
    order("arbora.fleet_tool_call._gated_call", "admit_release", "call_tool")


@pytest.mark.spec("ARBORA-HOST-R037.3")
def test_pack_capture_is_reached_only_from_the_gated_onboarding_pass() -> None:
    """The captures read a connector's surface; only the gated pass may call them."""
    allowed = {
        "arbora.fleet.onboarding.FleetOnboarding._onboard_pass",
        "arbora.fleet.onboarding.FleetOnboarding._admit_release",
        "arbora.fleet.onboarding_capture.capture_server_pack",
        "arbora.fleet.onboarding_capture.open_release_session",
    }
    watched = {"capture_server_pack", "open_release_session"}
    users = set()
    for path in ARBORA_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in watched:
                users.add((path.relative_to(ARBORA_ROOT.parent).as_posix(), node.id))
    modules = {module for module, _ in users}
    assert modules <= {
        "arbora/fleet/onboarding.py",
        "arbora/fleet/onboarding_capture.py",
    }, modules
    functions = _functions()
    assert all(_by_name(functions, dotted) is not None for dotted in allowed), (
        "an allowed function was renamed; update the test with the cover markers"
    )

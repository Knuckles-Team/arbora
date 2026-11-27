"""The changeset submitter and sandbox gate runner (ARBORA-OPS-R894).

The refusal tests run under the current meristem pin, whose changeset cycle cannot import
because ``HostPorts`` is missing. The real-path tests install a structural double of
meristem ``main``'s ``changeset_loop`` module and of the source mount, and drive the
adapters through them.
"""

from __future__ import annotations

import asyncio
import dataclasses
import sys
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from arbora.agent_host.host_port_composition import PortBackends, compose_host_ports
from arbora.api.domain.agent_host import host_port_adapters as adapters
from arbora.pin_lag import meristem as guard
from tests.pin_lag.test_host_ports_pin_lag import main_shape  # noqa: F401


def _pinned_has_changeset_loop() -> bool:
    try:
        from meristem.harness import changeset_loop  # noqa: F401
    except ImportError:
        return False
    return True


@dataclasses.dataclass(frozen=True)
class _Submitted:
    changeset_id: str


@dataclasses.dataclass(frozen=True)
class _Conflict:
    paths: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class _Pending:
    base_head: str
    edits: tuple[Any, ...]
    run_ref: str
    files: tuple[tuple[str, str], ...]
    gate_verdicts: tuple[Any, ...] = ()


@pytest.fixture
def cycle_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    import meristem.harness as harness

    double = types.ModuleType("meristem.harness.changeset_loop")
    double.Submitted, double.SourceConflict = _Submitted, _Conflict  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "meristem.harness.changeset_loop", double)
    monkeypatch.setattr(harness, "changeset_loop", double, raising=False)


@dataclasses.dataclass
class _Entry:
    path: str
    mode: str = "100644"


class _Mount:
    """Structural double of ``scion_sdk.source.mount.SourceMount``."""

    started: list[dict[str, Any]] = []

    def __init__(self, root: Path, files: Mapping[str, str]) -> None:
        self.root, self.files = root, files

    @classmethod
    async def start(cls, client: Any, **kwargs: Any) -> _Mount:
        cls.started.append(kwargs)
        return cls(kwargs["root"], client.files)

    async def listdir(self, prefix: str) -> tuple[_Entry, ...]:
        found = [_Entry(path) for path in sorted(self.files)]
        return (*found, _Entry("src", mode="040000"))

    async def read(self, path: str) -> bytes:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.files[path], encoding="utf-8")
        return self.files[path].encode()


class _Client:
    def __init__(self, files: Mapping[str, str], head: str) -> None:
        self.files = files
        self.head = head

    async def tree(self, request: Any) -> Any:
        provenance = types.SimpleNamespace(revision=self.head)
        return types.SimpleNamespace(provenance=provenance, entries=())


@pytest.fixture
def mount_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    import scion_sdk.source.mount as mount

    _Mount.started = []
    monkeypatch.setattr(mount, "SourceMount", _Mount)


class _Backend:
    def __init__(self, root: Path) -> None:
        self.root = root


class _Workspace:
    def __init__(self, root: Path, exit_code: int = 0) -> None:
        self.backend = _Backend(root)
        self.commands: list[str] = []
        self.exit_code = exit_code

    async def act(self, action: Any) -> Any:
        from meristem.runtime.events import CmdOutputObservation

        self.commands.append(action.command)
        return CmdOutputObservation(exit_code=self.exit_code)


TARGET = adapters.SourceTarget("repo-a", "feature", ("src",))


def _seams(
    files: Mapping[str, str] | None = None,
    *,
    staged: list[bytes] | None = None,
    sandbox: Any = None,
) -> adapters.ChangesetSeams:
    async def stage(run_ref: str, content: bytes) -> Mapping[str, Any]:
        if staged is not None:
            staged.append(content)
        return {"digest": f"sha256:{len(content)}", "size": len(content)}

    return adapters.ChangesetSeams(
        target=lambda run: TARGET if run == "run:1" else None,
        source_client=lambda run: _Client(files or {}, "head-9"),
        stage_blob=stage,
        is_sandbox=sandbox or (lambda backend: True),
    )


@pytest.mark.spec("ARBORA-OPS-R894")
@pytest.mark.skipif(_pinned_has_changeset_loop(), reason="the pin carries the cycle")
def test_pinned_release_refuses_the_submitter_naming_the_cycle_module() -> None:
    with pytest.raises(guard.PinLagUnavailable) as record:
        guard.changeset_types()
    assert record.value.missing == "meristem.harness.changeset_loop"

    async def call(op: str, params: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
        raise AssertionError("nothing may reach the door")

    submitter = adapters.ChangesetSubmitter(call, _seams(), "run:1", TARGET)
    with pytest.raises(guard.PinLagUnavailable):
        asyncio.run(submitter.submit(_Pending("h", (), "run:1", (("a", "x"),))))


@pytest.mark.spec("ARBORA-OPS-R894")
def test_registration_refuses_without_a_verified_boundary_or_a_grant() -> None:
    async def call(op: str, params: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
        return {}

    seams = _seams()
    assert (
        adapters.changeset_submitter_factory(call, seams, lambda: False)("run:1")
        is None
    )
    assert adapters.gate_runner_factory(seams, lambda: False)("run:1") is None
    assert (
        adapters.changeset_submitter_factory(call, seams, lambda: True)("run:2") is None
    )
    assert adapters.gate_runner_factory(seams, lambda: True)("run:2") is None
    assert adapters.changeset_submitter_factory(call, seams, lambda: True)("run:1")


@pytest.mark.spec("ARBORA-OPS-R894")
def test_materialize_commit_land_and_conflict_through_the_door(
    tmp_path: Path, cycle_shape: None, mount_shape: None
) -> None:
    seen: list[tuple[str, Mapping[str, Any]]] = []
    answers = [
        {
            "outcome": "refused",
            "refusal_code": "SOURCE_CONFLICT",
            "payload": {"paths": ["src/a.py"]},
        },
        {"outcome": "answered", "payload": {"value": {"changeset": {"id": "cs-1"}}}},
        {"outcome": "refused", "refusal_code": "POLICY_DENIED"},
        {"outcome": "refused", "refusal_code": "UNAVAILABLE"},
        {"outcome": "answered", "payload": {"value": {}}},
    ]

    async def call(op: str, params: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
        assert ref == "run:1"
        seen.append((op, params))
        return answers.pop(0)

    staged: list[bytes] = []
    seams = _seams({"src/a.py": "old\n"}, staged=staged)
    submitter = adapters.ChangesetSubmitter(call, seams, "run:1", TARGET)
    (tmp_path / "stale.txt").write_text("left over")
    workspace = _Workspace(tmp_path)

    async def scenario() -> None:
        assert await submitter.read_head() == "head-9"
        await submitter.materialize("head-9", workspace)
        assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == ["a.py"]
        assert _Mount.started[0]["ref"] == "head-9"
        assert _Mount.started[0]["prefixes"] == ("src",)
        pending = _Pending(
            "head-9", (), "run:1", (("src/a.py", "new\n"), ("src/b.py", "b\n"))
        )
        conflict = await submitter.submit(pending)
        assert conflict == _Conflict(("src/a.py",))
        assert [op for op, _ in seen] == ["source.commit"]
        accepted = await submitter.submit(pending)
        assert accepted == _Submitted("cs-1")
        # Landing is the owner side's decision: a refused land still reports submission.
        assert [op for op, _ in seen][1:] == ["source.commit", "source.land"]
        commit = seen[1][1]
        assert [(p["path"], p["op"]) for p in commit["paths"]] == [
            ("src/a.py", "modify"),
            ("src/b.py", "add"),
        ]
        assert commit["base_head"] == "head-9" and commit["run_trace_id"] == "run:1"
        assert seen[2][1] == {
            "repository": "repo-a",
            "branch": "feature",
            "changeset": "cs-1",
        }
        # Any other refusal carries no paths, which the harness reports as refused.
        assert await submitter.submit(pending) == _Conflict(())
        assert await submitter.submit(pending) == _Conflict(())

    asyncio.run(scenario())
    assert staged[:2] == [b"new\n", b"b\n"]


@pytest.mark.spec("ARBORA-OPS-R894")
def test_gate_runner_validates_the_whole_edit_set_before_writing(
    tmp_path: Path,
) -> None:
    from meristem.harness.edit_engine import Edit

    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    workspace = _Workspace(tmp_path)
    runner = adapters.SandboxGateRunner(_seams())
    good = Edit(path="a.txt", search="one\n", replace="two\n")
    bad = Edit(path="a.txt", search="absent\n", replace="x\n")

    async def scenario() -> None:
        assert await runner.apply_edits(workspace, (good, bad)) is False
        assert (tmp_path / "a.txt").read_text() == "one\n"
        assert await runner.apply_edits(workspace, (good,)) is True
        assert await runner.read_files(workspace, ("a.txt",)) == (("a.txt", "two\n"),)
        (tmp_path / "out").symlink_to("/")
        with pytest.raises(OSError):
            await runner.read_files(workspace, ("out/etc/hostname",))
        ran = await runner.run_gate(workspace, "make test")
        assert ran.exit_code == 0 and workspace.commands == ["make test"]

    asyncio.run(scenario())


@pytest.mark.spec("ARBORA-OPS-R894")
def test_gate_runner_never_runs_outside_the_sandbox_backend(tmp_path: Path) -> None:
    from meristem.harness.edit_engine import Edit

    workspace = _Workspace(tmp_path)
    runner = adapters.SandboxGateRunner(_seams(sandbox=lambda backend: False))
    edit = Edit(path="a.txt", search="", replace="x\n")

    async def scenario() -> None:
        assert await runner.apply_edits(workspace, (edit,)) is False
        assert not (tmp_path / "a.txt").exists()
        with pytest.raises(OSError):
            await runner.read_files(workspace, ("a.txt",))
        refused = await runner.run_gate(workspace, "make test")
        assert (refused.exit_code, refused.stderr) == (126, "SANDBOX_UNAVAILABLE")
        assert workspace.commands == []

    asyncio.run(scenario())


@pytest.mark.spec("ARBORA-OPS-R894")
def test_host_composition_registers_both_ports_only_with_the_seams(
    main_shape: None,  # noqa: F811
) -> None:
    async def wait(seconds: float) -> None:
        return None

    bare = compose_host_ports(object(), wait=wait, backends=PortBackends())
    assert bare.refusal is None
    assert bare.ports.changeset_submitter is None and bare.ports.gate_runner is None
    bound = compose_host_ports(
        object(),
        wait=wait,
        backends=PortBackends(changeset=_seams(), sandbox_verified=lambda: True),
    )
    assert bound.ports.changeset_submitter("run:1") is not None
    assert isinstance(bound.ports.gate_runner("run:1"), adapters.SandboxGateRunner)
    assert bound.ports.gate_runner("run:2") is None

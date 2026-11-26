"""``arbora acceptance run`` and its scenario interpreter (ARBORA-DEPLOY-R602.3)."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from arbora.deployment.acceptance import fleet_audit, runner, scenarios

PACKAGE = Path(runner.__file__).parent


class FakeGateway:
    """An in-memory stand-in for the MCP session: records calls, answers by op id."""

    instances: list[FakeGateway] = []

    def __init__(self, config: runner.AcceptanceConfig, mint: Any) -> None:
        self.config = config
        self.mint = mint
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        FakeGateway.instances.append(self)

    async def __aenter__(self) -> FakeGateway:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def op(
        self, verb: str, op: str, params: dict[str, Any], idempotent: bool = False
    ) -> dict[str, Any]:
        self.calls.append((verb, op, params))
        if op == "ops.ping":
            return {"ok": True, "result": {"value": {"status": "up", "n": 3}}}
        return {"ok": False, "error": {"code": "unknown", "message": op}}


def _write(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.mark.spec("ARBORA-DEPLOY-R602.3")
def test_runner_reads_every_input_from_the_config_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Gateway, CA, token reference, scenarios and output all come from the file."""
    monkeypatch.setenv("ARBORA_BASE_URL", "https://ignored.example.test")
    scenario_dir = tmp_path / "scenarios"
    scenario_dir.mkdir()
    _write(
        scenario_dir / "one.yml",
        {"name": "one", "steps": [{"id": "ping", "call": "ops.ping"}]},
    )
    config = _write(
        tmp_path / "acceptance.yml",
        {
            "gateway_url": "https://gateway.example.test/",
            "ca_bundle": "/trust/ca.pem",
            "token": "file:/secrets/bearer",
            "concurrency": 2,
            "output": str(tmp_path / "out" / "rows.json"),
            "scenarios": str(scenario_dir),
        },
    )
    references: list[str] = []
    FakeGateway.instances.clear()
    monkeypatch.setattr(runner, "Arbora", FakeGateway)
    monkeypatch.setattr(
        runner, "secret_minter", lambda ref: (references.append(ref), lambda: "t")[1]
    )

    assert runner.main(["run", "--config", str(config)]) == 0

    gateway = FakeGateway.instances[0]
    assert gateway.config.gateway_url == "https://gateway.example.test"
    assert gateway.config.ca_bundle == "/trust/ca.pem"
    assert gateway.config.concurrency == 2
    assert references == ["file:/secrets/bearer"]
    summary = json.loads((tmp_path / "out" / "rows.json").read_text())
    assert summary["base"] == "https://gateway.example.test"
    assert (tmp_path / "out" / "rows.md").read_text().startswith("acceptance: 1 pass")
    assert "1 pass" in capsys.readouterr().out
    assert "os.environ" not in (PACKAGE / "runner.py").read_text()
    for unknown in (
        {"gateway_url": "u", "ca_bundle": "c", "token": "t", "surprise": 1},
    ):
        with pytest.raises(runner.AcceptanceConfigError, match="surprise"):
            runner.AcceptanceConfig.load(_write(tmp_path / "bad.yml", unknown))
    with pytest.raises(runner.AcceptanceConfigError, match="token"):
        runner.AcceptanceConfig.load(
            _write(tmp_path / "bad2.yml", {"gateway_url": "u", "ca_bundle": "c"})
        )


@pytest.mark.spec("ARBORA-DEPLOY-R602.3")
def test_runner_exits_one_when_a_row_fails_and_two_on_a_bad_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario_dir = tmp_path / "s"
    scenario_dir.mkdir()
    _write(
        scenario_dir / "bad.yml",
        {"name": "bad", "steps": [{"id": "x", "call": "no.such"}]},
    )
    config = _write(
        tmp_path / "a.yml",
        {
            "gateway_url": "https://g.example.test",
            "ca_bundle": "c",
            "token": "env:X",
            "output": str(tmp_path / "o.json"),
            "scenarios": str(scenario_dir),
        },
    )
    monkeypatch.setattr(runner, "Arbora", FakeGateway)
    assert (
        runner.main(
            ["run", "--config", str(config), "--scenario", "bad"], mint=lambda: "t"
        )
        == 1
    )
    assert (
        runner.main(
            ["run", "--config", str(config), "--scenario", "nope"], mint=lambda: "t"
        )
        == 2
    )
    assert runner.main(["run", "--config", str(tmp_path / "missing.yml")]) == 2


@pytest.mark.spec("ARBORA-DEPLOY-R602.3")
def test_bearer_is_resolved_again_before_it_lapses() -> None:
    now = [1000.0]
    minted: list[str] = []

    def mint() -> str:
        minted.append("t")
        return f"tok{len(minted)}"

    bearer = runner.RefreshingBearer(mint, clock=lambda: now[0])
    assert bearer.current() == "tok1"  # no exp claim: valid for 120 s
    assert bearer.current() == "tok1"
    now[0] += 100
    assert bearer.current() == "tok2"


def _scenario(steps: list[dict[str, Any]]) -> scenarios.Scenario:
    return scenarios.parse_scenario({"name": "dag", "steps": steps}, "dag.yml")


@pytest.mark.spec("ARBORA-DEPLOY-R602.3")
def test_scenario_interpreter_runs_a_dag_and_reports_a_failed_expectation() -> None:
    order: list[str] = []

    async def call(verb: str, op: str, params: dict[str, Any]) -> dict[str, Any]:
        order.append(op)
        if op == "first":
            return {"ok": True, "result": {"value": {"items": [{"id": "a1"}], "n": 2}}}
        if op == "second":
            return {"ok": True, "result": {"echo": params["ref"], "n": 1}}
        return {"ok": True, "result": {}}

    scenario = _scenario(
        [
            {
                "id": "s2",
                "call": "second",
                "args": {"ref": "${s1.items[0].id}"},
                "needs": ["s1"],
                "expect": {"path": "echo", "op": "eq", "value": "a1"},
            },
            {
                "id": "s1",
                "call": "first",
                "expect": {"path": "n", "op": "gt", "value": 5},
            },
            {"id": "s3", "call": "third", "needs": ["s1"]},
        ]
    )
    writer = runner.ResultWriter(echo=lambda line: None)
    asyncio.run(scenarios.run_scenario(scenario, call, writer))

    rows = {r.check: r for r in writer.rows}
    assert order[0] == "first"
    assert rows["s1"].result == "FAIL" and "gt 5" in rows["s1"].evidence
    assert rows["s2"].result == "FAIL" and rows["s2"].evidence == "blocked by s1"
    assert rows["s3"].result == "FAIL"
    assert "second" not in order and "third" not in order

    passing = _scenario(
        [
            {"id": "s1", "call": "first"},
            {
                "id": "s2",
                "call": "second",
                "args": {"ref": "${s1.items[0].id}"},
                "needs": ["s1"],
                "expect": {"path": "echo", "op": "eq", "value": "a1"},
            },
        ]
    )
    good = runner.ResultWriter(echo=lambda line: None)
    asyncio.run(scenarios.run_scenario(passing, call, good))
    assert [r.result for r in good.rows] == ["PASS", "PASS"]


@pytest.mark.spec("ARBORA-DEPLOY-R602.3")
def test_scenario_files_refuse_unknown_needs_and_cycles() -> None:
    with pytest.raises(scenarios.ScenarioError, match="unknown step"):
        _scenario([{"id": "a", "call": "x", "needs": ["ghost"]}])
    with pytest.raises(scenarios.ScenarioError, match="cycle"):
        _scenario(
            [
                {"id": "a", "call": "x", "needs": ["b"]},
                {"id": "b", "call": "x", "needs": ["a"]},
            ]
        )
    with pytest.raises(scenarios.ScenarioError, match="unknown step fields"):
        _scenario([{"id": "a", "call": "x", "mystery": 1}])


@pytest.mark.spec("ARBORA-DEPLOY-R602.3")
def test_fleet_audit_calls_only_read_only_tools_without_write_names() -> None:
    schema = {
        "type": "object",
        "properties": {"limit": {"type": "integer"}},
        "required": [],
    }
    tools = [
        {
            "name": "thing_delete",
            "annotations": {"readOnlyHint": True},
            "input_schema": schema,
        },
        {
            "name": "thing_list",
            "annotations": {"readOnlyHint": True},
            "input_schema": schema,
        },
        {
            "name": "thing_status",
            "annotations": {"readOnlyHint": True},
            "input_schema": schema,
        },
        {"name": "thing_get", "annotations": {}, "input_schema": schema},
        {
            "name": "needs_id",
            "annotations": {"readOnlyHint": True},
            "input_schema": {
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    ]
    picked = fleet_audit.read_only_candidates(tools)
    assert [name for name, _ in picked] == ["thing_status", "thing_list"]
    assert picked[0][1] == {"limit": 1}


@pytest.mark.spec("ARBORA-DEPLOY-R602.3")
def test_no_scenario_name_is_in_the_package() -> None:
    """The scenario content is data; no system or scenario name is a literal here."""
    text = "\n".join(p.read_text() for p in PACKAGE.glob("*.py")).lower()
    for system in ("gitlab", "servicenow", "plane", "keycloak", "jellyfin", "plan_run"):
        assert not re.search(rf"\b{system}\b", text), system
    assert not list(PACKAGE.glob("*.yml"))

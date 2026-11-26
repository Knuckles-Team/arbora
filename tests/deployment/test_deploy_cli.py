"""The thin ``setup-config deploy`` subcommand (ARBORA-DEPLOY-R017.8)."""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from arbora.client import http as client_http
from arbora.client._models import registry_digest
from arbora.deployment import cli

_RECORD = {"target": "helm", "reason": "ha", "scores": {"helm": 3}}
_GRAPH = {"graph": {"nodes": [{"id": "render"}]}, "steps": []}


# spec: ARBORA-DEPLOY-R017.8
def test_output_equals_the_engine_decision_and_graph(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sent: list[tuple[str, dict[str, Any], str]] = []

    async def fake_invoke(
        self: Any, op_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        sent.append((op_id, dict(params), self._token))
        return {
            "ok": True,
            "result": {"value": {"decision": _RECORD, "assembly": _GRAPH, "x": 1}},
            "meta": {"registry_digest": registry_digest()},
        }

    monkeypatch.setattr(client_http.HttpTransport, "invoke", fake_invoke)
    monkeypatch.setenv("ARBORA_TOKEN", "tok")
    request = {"workload_iri": "urn:w", "candidate_targets": ["helm"]}
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")

    code = cli.main(["deploy", str(path), "--url", "https://arbora.example"])

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {
        "decision": _RECORD,
        "graph": _GRAPH,
    }
    assert sent == [("deploy.placement.plan", request, "tok")]


# spec: ARBORA-DEPLOY-R017.8
def test_a_refusal_prints_the_door_code_and_the_handler_holds_no_planner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def refuse(self: Any, op_id: str, params: dict[str, Any]) -> Any:
        raise client_http.OperationError("UNAVAILABLE")

    monkeypatch.setattr(client_http.HttpTransport, "invoke", refuse)
    monkeypatch.setenv("ARBORA_TOKEN", "tok")
    path = tmp_path / "r.json"
    path.write_text("{}", encoding="utf-8")
    assert cli.main(["deploy", str(path), "--url", "https://a.example"]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "UNAVAILABLE"

    tree = ast.parse(inspect.getsource(cli._run_deploy))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not {m for m in imported if "artifacts" in m or "placement" in m}

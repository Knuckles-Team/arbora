"""Operator configuration is explicit, strict and validated before launch."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arbora.core.settings import ArboraConfig, runtime_config
from arbora.mcp_server.task_plan import configured_task_plan

pytestmark = pytest.mark.spec("ARBORA-HOST-R001.2.1.2")
CONFIG = {
    "policy": {"policy": "default"},
    "max_concurrency": 2,
    "max_steps": 4,
}


def test_default_disabled(monkeypatch):
    monkeypatch.delenv("ARBORA_TASK_PLAN", raising=False)
    assert configured_task_plan() is None


def test_config_model_accepts_missing_static_schema_pin():
    config = ArboraConfig(ARBORA_TASK_PLAN=json.dumps(CONFIG))
    assert config.task_plan is not None
    assert "feature_schema" not in config.task_plan


@pytest.mark.spec("ARBORA-OPS-R021.4.5.1.2")
@pytest.mark.parametrize(
    "extra, expected",
    [({}, "off"), ({"plan_scaffold": "off"}, "off"), ({"plan_scaffold": "on"}, "on")],
)
def test_config_json_setting_reaches_trusted_composition(monkeypatch, extra, expected):
    from arbora.mcp_server import served_engine, served_invocation

    monkeypatch.setenv("ARBORA_TASK_PLAN", json.dumps({**CONFIG, **extra}))
    monkeypatch.setitem(
        runtime_config._overrides, "graph_service_endpoints", ["unix:///fixture"]
    )
    monkeypatch.setattr(served_engine, "process_engine_pool", lambda: object())
    compose = Mock(return_value=object())
    monkeypatch.setattr(served_invocation, "compose_served_invocation", compose)
    served_invocation.served_invocation_from_settings(
        process_session=lambda: SimpleNamespace(graph="fixture"),
        bindings={},
        fleet_effect=None,
    )
    trusted = compose.call_args.kwargs["task_plan_config"]
    assert trusted.plan_scaffold == expected
    assert trusted.max_steps == 4
    assert trusted.feature_schema == {}


@pytest.mark.parametrize(
    "change",
    [
        {"max_steps": True},
        {"max_concurrency": 9},
        {"timeout_seconds": "10"},
        {"timeout_seconds": float("inf")},
        {"tenant": "injected"},
        {"model": "injected"},
        {"client": "injected"},
        {"policy": []},
        {"plan_scaffold": True},
        {"plan_scaffold": "ON"},
        {"plan_scaffold": None},
    ],
)
def test_malformed_settings_fail_closed(monkeypatch, change):
    monkeypatch.setenv("ARBORA_TASK_PLAN", json.dumps({**CONFIG, **change}))
    with pytest.raises((TypeError, ValueError)):
        configured_task_plan()
    with pytest.raises((TypeError, ValueError)):
        ArboraConfig(ARBORA_TASK_PLAN=json.dumps({**CONFIG, **change}))


@pytest.mark.parametrize("raw", ["not-json", "[]", '{"max_steps":'])
def test_bad_json_never_silently_disables_feature(monkeypatch, raw):
    monkeypatch.setenv("ARBORA_TASK_PLAN", raw)
    with pytest.raises(ValueError):
        configured_task_plan()


def test_content_digest_pin_refuses_before_process_authority(monkeypatch):
    from epistemic_graph.decision_client import TASK_PLAN_FEATURE_SCHEMA

    from arbora.mcp_server import server

    monkeypatch.setenv(
        "ARBORA_TASK_PLAN",
        json.dumps(
            {
                **CONFIG,
                "feature_schema": {
                    "component_id": "arbora.agents.plan_run.feature_schema",
                    "definition_digest": TASK_PLAN_FEATURE_SCHEMA["content_digest"],
                    "kind": "feature_schema",
                },
            }
        ),
    )
    mint = Mock(side_effect=AssertionError("must not mint"))
    monkeypatch.setattr(server.runtime, "_mint_process_session", mint)
    with pytest.raises(ValueError, match="content digest"):
        server._served_invocation()
    mint.assert_not_called()

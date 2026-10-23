"""The published task-plan schema pin binds only when verified (R003.4.4).

The engine modules are fixtures with the engine's own helper shapes, so the
tests cover a final pin before the engine release that publishes it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api import task_plan_schema as pin_module


def _stat(schema_version: int = 1) -> SimpleNamespace:
    def encode_body(value: Any) -> tuple[str, dict[str, str]]:
        data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(data).hexdigest(), {}

    return SimpleNamespace(
        feature_schema_body=lambda features: {
            "schema_version": schema_version,
            "features": list(features),
        },
        feature=lambda name, kind: {"name": name, "kind": kind},
        text_bm25=lambda key, param: {
            "feature": "text_bm25",
            "key": key,
            "param": param,
        },
        encode_body=encode_body,
    )


DEFINITION = "sha256:" + "d" * 64


def _facade(definition: str | None = DEFINITION) -> SimpleNamespace:
    if definition is None:
        return SimpleNamespace()
    return SimpleNamespace(
        TASK_PLAN_FEATURE_SCHEMA={
            "component_id": pin_module.COMPONENT_ID,
            "definition_digest": definition,
        }
    )


_GENERATE = pin_module.generate_pin


def _pin(**kwargs: Any) -> dict[str, Any]:
    return _GENERATE(
        stat=kwargs.pop("stat", _stat()),
        facade=kwargs.pop("facade", _facade()),
        engine_version=kwargs.pop("engine_version", "2.27.5"),
    )


# spec: ARBORA-RELEASE-R003.4.4
def test_matching_final_pin_binds() -> None:
    committed = _pin()
    assert committed["final"] is True
    bound, reason = pin_module.bound_feature_schema(committed, _pin())
    assert reason is None
    assert bound == {
        "component_id": pin_module.COMPONENT_ID,
        "kind": "feature_schema",
        "definition_digest": DEFINITION,
    }


# spec: ARBORA-RELEASE-R003.4.4
def test_newer_engine_with_same_schema_keeps_the_pin() -> None:
    bound, reason = pin_module.bound_feature_schema(
        _pin(), _pin(engine_version="2.27.6")
    )
    assert reason is None and bound is not None


# spec: ARBORA-RELEASE-R003.4.4
@pytest.mark.parametrize(
    ("installed", "field"),
    [
        (lambda: _pin(stat=_stat(schema_version=2)), "schema_version"),
        (lambda: _pin(facade=_facade("sha256:" + "e" * 64)), "definition_digest"),
    ],
)
def test_drifted_pin_does_not_bind(installed: Any, field: str) -> None:
    bound, reason = pin_module.bound_feature_schema(_pin(), installed())
    assert bound is None
    assert reason is not None and field in reason


# spec: ARBORA-RELEASE-R003.4.4
def test_changed_body_digest_does_not_bind() -> None:
    committed = {**_pin(), "content_digest": "sha256:" + "0" * 64}
    bound, reason = pin_module.bound_feature_schema(committed, _pin())
    assert bound is None and reason is not None and "content_digest" in reason


# spec: ARBORA-RELEASE-R003.4.4
def test_unfinished_pin_does_not_bind() -> None:
    unfinished = _pin(facade=_facade(None))
    assert unfinished["final"] is False and unfinished["definition_digest"] is None
    bound, reason = pin_module.bound_feature_schema(unfinished, unfinished)
    assert bound is None and reason == "task-plan schema pin is not final"


# spec: ARBORA-RELEASE-R003.4.4
def test_startup_logs_and_skips_a_drifted_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "task_plan_schema.json"
    path.write_bytes(pin_module.pin_bytes(_pin()))
    monkeypatch.setattr(
        pin_module, "generate_pin", lambda: _pin(stat=_stat(schema_version=2))
    )
    assert pin_module.startup_feature_schema(path) is None
    assert "schema_version" in caplog.text
    monkeypatch.setattr(pin_module, "generate_pin", _pin)
    assert pin_module.startup_feature_schema(path) is not None


# spec: ARBORA-RELEASE-R003.4.4
def test_committed_pin_matches_the_installed_engine() -> None:
    pytest.importorskip("epistemic_graph.decision_stat")
    committed = pin_module.load_pin()
    assert pin_module.drift(committed, pin_module.generate_pin()) == []

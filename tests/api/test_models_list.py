"""Model read operations (ARBORA-OPS-R922.2)."""

from __future__ import annotations

import json

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import models

pytestmark = pytest.mark.spec("ARBORA-OPS-R922.2")

_PLAIN = "sk-plain-value-must-never-leave"


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from arbora.core.settings import runtime_config

    chat = [
        {
            "id": "org/model-a",
            "name": "Model A",
            "provider": "openai",
            "base_url": "http://models.invalid/v1",
            "temperature": 0.2,
            "api_key": "env://MODEL_A_KEY",
            "legacy_token": _PLAIN,
            "headers": {
                "x-trace": "on",
                "Authorization": _PLAIN,
                "x-ref": "openbao://kv/a#h",
            },
        }
    ]
    monkeypatch.setattr(runtime_config, "chat_models", json.dumps(chat), raising=False)
    monkeypatch.setattr(runtime_config, "embedding_models", [], raising=False)


async def test_get_returns_secret_references_and_never_values(configured) -> None:
    result = await models.handle_models_get(
        None,
        {"kind": "chat", "model_id": "org/model-a"},
        None,  # type: ignore[arg-type]
    )
    value = result["value"]
    assert value["kind"] == "chat"
    assert value["model"] == {
        "id": "org/model-a",
        "label": "Model A",
        "provider": "openai",
        "fields": {
            "base_url": "http://models.invalid/v1",
            "temperature": 0.2,
            "headers": {"x-trace": "on"},
        },
    }
    assert value["secret_refs"] == [
        {"name": "api_key", "ref": "env://MODEL_A_KEY"},
        {"name": "headers.x-ref", "ref": "openbao://kv/a#h"},
    ]
    assert _PLAIN not in json.dumps(result)


async def test_get_refuses_unknown_kind_and_unknown_model(configured) -> None:
    with pytest.raises(OperationRefused) as kind:
        await models.handle_models_get(
            None,
            {"kind": "image", "model_id": "x"},
            None,  # type: ignore[arg-type]
        )
    assert kind.value.code == "UNKNOWN_KIND"
    with pytest.raises(OperationRefused) as missing:
        await models.handle_models_get(
            None,
            {"kind": "chat", "model_id": "nope"},
            None,  # type: ignore[arg-type]
        )
    assert missing.value.code == "NOT_FOUND"


def test_get_is_an_admin_read_with_the_model_id_in_the_body() -> None:
    op = {spec.id: spec for spec in models.operations()}["models.get"]
    assert op.verb.value == "ask"
    assert op.effect.value == "read"
    assert op.scopes == frozenset({"ops:admin"})
    assert set(op.params.model_json_schema()["properties"]) == {"kind", "model_id"}


@pytest.mark.spec("ARBORA-OPS-R907")
async def test_non_admin_session_lists_model_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    (op,) = (op for op in models.operations() if op.id == "models.list")
    assert op.scopes == frozenset({"kg:read"})

    async def send(context, method, params, **_):
        assert (method, params["label"]) == ("GetNodesByLabel", "ModelProfile")
        return [["profile:a", {"display_name": "Model A", "provider": "openai"}]]

    monkeypatch.setattr(models, "engine_send", send)
    context = SimpleNamespace(caller=SimpleNamespace(tenant="tenant:one"))
    answer = await models.handle_models_list(context, {}, op)
    assert answer["value"]["models"] == [
        {"id": "profile:a", "display_name": "Model A", "provider": "openai"}
    ]

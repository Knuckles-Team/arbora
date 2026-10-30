"""Real-process continuation gate; opt in only with matching installed artifacts.

Only the loopback model's answers are scripted. Native ownership, planning,
claims, commits, tool invocation and both process restarts remain real.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from arbora.a2a.history_capability import EXTENSION_HEADER, HISTORY_EXTENSION
from tests._boot_model import HISTORY_FIRST, HISTORY_SECOND, _message_text
from tests.test_real_boot import _INHERITED_ENV, _booted_stack, _effect_items

pytestmark = [
    pytest.mark.real_boot,
    pytest.mark.spec("ARBORA-A2A-R001.3.3"),
    pytest.mark.skipif(
        os.getenv("ARBORA_RUN_NATIVE_HISTORY_ACCEPTANCE") != "1",
        reason="requires explicitly provisioned matching installed native artifacts",
    ),
]


def _receipts(path: Path) -> list[dict]:
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def _text(task: dict) -> str:
    return "\n".join(
        part["text"]
        for artifact in task.get("artifacts") or []
        for part in artifact.get("parts", [])
        if part.get("kind") == "text"
    )


def _call(
    stack: dict,
    method: str,
    params: dict,
    *,
    key: str,
    foreign: bool = False,
    negotiate: bool = True,
):
    return httpx.post(
        stack["base_url"] + "/a2a",
        json={"jsonrpc": "2.0", "id": key, "method": method, "params": params},
        headers={
            "Authorization": "Bearer " + stack["foreign_token" if foreign else "token"],
            "Idempotency-Key": key,
            **({EXTENSION_HEADER: HISTORY_EXTENSION} if negotiate else {}),
        },
        timeout=300,
    )


def _completed(response: httpx.Response) -> dict:
    assert response.status_code == 200, response.text
    value = response.json()["result"]
    assert value["status"]["state"] == "completed", value
    return value


def _file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _installed_inputs(client_kind: str) -> dict:
    import agent_utilities
    import epistemic_graph
    from epistemic_graph.server_binary import resolve_server_binary

    import arbora

    prefix = Path(sys.prefix).resolve()
    for module in (arbora, agent_utilities, epistemic_graph):
        assert module.__file__, "installed module must have a concrete path"
        assert prefix in Path(module.__file__).resolve().parents, (
            "qualification requires installed packages without source overlays",
            module.__file__,
        )
    binary = Path(
        resolve_server_binary()
    )  # Missing native executable FAILS, not skips.
    result: dict[str, Any] = {
        "server_sha256": _file_sha256(binary),
        "arbora_version": importlib.metadata.version("arbora"),
        "meristem_version": importlib.metadata.version("meristem"),
        "provider_module": str(Path(epistemic_graph.__file__ or "").resolve()),
    }
    names = importlib.metadata.packages_distributions().get("epistemic_graph", [])
    assert names, "native provider distribution metadata required"
    result["provider_versions"] = {
        name: importlib.metadata.version(name) for name in names
    }
    if client_kind == "tuig":
        executable = os.getenv("ARBORA_HISTORY_TUIG_BIN") or shutil.which("tuig")
        assert executable, "matching native Tuig candidate binary is required"
        result["tuig_binary"] = executable
        result["tuig_sha256"] = _file_sha256(Path(executable))
    return result


@pytest.mark.parametrize("client_kind", ["overstory-wire", "tuig"])
def test_completed_turn_survives_both_process_restarts(
    tmp_path: Path, client_kind: str
):
    inputs = _installed_inputs(client_kind)
    model_log, effect_log = tmp_path / "model.jsonl", tmp_path / "effects.jsonl"
    nonce = "proof-" + uuid.uuid4().hex
    first_prompt, second_prompt = (
        HISTORY_FIRST + " " + nonce,
        HISTORY_SECOND + " " + nonce,
    )
    label = "webui:/" + uuid.uuid4().hex
    first_key, first_message = uuid.uuid4().hex, uuid.uuid4().hex
    first_params = {
        "message": {
            "kind": "message",
            "role": "user",
            "messageId": first_message,
            "contextId": label,
            "parts": [{"kind": "text", "text": first_prompt}],
        }
    }
    with _booted_stack(
        private_root=tmp_path,
        extra_config={
            "ARBORA_COMPLETED_TEXT_HISTORY": "true",
            "ARBORA_TASK_PLAN": json.dumps(
                {
                    "policy": {"policy": "default"},
                    "max_concurrency": 1,
                    "max_steps": 8,
                }
            ),
        },
        extra_token_roles=(
            "work:read",
            "work:write",
            "work:submit",
            "work:claim",
            "query:decide",
        ),
        model_fixture_env={
            "BOOT_HISTORY_ACCEPTANCE": "1",
            "BOOT_MODEL_RECEIPTS": str(model_log),
        },
        connector_fixture_env={"BOOT_EFFECT_RECEIPTS": str(effect_log)},
    ) as stack:
        card = httpx.get(
            stack["base_url"] + "/a2a/.well-known/agent-card.json",
            headers={"Authorization": "Bearer " + stack["token"]},
            timeout=30,
        )
        assert card.status_code == 200, card.text
        root_card = httpx.get(
            stack["base_url"] + "/.well-known/agent-card.json",
            headers={"Authorization": "Bearer " + stack["token"]},
            timeout=30,
        )
        assert root_card.status_code == 200 and root_card.json() == card.json()
        assert any(
            row.get("uri") == HISTORY_EXTENSION and row.get("required") is False
            for row in card.json()["capabilities"].get("extensions", [])
        ), card.text
        first_response = _call(
            stack,
            "message/send",
            first_params,
            key=first_key,
            negotiate=client_kind == "tuig",
        )
        if client_kind == "tuig":
            assert first_response.headers.get(EXTENSION_HEADER) == HISTORY_EXTENSION
        first = _completed(first_response)
        first_reply, context = _text(first), first["contextId"]
        assert "history-first-reply" in first_reply
        assert _receipts(effect_log) == [{"tool": "echo", "text": nonce}]
        effects = asyncio.run(_effect_items(stack))
        assert len(effects) == 1 and effects[0]["status"] == "succeeded", effects
        effect_ids = [item["work_item_id"] for item in effects]
        before = len(_receipts(model_log))

        pids = stack["restart_stack"]()
        assert all(old != new for old, new in pids.values()), pids
        recovered = _completed(
            _call(stack, "tasks/get", {"id": first["id"]}, key=uuid.uuid4().hex)
        )
        assert _text(recovered) == first_reply
        replay = _completed(
            _call(
                stack,
                "message/send",
                first_params,
                key=first_key,
                negotiate=client_kind == "tuig",
            )
        )
        assert replay["id"] == first["id"] and _text(replay) == first_reply
        assert len(_receipts(model_log)) == before
        foreign = _call(
            stack, "tasks/get", {"id": first["id"]}, key=uuid.uuid4().hex, foreign=True
        )
        assert foreign.status_code != 200 or foreign.json().get("result") is None
        assert first_reply not in foreign.text
        assert len(_receipts(model_log)) == before

        if client_kind == "tuig":
            done = subprocess.run(
                [
                    inputs["tuig_binary"],
                    "--headless",
                    "--continue",
                    context,
                    "--prompt",
                    second_prompt,
                ],
                env={
                    **{
                        key: value
                        for key, value in os.environ.items()
                        if key in _INHERITED_ENV
                    },
                    "AGENT_URL": stack["base_url"],
                    "AGENT_BEARER_TOKEN": stack["token"],
                    "HOME": str(tmp_path / "tuig-home"),
                },
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            assert done.returncode == 0, (done.stdout, done.stderr, stack["logs"]())
            assert "history-second-reply" in done.stdout
        else:
            second_params = {
                "message": {
                    "kind": "message",
                    "role": "user",
                    "messageId": uuid.uuid4().hex,
                    "contextId": label,
                    "parts": [{"kind": "text", "text": second_prompt}],
                }
            }
            second_response = _call(
                stack,
                "message/send",
                second_params,
                key=uuid.uuid4().hex,
                negotiate=False,
            )
            assert EXTENSION_HEADER not in second_response.headers
            second = _completed(second_response)
            assert second["contextId"] == context and second["id"] != first["id"]
            assert "history-second-reply" in _text(second)
        execution = [
            row
            for row in _receipts(model_log)[before:]
            if row["reply"].startswith("history-second-reply")
        ]
        assert len(execution) == 1, execution
        messages = execution[0]["messages"]
        assert any(
            row.get("role") == "user" and first_prompt in _message_text(row)
            for row in messages
        )
        assert any(
            row.get("role") == "assistant" and _message_text(row) == first_reply
            for row in messages
        )
        assert _receipts(effect_log) == [{"tool": "echo", "text": nonce}]
        recovered_effects = asyncio.run(_effect_items(stack))
        assert [item["work_item_id"] for item in recovered_effects] == effect_ids
        assert all(item["status"] == "succeeded" for item in recovered_effects)
        assert recovered_effects == effects, (
            "terminal effect receipt changed across restart"
        )
        (tmp_path / "evidence.json").write_text(
            json.dumps(
                {
                    **inputs,
                    "client": client_kind,
                    "process_restarts": pids,
                    "context_id": context,
                    "first_task_id": first["id"],
                    "first_task": first,
                    "recovered_task": recovered,
                    "effect_ids": effect_ids,
                    "native_effects_before": effects,
                    "native_effects_after": recovered_effects,
                    "model_requests": len(_receipts(model_log)),
                },
                indent=2,
            )
        )

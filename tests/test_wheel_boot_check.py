"""The fresh-install boot proof calls doctor, find and fleet.status (ARBORA-HOST-R040.4)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "wheel_boot_check",
    Path(__file__).resolve().parents[1] / "scripts/wheel_boot_check.py",
)
assert _SPEC is not None and _SPEC.loader is not None
boot = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(boot)


def _answer(body: dict[str, Any], *, error: bool = False) -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(text=json.dumps(body))], is_error=error
    )


@pytest.mark.spec("ARBORA-HOST-R040.4")
def test_boot_check_calls_doctor_find_and_status() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def call(tool: str, arguments: dict[str, Any]) -> Any:
        seen.append((tool, arguments))
        return _answer({"ok": True})

    assert asyncio.run(boot.check_served_calls(call)) == []
    assert [a.get("op") or t for t, a in seen] == ["ops.doctor", "find", "fleet.status"]


@pytest.mark.spec("ARBORA-HOST-R040.4")
def test_a_refused_or_raising_boot_call_fails_the_proof_by_name() -> None:
    async def call(tool: str, arguments: dict[str, Any]) -> Any:
        if arguments.get("op") == "fleet.status":
            raise ConnectionError("down")
        if tool == "find":
            return _answer({"ok": False, "error": {"code": "UNAVAILABLE"}})
        return _answer({"ok": True})

    failures = asyncio.run(boot.check_served_calls(call))
    assert failures == ["find: UNAVAILABLE", "fleet.status: ConnectionError"]


@pytest.mark.spec("ARBORA-HOST-R040.4")
def test_served_subcommand_is_part_of_the_one_boot_gate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def served(url: str) -> list[str]:
        assert url == "http://127.0.0.1:1/mcp"
        return []

    monkeypatch.setattr(boot, "_served", served)
    assert boot.main(["served", "http://127.0.0.1:1/mcp"]) == 0
    assert "served boot calls ok: 3" in capsys.readouterr().out

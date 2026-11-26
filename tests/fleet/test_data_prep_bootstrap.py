"""Host bootstrap declares data-prep fields (ARBORA-FLEET-R058)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from arbora.core.settings import data_prep_path
from arbora.deployment.data_prep import (
    DataPrepError,
    declare_data_prep,
    load_declarations,
)

D1 = "a" * 64
D2 = "b" * 64


def _file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "data_prep.yml"
    path.write_text(text, encoding="utf-8")
    return path


class Recorded:
    def __init__(self, refusal: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.refusal = refusal

    async def __call__(self, request: dict[str, Any]) -> Any:
        self.calls.append(request)
        return {"content_digest": D1, "refusal_code": self.refusal}


@pytest.mark.spec("ARBORA-FLEET-R058")
def test_bootstrap_declares_each_capability_once(tmp_path: Path) -> None:
    path = _file(
        tmp_path,
        f"declarations:\n- {{capability: ocr, field_refs: [a, b], policy_digest: {D1}}}\n"
        f"- {{capability: pii, field_refs: [c], policy_digest: {D2}}}\n",
    )
    send = Recorded()
    done = asyncio.run(declare_data_prep(send, tenant="t:1", path=path))
    assert done == ["ocr", "pii"]
    assert [c["payload"]["capability"] for c in send.calls] == ["ocr", "pii"]
    assert send.calls[0] == {
        "payload": {"capability": "ocr", "field_refs": ["a", "b"], "policy_digest": D1},
        "tenant": "t:1",
    }


@pytest.mark.spec("ARBORA-FLEET-R058")
@pytest.mark.parametrize(
    "body",
    [
        "declarations:\n- {capability: x, field_refs: [a], policy_digest: BAD}\n",
        f"declarations:\n- {{capability: x, field_refs: [a], policy_digest: {D1}, import_path: os.system}}\n",
        f"declarations:\n- {{capability: x, field_refs: [a], policy_digest: {D1}}}\n"
        f"- {{capability: x, field_refs: [a], policy_digest: {D1}}}\n",
        "nothing: here\n",
    ],
)
def test_malformed_file_declares_nothing(tmp_path: Path, body: str) -> None:
    send = Recorded()
    with pytest.raises(DataPrepError):
        asyncio.run(declare_data_prep(send, tenant="t", path=_file(tmp_path, body)))
    assert send.calls == []


@pytest.mark.spec("ARBORA-FLEET-R058")
def test_absent_file_declares_nothing_and_engine_refusal_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARBORA_CONFIG_DIR", str(tmp_path))
    assert data_prep_path() == tmp_path / "data_prep.yml"
    assert load_declarations() == []
    path = _file(
        tmp_path,
        f"declarations:\n- {{capability: x, field_refs: [a], policy_digest: {D1}}}\n",
    )
    with pytest.raises(DataPrepError, match="POLICY_DIGEST_UNKNOWN"):
        asyncio.run(
            declare_data_prep(Recorded("POLICY_DIGEST_UNKNOWN"), tenant="t", path=path)
        )

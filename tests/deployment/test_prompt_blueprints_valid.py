"""Every prompt Arbora serves is a canonical prompt blueprint (ARBORA-DEPLOY-R020.2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from scion_sdk.artifacts.prompt_blueprint import (
    BLUEPRINT_SCHEMA_VERSION,
    blueprint_from_source,
    validate_prompt_blueprint,
)

import arbora

PROMPTS = Path(arbora.__file__).resolve().parent / "prompts"


@pytest.mark.spec("ARBORA-DEPLOY-R020.2")
def test_every_prompt_passes_the_pinned_engine_blueprint() -> None:
    files = sorted(PROMPTS.glob("*.json"))
    assert files, "Arbora declares no prompts"
    names = {path.stem for path in files}
    existing = {f"prompt://arbora/{name}" for name in names}
    failures: dict[str, list[str]] = {}
    for path in files:
        try:
            document = blueprint_from_source(
                path.stem, path, connector="arbora", existing=existing
            )
        except Exception as exc:
            failures[path.name] = [str(exc)]
            continue
        found = validate_prompt_blueprint(document, existing)
        if found:
            failures[path.name] = [f"{v.path} ({v.rule})" for v in found]
        else:
            assert document["schema_version"] == BLUEPRINT_SCHEMA_VERSION
            assert document["task"] == path.stem
            assert document["type"] == "prompt"
    assert not failures, failures

"""Security evaluation models validate and close the verdict set (ARBORA-OPS-R051.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arbora.api.ops.security_eval import (
    CatalogEntry,
    EvaluationCatalog,
    EvaluationGoal,
    EvaluationScope,
    Finding,
)


@pytest.mark.spec("ARBORA-OPS-R051.1")
def test_models_validate_and_close_verdict_set() -> None:
    assert EvaluationGoal(text="x" * 4096).text
    for bad in ("", "x" * 4097):
        with pytest.raises(ValidationError):
            EvaluationGoal(text=bad)
    catalog = EvaluationCatalog(entries=(CatalogEntry(entry_id="a"),))
    assert catalog.entries[0].entry_id == "a"
    with pytest.raises(ValidationError):
        EvaluationScope(target_ids=(), max_probes=1)
    with pytest.raises(ValidationError):
        EvaluationScope(target_ids=("t",), max_probes=0)
    for verdict in ("resisted", "compromised", "inconclusive"):
        assert Finding(probe_id="p", category="c", verdict=verdict, evidence_ref="e")
    with pytest.raises(ValidationError):
        Finding(probe_id="p", category="c", verdict="breached", evidence_ref="e")
    with pytest.raises(ValidationError):
        Finding(probe_id="p", category="c", verdict="resisted", evidence_ref="e", x=1)
    finding = Finding(probe_id="p", category="c", verdict="resisted", evidence_ref="e")
    with pytest.raises(ValidationError):
        finding.verdict = "compromised"  # type: ignore[misc]

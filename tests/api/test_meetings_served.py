"""Meeting transcript, provider and summary models (ARBORA-OPS-R065.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arbora.api.ops.meetings import (
    ProviderChoice,
    SummaryRequest,
    SummaryResult,
    TranscriptEvent,
)

EVENT = {
    "schema_version": 1,
    "meeting_id": "m1",
    "segment_id": "s1",
    "sequence": 4,
    "correction_revision": 1,
    "state": "final",
    "start_ms": 1000,
    "end_ms": 2500,
    "text": "agreed",
    "source_id": "mic-1",
    "model_id": "speech-small",
}


@pytest.mark.spec("ARBORA-OPS-R065.1")
def test_models_round_trip_vectors() -> None:
    models = {
        TranscriptEvent: EVENT,
        ProviderChoice: {
            "provider_id": "p1",
            "execution_class": "hosted",
            "model_id": "m",
        },
        SummaryRequest: {
            "schema_version": 1,
            "meeting_id": "m1",
            "transcript_revision": 9,
            "provider_id": "p1",
            "context_revision": "ctx-3",
        },
        SummaryResult: {
            "schema_version": 1,
            "summary_id": "sum-1",
            "text": "decided",
            "provenance": {"provider": "p1", "model": "m"},
            "state": "complete",
        },
    }
    for model, vector in models.items():
        assert model.model_validate(vector).model_dump() == vector
    for state in ("partial", "final", "finalized"):
        assert TranscriptEvent.model_validate({**EVENT, "state": state}).state == state


@pytest.mark.spec("ARBORA-OPS-R065.1")
def test_closed_values_and_extra_fields_are_refused() -> None:
    with pytest.raises(ValidationError):
        TranscriptEvent.model_validate({**EVENT, "state": "draft"})
    with pytest.raises(ValidationError):
        TranscriptEvent.model_validate({**EVENT, "extra": 1})
    with pytest.raises(ValidationError):
        ProviderChoice.model_validate(
            {"provider_id": "p", "execution_class": "cloud", "model_id": "m"}
        )
    with pytest.raises(ValidationError):
        TranscriptEvent.model_validate({**EVENT, "sequence": -1})

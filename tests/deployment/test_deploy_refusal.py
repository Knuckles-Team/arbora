"""Three missing producers give three distinct refusals, none with an effect."""

from __future__ import annotations

import pytest

from arbora.api.domain.deployment.assembly import refuse_unavailable

pytestmark = pytest.mark.spec("ARBORA-DEPLOY-R017.5")


def test_three_unavailable_causes_give_distinct_refusals_with_no_effect() -> None:
    codes = {}
    for cause in ("decide", "placement_kind", "confirm"):
        refusal = refuse_unavailable(cause)
        assert refusal.code == "UNAVAILABLE"
        codes[cause] = refusal.details["code"]
    assert codes == {
        "decide": "ENGINE_DECIDE_UNAVAILABLE",
        "placement_kind": "PLACEMENT_KIND_UNAVAILABLE",
        "confirm": "CONFIRM_UNAVAILABLE",
    }
    assert len(set(codes.values())) == 3
    with pytest.raises(ValueError):
        refuse_unavailable("other")  # type: ignore[arg-type]

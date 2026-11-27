"""``finance.guide.get`` projects the skill revision header (ARBORA-DATA-MARKET-R003.15)."""

# ruff: noqa: F811
# The served fixtures are imported for pytest and re-requested by name, the repo idiom.

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from arbora.api.ops import finance
from tests.api._market_support import MarketServed, captured  # noqa: F401
from tests.test_served_operation_api import _value, served  # noqa: F401

SKILL = """---
name: emerald-exchange-leveraged-trading
revision: "7"
reviewed_at: 2026-11-20
reviewer: risk-desk
source_cards:
  - card-a
  - card-b
---
# Leveraged trading
Body text.
"""


def _venue(monkeypatch: pytest.MonkeyPatch, text: Any) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def read(_context: Any, op_id: str, arguments: Any, **_k: Any) -> Any:
        assert op_id == "fleet.resources.read"
        seen.append(dict(arguments))
        return {"contents": [{"uri": arguments["uri"], "text": text}]}

    monkeypatch.setattr(finance, "invoke_registered", read)
    return seen


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.15")
def test_projection_matches_skill_revision_header(
    captured: Any,
    served: Any,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    seen = _venue(monkeypatch, SKILL)
    market = MarketServed(captured, served, monkeypatch, {})
    expected = {
        "guide_id": "emerald-exchange-leveraged-trading",
        "revision": "7",
        "reviewed_at": "2026-11-20",
        "reviewer": "risk-desk",
        "source_cards": ["card-a", "card-b"],
        "body_sha256": hashlib.sha256(SKILL.encode()).hexdigest(),
    }
    assert _value(market.http("finance.guide.get", {})) == expected
    answer = market.mcp("finance.guide.get", {})
    assert answer["ok"] is True and answer["result"] == expected
    assert seen[0] == {
        "server": "emerald-exchange",
        "uri": "skill://emerald-exchange-leveraged-trading/SKILL.md",
    }
    assert market.http("finance.guide.get", {"revision": "9"}).status_code == 400


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.15")
def test_a_missing_or_unparsable_revision_header_is_unavailable(
    captured: Any,
    served: Any,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    market = MarketServed(captured, served, monkeypatch, {})
    for text in (
        "# no header\n",
        "---\nname: x\n---\nbody",
        "---\nname: x\nrevision: 1\nreviewed_at: soon\nreviewer: r\nsource_cards: []\n---\n",
        "---\n: [bad\n---\n",
        None,
    ):
        _venue(monkeypatch, text)
        refused = market.http("finance.guide.get", {})
        assert refused.status_code == 503, text
        assert refused.json()["error"]["code"] == "UNAVAILABLE"

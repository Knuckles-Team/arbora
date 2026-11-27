"""ARBORA-FLEET-R083.4: one scoring floor; no other function scores or ranks."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "arbora"
SCANNED = ("fleet", "api/mcp", "mcp_server/backing")
FLOOR = {"lexical_floor.py", "ranking.py"}
#: Names that match the pattern but order nothing by relevance (ports, evidence, renames).
SCORING = re.compile(r"score|rank|relevance", re.IGNORECASE)


def _offenders() -> list[str]:
    found = []
    for directory in SCANNED:
        for path in sorted((ROOT / directory).rglob("*.py")):
            if path.name in FLOOR:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and SCORING.search(node.name):
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno} {node.name}")
    return found


@pytest.mark.spec("ARBORA-FLEET-R083.4")
def test_no_scoring_function_exists_outside_the_floor() -> None:
    assert _offenders() == []


@pytest.mark.spec("ARBORA-FLEET-R083.4")
def test_the_retired_scorers_are_gone() -> None:
    from arbora.fleet import multiplexer
    from arbora.mcp_server.backing.tools import intent_tools

    assert not hasattr(multiplexer, "_cosine")
    assert not hasattr(multiplexer.MCPMultiplexer, "_lexical_score")
    assert not hasattr(multiplexer.MCPMultiplexer, "_relevance")
    assert not hasattr(intent_tools, "_score")
    assert not (ROOT / "fleet" / "discovery_ranking.py").exists()

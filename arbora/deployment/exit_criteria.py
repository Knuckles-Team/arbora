"""Exit-criteria matrix registry and audit (ARBORA-RELEASE-R003.1).

ARBORA-RELEASE-R003 requires an exit-criteria matrix mapping each
release-readiness obligation to an executable test, and a test-suite audit
confirming every row resolves to a *passing* automated test. The seven
obligation rows named in ``specs/dependency-ordered-release/requirements.md``
span multiple repositories (ingestion receipts and SPARQL/natural-language
query proofs live in medulla; the multi-agent orchestration graph in
meristem; connector certification in scion-sdk; and so on),
so wiring every row to a currently-passing test is cross-repo work tracked
separately as ``ARBORA-RELEASE-R003.2`` and is not claimed here.

This module delivers the arbora side available against the currently
pinned medulla: the typed matrix registry and the audit function a
test-suite audit runs against pytest's own collected node IDs. Rows are
registered by callers (this module ships no fabricated rows); an unregistered
obligation audits as missing, never as a silent pass.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET  # noqa: S405  # nosec B405 - a local pytest report
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from arbora.deployment.release_candidate import ExitCriterionRow


class ExitCriteriaAudit(BaseModel):
    """Per-row audit outcome for a matrix against a set of collected test IDs."""

    model_config = ConfigDict(frozen=True)

    satisfied: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def all_satisfied(self) -> bool:
        return not self.missing


def audit_exit_criteria(
    matrix: Iterable[ExitCriterionRow],
    collected_test_ids: Iterable[str],
) -> ExitCriteriaAudit:
    """Report which matrix rows resolve to a test present in ``collected_test_ids``.

    ``collected_test_ids`` is the passing-test universe a test-suite audit
    observed (for example pytest's collected/passed node IDs); a row whose
    ``test_reference`` is absent from that set audits as missing, never as a
    best-effort pass.
    """
    available = set(collected_test_ids)
    satisfied: list[str] = []
    missing: list[str] = []
    for criterion in matrix:
        if criterion.test_reference in available:
            satisfied.append(criterion.obligation_id)
        else:
            missing.append(criterion.obligation_id)
    return ExitCriteriaAudit(satisfied=tuple(satisfied), missing=tuple(missing))


def passed_node_ids_from_junit(path: Path) -> frozenset[str]:
    """The node ids of passed cases in a pytest report written with ``xunit1``.

    A passed case yields ``<file>::<class>::<name>`` or ``<file>::<name>`` when
    it has no class. Failed, errored and skipped cases yield no id
    (ARBORA-RELEASE-R003.10).
    """
    passed: set[str] = set()
    for case in ET.parse(path).getroot().iter("testcase"):  # noqa: S314  # nosec B314
        if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
            continue
        classname, name = case.get("classname", ""), case.get("name", "")
        parts = classname.split(".")
        # ``tests.api.test_x.TestClass`` -> file ``tests/api/test_x.py`` and a class.
        index = next(
            (i for i, part in enumerate(parts) if part.startswith("test_")), None
        )
        if index is None:
            continue
        file = "/".join(parts[: index + 1]) + ".py"
        owner = parts[index + 1 :]
        passed.add("::".join([file, *owner, name]))
    return frozenset(passed)

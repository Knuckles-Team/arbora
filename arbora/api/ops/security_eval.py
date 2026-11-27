"""Scoped security evaluation (ARBORA-OPS-R051).

``security.evaluate`` sequences three owners and decides nothing itself. The engine
selects the attacks and transforms for the goal (``Decide`` ``probe_selection``) and
judges each probe's scorer results (``Decide`` ``probe_verdict``). The harness runs
the selected probes against the caller's authorized targets through the
``SecurityProbeRunner`` port. Arbora checks that every target is in the caller's
verified scope before any probe starts, applies the probe cap, and returns the
findings. It composes and scores nothing locally and keeps no probe payload.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field, ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _common
from arbora.api.ops._question import (
    FamilyRefusal,
    advisory_scores,
    chosen_option,
    decide_question,
    q32_permille,
    question_request,
)
from arbora.api.registry import Confirm, OpSpec

SCOPE = "agent:decision-evaluate"
#: A caller may evaluate a target only when it holds ``security:target:<id>``.
TARGET_SCOPE_PREFIX = "security:target:"
#: The probe cap when the engine decision carries none (``knob red_team.max_probes``).
DEFAULT_MAX_PROBES = 50
_NO_ENTRY = "NO_CATALOG_ENTRY_SELECTED"
_LIBRARY_KINDS = ("red_team_attack", "red_team_transform")
Verdict = Literal["resisted", "compromised", "inconclusive"]


class EvaluationGoal(_common.FrozenState):
    text: str = Field(min_length=1, max_length=4096)


class CatalogEntry(_common.FrozenState):
    entry_id: str = Field(min_length=1, max_length=256)


class EvaluationCatalog(_common.FrozenState):
    entries: tuple[CatalogEntry, ...] = ()


class EvaluationScope(_common.FrozenState):
    target_ids: tuple[str, ...] = Field(min_length=1, max_length=16)
    max_probes: int = Field(ge=1, le=10_000)


class Finding(_common.FrozenState):
    probe_id: str = Field(min_length=1, max_length=256)
    category: str = Field(min_length=1, max_length=256)
    verdict: Verdict
    evidence_ref: str = Field(min_length=1, max_length=512)


class ScorerScore(_common.FrozenState):
    scorer_id: str = Field(min_length=1, max_length=256)
    #: Per mille, 0 to 1000; absent when the scorer produced no score.
    permille: int | None = Field(default=None, ge=0, le=1000)


class ProbeRun(_common.FrozenState):
    """One executed probe as the harness reports it: ids and scores, never payloads."""

    probe_id: str = Field(min_length=1, max_length=256)
    attack_id: str = Field(min_length=1, max_length=256)
    transform_ids: tuple[str, ...] = ()
    category: str = Field(min_length=1, max_length=256)
    evidence_ref: str = Field(min_length=1, max_length=512)
    scores: tuple[ScorerScore, ...] = ()


class SecurityEvaluateParams(_common.Params):
    goal: str = Field(min_length=1, max_length=4096)
    target_ids: list[str] = Field(min_length=1, max_length=16)
    max_probes: int | None = Field(default=None, ge=1, le=10_000)
    target_categories: list[str] = Field(default_factory=list, max_length=64)


class SecurityEvaluateResult(_common.Params):
    value: dict[str, Any]


@runtime_checkable
class SecurityEvaluationPort(Protocol):
    """What the engine decides: catalog selection, probe verdicts and the probe cap."""

    async def select(
        self, context: Any, goal: EvaluationGoal, categories: Sequence[str]
    ) -> EvaluationCatalog: ...

    async def verdict(self, context: Any, probe: ProbeRun) -> Verdict: ...

    async def probe_cap(self, context: Any) -> int | None: ...


@runtime_checkable
class SecurityProbeRunner(Protocol):
    """What the harness executes: the selected entries against one authorized target."""

    async def run(
        self, context: Any, entries: EvaluationCatalog, target_id: str
    ) -> tuple[ProbeRun, ...]: ...


class EngineSecurityEvaluation:
    """The engine ``Decide`` questions of catalog selection and probe verdicts."""

    async def _schema(self, context: Any) -> Mapping[str, Any]:
        fixed = context.services.get("security_feature_schema")
        if isinstance(fixed, Mapping):
            return fixed
        from arbora.api.ops.plans import _feature_schema

        return await _feature_schema(context, str(context.caller.tenant))

    async def select(
        self, context: Any, goal: EvaluationGoal, categories: Sequence[str]
    ) -> EvaluationCatalog:
        from medulla import decision_client as dc

        params = [dc.param("goal_text", "text", goal.text)]
        if categories:
            params.append(dc.param("target_categories", "iri_list", list(categories)))
        request = question_request(
            str(context.caller.tenant),
            "security.evaluate.select",
            "probe_selection",
            dc.library(_LIBRARY_KINDS),
            await self._schema(context),
            params,
        )
        try:
            record = await decide_question(
                context, "probe_selection", request, family_codes=frozenset({_NO_ENTRY})
            )
        except FamilyRefusal:
            return EvaluationCatalog()
        return EvaluationCatalog(
            entries=tuple(
                CatalogEntry(entry_id=name) for name, _ in advisory_scores(record)
            )
        )

    async def verdict(self, context: Any, probe: ProbeRun) -> Verdict:
        from medulla import decision_client as dc

        options = [
            dc.DeclaredOption(
                score.scorer_id,
                numbers={}
                if score.permille is None
                else {"score_permille": q32_permille(score.permille)},
            )
            for score in probe.scores
        ]
        request = question_request(
            str(context.caller.tenant),
            f"security.evaluate.verdict:{probe.probe_id}",
            "probe_verdict",
            dc.declared(options),
            await self._schema(context),
            [
                dc.param("attack_id", "iri", probe.attack_id),
                dc.param("transform_ids", "iri_list", list(probe.transform_ids)),
            ],
        )
        record = await decide_question(context, "probe_verdict", request)
        outcome = record.get("outcome")
        if isinstance(outcome, Mapping) and outcome.get("outcome") == "abstained":
            # The engine named no scorer for the category: nothing was judged.
            return "inconclusive"
        chosen = chosen_option(record)
        if chosen not in ("resisted", "compromised", "inconclusive"):
            raise OperationRefused("RECEIPT_MISMATCH", {"reason": "unknown verdict"})
        return chosen  # type: ignore[return-value]

    async def probe_cap(self, context: Any) -> int | None:
        """No engine method publishes ``red_team.max_probes`` yet; the default applies."""
        return None


def _authorize_targets(context: Any, target_ids: Sequence[str]) -> None:
    held = frozenset(getattr(context.caller, "effective_scopes", ()) or ())
    if any(f"{TARGET_SCOPE_PREFIX}{target}" not in held for target in target_ids):
        raise OperationRefused("SUBJECT_ACCESS_DENIED")


async def _limit(
    context: Any, evaluation: SecurityEvaluationPort, requested: int | None
) -> int:
    cap = await evaluation.probe_cap(context) or DEFAULT_MAX_PROBES
    if requested is not None and requested > cap:
        raise OperationRefused("LOAD_CAP_EXCEEDED", {"loaded_items": requested})
    return requested or cap


async def _findings(
    context: Any,
    evaluation: SecurityEvaluationPort,
    runner: SecurityProbeRunner,
    entries: EvaluationCatalog,
    target_ids: Sequence[str],
) -> list[Finding]:
    findings: list[Finding] = []
    for target in target_ids:
        for probe in await runner.run(context, entries, target):
            findings.append(
                Finding(
                    probe_id=probe.probe_id,
                    category=probe.category,
                    verdict=await evaluation.verdict(context, probe),
                    evidence_ref=probe.evidence_ref,
                )
            )
    return findings


async def handle_security_evaluate(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Select, run and judge probes for the caller's authorized targets."""
    evaluation: SecurityEvaluationPort = _common.bound_service(
        context, "security_evaluation", reason="security evaluation is not composed"
    )
    runner: SecurityProbeRunner = _common.bound_service(
        context, "security_probe_runner", reason="probe runner is not composed"
    )
    try:
        goal = EvaluationGoal(text=params["goal"])
    except ValidationError as invalid:
        raise OperationRefused("INVALID_ARGUMENT", {"field": "goal"}) from invalid
    targets = tuple(params["target_ids"])
    _authorize_targets(context, targets)
    limit = await _limit(context, evaluation, params.get("max_probes"))
    selected = await evaluation.select(
        context, goal, tuple(params.get("target_categories") or ())
    )
    entries = EvaluationCatalog(entries=selected.entries[:limit])
    if not entries.entries:
        raise OperationRefused("INVALID_ARGUMENT", {"reason": "no catalog entry"})
    findings = await _findings(context, evaluation, runner, entries, targets)
    return {
        "value": {
            "entry_ids": [entry.entry_id for entry in entries.entries],
            "findings": [finding.model_dump(mode="json") for finding in findings],
        }
    }


def operations() -> tuple[OpSpec, ...]:
    return (
        _common.build_read_op(
            op_id="security.evaluate",
            summary="Evaluate authorized targets with engine-selected security probes",
            examples=("run a security evaluation against this target",),
            params=SecurityEvaluateParams,
            result=SecurityEvaluateResult,
            handler="arbora.api.ops.security_eval.handle_security_evaluate",
            scope=SCOPE,
            confirm=Confirm.PLAN,
        ),
    )


specs = operations

__all__ = [
    "DEFAULT_MAX_PROBES",
    "CatalogEntry",
    "EngineSecurityEvaluation",
    "EvaluationCatalog",
    "EvaluationGoal",
    "EvaluationScope",
    "Finding",
    "ProbeRun",
    "ScorerScore",
    "SecurityEvaluateParams",
    "SecurityEvaluationPort",
    "SecurityProbeRunner",
    "handle_security_evaluate",
    "operations",
    "specs",
]

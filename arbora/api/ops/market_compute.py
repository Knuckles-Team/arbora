"""Served market calculations over the engine's finance methods (ARBORA-DATA-MARKET).

Every figure is the engine's. Each operation validates its request against the
engine's own generated request shape, sends one method as the verified caller and
returns the answer unchanged. Arbora holds no formula and adds no default: an
omitted optional stays omitted, so the engine applies its own knobs. Engine
refusal codes (``SIZING_INPUT_INVALID``, ``KERNEL_*``, ``PATTERN_*`` and the
rest) reach the caller as the engine named them.

Rows: R003.8 signal operations, R003.9 ``market.size``, R003.10 ``market.evaluate``,
R003.12 research kernels and R003.14 banking operations.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from medulla.generated.models import NumericEvaluateOp
from pydantic import Field, RootModel, StrictInt, TypeAdapter, ValidationError

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops._engine import engine_send
from arbora.api.ops._nested import invoke_registered
from arbora.api.registry import OpSpec
from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_models

READ_SCOPE = "finance:read"
#: The venue connector and its strategy tool, reached through the governed fleet call.
VENUE_SERVER = "emerald-exchange"
STRATEGY_TOOL = "emerald_strategy"
#: The most numbers one request may carry; the engine applies its own ceilings too.
MAX_VALUES = 100_000
_SERIES = Field(min_length=1, max_length=MAX_VALUES)
_ID = Field(min_length=1, max_length=256)


class RecommendationParams(Params):
    listing_id: str = Field(min_length=1, max_length=128)
    strategy_id: str | None = Field(default=None, min_length=1, max_length=128)


class EvidenceRef(Params):
    ref: str = Field(min_length=1, max_length=512)
    digest: str = Field(pattern=r"^(sha256:)?[0-9a-f]{64}$")
    as_of: str


class RecommendationResult(Params):
    """The recommendation as the strategy tool gave it; it has no order field."""

    recommendation: Literal["accumulate", "hold", "de-risk", "abstain"]
    strategy_version: str | None
    evidence: list[EvidenceRef]
    abstain_reason: str | None
    scorecard_ref: str | None


class ComputeResult(Params):
    value: Any


class SignalRequestParams(Params):
    """One signal request in the engine's own request shape."""

    request: dict[str, Any]


class BacktestRunParams(Params):
    draft: dict[str, Any]


class MarketSizeParams(Params):
    forecast_digest: str = _ID
    policy: dict[str, Any]
    request: dict[str, Any]


class MarketEvaluateParams(RootModel[NumericEvaluateOp]):
    """``{op, ...kernel fields}`` in the engine's own closed ``NumericEvaluateOp`` shape."""


class PatternClassifyParams(Params):
    open: list[float] = _SERIES
    high: list[float] = _SERIES
    low: list[float] = _SERIES
    close: list[float] = _SERIES


class AttributionGroup(Params):
    group: str = Field(min_length=1, max_length=256)
    r_b: float
    r_p: float
    w_b: float
    w_p: float


class ProfitAttributionParams(Params):
    groups: list[AttributionGroup] = Field(min_length=1, max_length=10_000)


class AmlScreenParams(Params):
    amount_minor: StrictInt
    rule_id: str = _ID
    threshold_minor: StrictInt
    version: StrictInt = Field(ge=0)


class CapitalRatiosParams(Params):
    at1: StrictInt
    cet1: StrictInt
    risk_weighted_assets: StrictInt
    tier2: StrictInt
    total_exposure: StrictInt


class ExpectedLossParams(Params):
    ead_minor: StrictInt
    lgd: float
    pd: float


class SettlementChainParams(Params):
    amount_minor: StrictInt
    beneficiary: str = _ID
    correspondents: list[str] = Field(max_length=64)
    origin: str = _ID


def _validated(model: str, payload: Any) -> None:
    """Check ``payload`` against the engine's request shape; refuse with no cell contents."""
    from medulla.generated import models

    try:
        require_models((model,))
    except PinLagUnavailable as missing:
        raise refusal(missing) from missing
    try:
        TypeAdapter(getattr(models, model)).validate_python(payload)
    except ValidationError as error:
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in error.errors()})
        raise OperationRefused(
            "INVALID_ARGUMENT", {"model": model, "fields": fields[:20]}
        ) from None


def _ceiling(value: Any, budget: list[int]) -> None:
    """Refuse a request that carries more than ``MAX_VALUES`` numbers in all."""
    if isinstance(value, Mapping):
        for item in value.values():
            _ceiling(item, budget)
    elif isinstance(value, (list, tuple)):
        budget[0] -= len(value)
        if budget[0] < 0:
            raise OperationRefused("INVALID_ARGUMENT", {"reason": "request too large"})
        for item in value:
            _ceiling(item, budget)


def _tool_value(reply: Any) -> Any:
    """The strategy tool's answer as plain data; a refusal it names is raised."""
    value = reply.get("value", reply) if isinstance(reply, Mapping) else reply
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    if isinstance(value, Mapping) and isinstance(value.get("error"), str):
        named = value["error"]
        if named == "STRATEGY_NOT_MAPPED":
            raise EngineRefusal(named)
        if named == "POLICY_DENIED":
            raise OperationRefused(named)
        raise OperationRefused("UNAVAILABLE", {"reason": "the strategy tool refused"})
    if not isinstance(value, Mapping):
        raise OperationRefused("UNAVAILABLE", {"reason": "strategy result malformed"})
    return value


def _grounded(answer: RecommendationResult) -> bool:
    """An abstain gives its reason; any other call cites a version and evidence."""
    if answer.recommendation == "abstain":
        return bool(answer.abstain_reason)
    return bool(answer.strategy_version) and bool(answer.evidence)


async def handle_market_recommendation_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One strategy recommendation from the venue's strategy tool, evidence cited or abstained.

    The tool's answer passes through its closed projection: an order field is
    dropped, and a recommendation with no strategy version and evidence, or an
    abstain with no reason, is not served.
    """
    arguments: dict[str, Any] = {
        "action": "recommend",
        "listing_id": params["listing_id"],
    }
    if params.get("strategy_id") is not None:
        arguments["strategy_id"] = params["strategy_id"]
    reply = await invoke_registered(
        context,
        "fleet.call",
        {"server": VENUE_SERVER, "tool": STRATEGY_TOOL, "arguments": arguments},
    )
    value = _tool_value(reply)
    keys = set(RecommendationResult.model_fields)
    try:
        answer = RecommendationResult.model_validate(
            {key: value[key] for key in keys if key in value}
        )
    except ValidationError:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "strategy result malformed"}
        ) from None
    if not _grounded(answer):
        raise OperationRefused("UNAVAILABLE", {"reason": "recommendation not grounded"})
    return answer.model_dump()


async def _send(context: Any, method: str, wire: dict[str, Any]) -> dict[str, Any]:
    return {"value": await engine_send(context, method, wire)}


async def _market(context: Any, op: str, key: str, model: str, body: Any) -> Any:
    _validated(model, body)
    return await _send(context, "FinanceMarket", {"op": {"op": op, key: body}})


async def handle_market_signal_replay(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _market(
        context, "signal_replay", "request", "SignalReplayRequest", params["request"]
    )


async def handle_market_signal_scan(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _market(
        context, "signal_scan", "request", "ScanRequest", params["request"]
    )


async def handle_market_flip_confidence(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _market(
        context,
        "flip_confidence",
        "request",
        "FlipConfidenceRequest",
        params["request"],
    )


async def handle_market_backtest_run(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _market(
        context, "backtest_run", "draft", "BacktestRunDraft", params["draft"]
    )


async def handle_market_size(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One regime-sizing answer; a ``SIZING_ABSTAIN`` refusal passes through."""
    _validated("RegimeSizingPolicy", params["policy"])
    _validated("RegimeForecastRequest", params["request"])
    return await _send(
        context,
        "RegimeSizingInput",
        {
            "forecast_digest": params["forecast_digest"],
            "policy": params["policy"],
            "request": params["request"],
        },
    )


async def handle_market_evaluate(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One numeric kernel; the kernel fields pass through unchanged."""
    kernel = dict(params)
    _ceiling(kernel, [MAX_VALUES])
    return await _send(context, "NumericEvaluate", {"op": kernel})


async def handle_market_pattern_classify(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _send(context, "FinancePatternClassify", dict(params))


async def handle_market_profit_attribution(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _send(context, "FinanceProfitAttribution", dict(params))


async def handle_market_aml_screen(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _send(context, "FinanceAmlScreen", dict(params))


async def handle_market_capital_ratios(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _send(context, "FinanceCapitalRatios", dict(params))


async def handle_market_expected_loss(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _send(context, "FinanceExpectedLoss", dict(params))


async def handle_market_settlement_chain(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _send(context, "FinanceSettlementChain", dict(params))


#: ``(op id, params model, summary, example)``; the handler is ``handle_<op id>``.
_OPS: tuple[tuple[str, type[Params], str, str], ...] = (
    (
        "market.signal_replay",
        SignalRequestParams,
        "Replay a signal over recorded bars",
        "replay this signal over the stored bars",
    ),
    (
        "market.signal_scan",
        SignalRequestParams,
        "Scan instruments for signal flips",
        "scan these instruments for trend flips",
    ),
    (
        "market.flip_confidence",
        SignalRequestParams,
        "Score the confidence of a signal flip",
        "how confident is this trend flip",
    ),
    (
        "market.backtest_run",
        BacktestRunParams,
        "Run one backtest and return its digest",
        "backtest this strategy draft",
    ),
    (
        "market.size",
        MarketSizeParams,
        "Size a position from a regime forecast",
        "size this position from the regime forecast",
    ),
    (
        "market.evaluate",
        MarketEvaluateParams,
        "Evaluate one numeric kernel such as Brier or ADF",
        "compute the Brier score of these forecasts",
    ),
    (
        "market.pattern_classify",
        PatternClassifyParams,
        "Classify candlestick patterns in an OHLC window",
        "classify the candlestick patterns in this window",
    ),
    (
        "market.profit_attribution",
        ProfitAttributionParams,
        "Attribute portfolio profit across groups",
        "attribute this portfolio's profit to its groups",
    ),
    (
        "market.aml_screen",
        AmlScreenParams,
        "Screen an amount in minor units against an AML rule",
        "screen this payment against the AML rule",
    ),
    (
        "market.capital_ratios",
        CapitalRatiosParams,
        "Compute capital ratios from minor-unit balances",
        "compute the capital ratios of this balance sheet",
    ),
    (
        "market.expected_loss",
        ExpectedLossParams,
        "Compute the expected loss of a credit exposure",
        "what is the expected loss of this exposure",
    ),
    (
        "market.settlement_chain",
        SettlementChainParams,
        "Resolve the settlement chain of a payment",
        "resolve the settlement chain for this payment",
    ),
)


def operations() -> tuple[OpSpec, ...]:
    return (
        *(
            build_read_op(
                op_id=op_id,
                summary=summary,
                examples=(example,),
                params=params,
                result=ComputeResult,
                handler=f"arbora.api.ops.market_compute.handle_{op_id.replace('.', '_')}",
                scope=READ_SCOPE,
            )
            for op_id, params, summary, example in _OPS
        ),
        build_read_op(
            op_id="market.recommendation.get",
            summary="Read the strategy recommendation for a listing, with evidence or an abstain reason",
            examples=("what does the strategy recommend for this listing",),
            params=RecommendationParams,
            result=RecommendationResult,
            handler="arbora.api.ops.market_compute.handle_market_recommendation_get",
            scope=READ_SCOPE,
        ),
    )


specs = operations

__all__ = [
    "MAX_VALUES",
    "operations",
    "specs",
]

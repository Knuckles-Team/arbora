"""``market.forecast.sample`` stores a probabilistic record behind pin-lag (ARBORA-DATA-MARKET-R008)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import market_detail as detail
from arbora.pin_lag import scion_sdk as pin

NO_OP: Any = None
PARAMS = {
    "series_ids": ["s1"],
    "model_revision": "m1",
    "temperature": 0.5,
    "top_p": 0.9,
    "sample_count": 8,
    "seed": 7,
    "source_id": "forecast-1",
}


class Refused(Exception):
    def __init__(self, code: str) -> None:
        self.code = SimpleNamespace(value=code)


def structural_sdk() -> Any:
    """The adapter shapes asked of scion-sdk (``forecast_provider``, ``forecast_ingest_request``)."""

    class Request(SimpleNamespace):
        pass

    async def request_forecast(provider: Any, request: Any) -> Any:
        return await provider(request)

    def forecast_provider(call: Any, tool: str) -> Any:
        async def provider(request: Any) -> Any:
            await call(tool, {"series_ids": list(request.series_ids)})
            return SimpleNamespace(
                per_series=(("s1", [1, 2]),), seed=request.seed, model_revision="m1"
            )

        return provider

    def ingest(response: Any, *, tenant: str, source_id: str) -> dict[str, Any]:
        return {
            "tenant": tenant,
            "source_id": source_id,
            "probabilistic": True,
            "seed": response.seed,
        }

    return SimpleNamespace(
        ForecastRequest=lambda **kw: Request(**kw),
        ForecastError=Refused,
        request_forecast=request_forecast,
        forecast_provider=forecast_provider,
        forecast_ingest_request=ingest,
    )


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R008")
async def test_sample_is_stored_with_probabilistic_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, dict[str, Any]]] = []
    called: list[Any] = []

    async def engine(_c: Any, method: str, body: dict[str, Any], **_k: Any) -> Any:
        sent.append((method, body))
        return {"record_digest": "sha256:" + "a" * 64}

    async def fleet(_c: Any, op: str, params: dict[str, Any]) -> Any:
        called.append((op, params))
        return {}

    monkeypatch.setattr(pin, "require_forecast", structural_sdk)
    monkeypatch.setattr(detail, "engine_send", engine)
    monkeypatch.setattr(detail, "invoke_registered", fleet)
    monkeypatch.setattr(detail, "guard", lambda *_a, **_k: None)
    context = SimpleNamespace(caller=SimpleNamespace(tenant="tenant:one"))
    out = (await detail.handle_market_forecast_sample(context, PARAMS, NO_OP))["value"]
    # The caller gets the stored digest and the flag, never a raw sample.
    assert out == {"record_digest": "sha256:" + "a" * 64, "probabilistic": True}
    assert sent == [
        (
            "SourceIngest",
            {
                "tenant": "tenant:one",
                "source_id": "forecast-1",
                "probabilistic": True,
                "seed": 7,
            },
        )
    ]
    assert called[0][0] == "fleet.call" and called[0][1]["tool"] == detail.FORECAST_TOOL


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R008")
async def test_the_pinned_sdk_refuses_naming_the_missing_adapter() -> None:
    context = SimpleNamespace(caller=SimpleNamespace(tenant="tenant:one"))
    with pytest.raises(OperationRefused) as refused:
        await detail.handle_market_forecast_sample(context, PARAMS, NO_OP)
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"].startswith("scion_sdk.finance.forecast")

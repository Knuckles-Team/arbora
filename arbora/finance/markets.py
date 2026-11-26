"""Tenant-scoped market read service (ARBORA-DATA-MARKET-R003, FI-01/FI-02).

Server-side market read logic consumed by the not-yet-registered
``arbora/api/ops/finance.py`` operation (deferred for the same reason
``arbora/api/ops/access.py`` defers its ``approvals.*`` operations: the
installed EG contract does not yet publish a ``finance:read`` scope for
``get_registry()`` to bind, tracked as GDM-06 follow-up in
``specs/data-and-market-projections/tasks.md``).

Mirrors ``arbora.gateway.schema_context_service``'s fail-closed shape:
``get_market_read`` refuses with ``UNAVAILABLE`` *before* any provider
dispatch when the caller's tenant/account binding is missing or does not
match the requested scope (FI-02's "zero provider calls" proof). Paper and
live order submission (FI-08/FI-10) are separate, not-yet-built slices.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from arbora.api.errors import ArboraErrorCode, ArboraRefusal

__all__ = [
    "AccountBinding",
    "FinanceAuthority",
    "MarketProvider",
    "MarketReadRequest",
    "MarketReadResult",
    "get_market_read",
    "project_account",
    "project_performance",
    "resolve_finance_authority",
    "ticks_to_decimal",
]


class MarketReadRequest(BaseModel):
    """One tenant-scoped market read, as resolved by the caller's session."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=256)
    account_id: str = Field(min_length=1, max_length=256)
    symbol: str = Field(min_length=1, max_length=64)


class MarketReadResult(BaseModel):
    """FI-01: exact decimal price string, source, as-of, currency, staleness."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    account_id: str
    symbol: str
    price: str = Field(description="Exact decimal price, as a string.")
    currency: str
    source: str
    as_of: str = Field(description="ISO-8601 timestamp this read is as of.")
    staleness_seconds: int = Field(ge=0)


class AccountBinding(BaseModel):
    """A verified tenant/account pair.

    Resolving this binding (session, grant, and provider account lookup) is
    the caller's responsibility; this module only refuses to dispatch when
    it is absent or does not match the request.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=256)
    account_id: str = Field(min_length=1, max_length=256)


class MarketProvider(Protocol):
    """The one capability this service dispatches through."""

    def market_read(self, request: MarketReadRequest) -> MarketReadResult:
        """Return a live market read for an already-authorized request."""


def _binding_matches(
    binding: AccountBinding | None, request: MarketReadRequest
) -> bool:
    return (
        binding is not None
        and binding.tenant_id == request.tenant_id
        and binding.account_id == request.account_id
    )


def get_market_read(
    provider: Any,
    request: MarketReadRequest,
    *,
    account_binding: AccountBinding | None,
) -> MarketReadResult:
    """Answer one market read, or refuse before any provider dispatch.

    FI-02: a missing or mismatched ``account_binding`` returns typed
    ``UNAVAILABLE`` and never calls ``provider`` at all -- not even to
    check whether it has the capability.
    """

    if not _binding_matches(account_binding, request):
        raise ArboraRefusal(
            ArboraErrorCode.UNAVAILABLE,
            "unverified tenant/account binding for market read",
            details={"tenant_id": request.tenant_id, "symbol": request.symbol},
        )
    read = getattr(provider, "market_read", None)
    if read is None:
        raise ArboraRefusal(
            ArboraErrorCode.UNAVAILABLE,
            "no market read provider configured",
            details={"symbol": request.symbol},
        )
    return read(request)


class FinanceAuthority(BaseModel):
    """The tenant and principal a finance call runs under, from verified authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=256)
    principal: str = Field(min_length=1, max_length=256)


def resolve_finance_authority(
    caller: Any, params: Mapping[str, Any]
) -> FinanceAuthority:
    """The authority of one finance call, resolved before any provider request.

    ARBORA-DATA-MARKET-R003.5. Tenant and principal come from the verified
    caller only. A request that names either one is refused
    ``INVALID_ARGUMENT``, and a caller without both is ``UNAVAILABLE``. The
    function holds no provider client, so a refusal here has made zero
    provider calls.
    """

    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.api.invoke.steps import (
        FORBIDDEN_AUTHORITY,
        FORBIDDEN_OWNER,
        forbidden_path,
    )

    named = forbidden_path(
        params,
        names=FORBIDDEN_AUTHORITY | FORBIDDEN_OWNER | {"tenant_id", "principal_id"},
    )
    if named is not None:
        raise OperationRefused("INVALID_ARGUMENT", {"field": named})
    tenant = getattr(caller, "tenant", None)
    principal = getattr(caller, "principal", None)
    if not isinstance(tenant, str) or not tenant or not isinstance(principal, str):
        raise OperationRefused("UNAVAILABLE", {"reason": "finance authority absent"})
    if not principal:
        raise OperationRefused("UNAVAILABLE", {"reason": "finance authority absent"})
    return FinanceAuthority(tenant_id=tenant, principal=principal)


def ticks_to_decimal(ticks: int, scale: int) -> str:
    """The exact base-10 string of fixed-point ticks; formatting only, no rounding.

    ``scale`` is a power of ten (the engine's ``FINANCE_SCALE``). The string has
    no exponent and never passes through a float.
    """

    if isinstance(ticks, bool) or not isinstance(ticks, int):
        raise ValueError("ticks must be an integer")
    if not isinstance(scale, int) or scale < 1 or set(str(scale)) - {"1", "0"}:
        raise ValueError("scale must be a power of ten")
    if scale == 1:
        return str(ticks)
    width = len(str(scale)) - 1
    sign = "-" if ticks < 0 else ""
    whole, fraction = divmod(abs(ticks), scale)
    return f"{sign}{whole}.{fraction:0{width}d}"


_FIGURE_KEYS = ("position", "name", "value_ticks", "source", "as_of", "session")


def project_account(projection: Mapping[str, Any]) -> dict[str, Any]:
    """The served shape of one engine account projection.

    ARBORA-DATA-MARKET-R003.11. Every figure keeps the source, as-of and
    session the engine gave it; a figure without all three is a malformed
    engine answer and refuses ``UNAVAILABLE``. Arbora adds the decimal string
    of the engine's ticks and does no other arithmetic.
    """

    from arbora.api.invoke.pipeline import OperationRefused

    try:
        scale = projection["scale"]
        figures = []
        for figure in projection["figures"]:
            if any(
                not figure.get(key) and key != "value_ticks" for key in _FIGURE_KEYS
            ):
                raise KeyError("figure provenance")
            figures.append(
                {
                    **{key: figure[key] for key in _FIGURE_KEYS},
                    "value": ticks_to_decimal(figure["value_ticks"], scale),
                }
            )
        return {
            "account": projection["account"],
            "as_of": projection["as_of"],
            "known_at": projection["known_at"],
            "basis_policy": projection["basis_policy"],
            "base_currency": projection["base_currency"],
            "scale": scale,
            "input_digest": projection["input_digest"],
            "figures": figures,
        }
    except (KeyError, TypeError, ValueError, AttributeError):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "engine result malformed"}
        ) from None


def project_performance(card: Mapping[str, Any]) -> dict[str, Any]:
    """The served shape of one engine performance card (ARBORA-DATA-MARKET-R009.1).

    Each number is the engine's decimal string, copied as it came. A figure the
    engine could not produce stays null with its reason and is never zero.
    """

    from arbora.api.invoke.pipeline import OperationRefused

    try:
        benchmark = card.get("benchmark")
        return {
            "method": card["method"],
            "currency": card["currency"],
            "range": card["range"],
            "value": card["value"],
            "benchmark": None
            if benchmark is None
            else {
                "name": benchmark["name"],
                "value": benchmark["value"],
                "unavailable_reason": benchmark["reason"],
            },
            "source": card["source"],
            "as_of": card["as_of"],
            "session": card["session"],
            "stale": card["stale"],
            "unavailable_reason": card["reason"],
        }
    except (KeyError, TypeError, AttributeError):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "engine result malformed"}
        ) from None

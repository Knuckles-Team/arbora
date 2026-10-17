# mypy: disable-error-code="arg-type,assignment,misc,call-arg"
"""Public-surface behaviour of the authenticated scaling-signal provider."""

import math
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from graph_os.fleet.scaling.scaling_signal_authority import (
    AuthenticatedSignalProvider,
    OpaqueReferenceAuthenticator,
    SignalAggregation,
    SignalBackendPage,
    SignalKind,
    SignalReadPolicy,
    SignalReadRequest,
    SignalSample,
    SignalSourceBinding,
    SignalSourceKind,
    SignalSummary,
    query_digest,
    summarize_signal_page,
)

pytestmark = pytest.mark.spec("GRAPHOS-CAPACITY-R750.751")

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
WINDOW_START = NOW - timedelta(minutes=2)
WINDOW_END = NOW
SNAPSHOT_DIGEST = "b" * 64
QUERY = "sum(cpu_usage_seconds_total)"


class _Backend:
    def __init__(self, page: SignalBackendPage):
        self.samples = tuple(page.samples)
        self.next_cursor = page.next_cursor
        self.has_more = page.has_more
        self.snapshot_digest = page.snapshot_digest
        self.requests: list[SignalReadRequest] = []

    def read(self, request: SignalReadRequest) -> SignalBackendPage:
        self.requests.append(request)
        return SignalBackendPage(
            iter(self.samples),
            next_cursor=self.next_cursor,
            has_more=self.has_more,
            snapshot_digest=self.snapshot_digest,
        )


def _binding() -> SignalSourceBinding:
    return SignalSourceBinding(
        source_id="source:metrics",
        source_kind=SignalSourceKind.TELEMETRY,
        service_scope="service:gateway",
        tenant_scope="tenant:acme",
        unit_id="unit:gateway",
        source_epoch=3,
        auth_evidence_ref="attestation:metrics:3",
    )


def _request(**overrides: object) -> SignalReadRequest:
    values: dict[str, object] = {
        "request_id": "read:gateway:1",
        "source_id": "source:metrics",
        "service_scope": "service:gateway",
        "tenant_scope": "tenant:acme",
        "unit_id": "unit:gateway",
        "signals": (SignalKind.CPU,),
        "aggregation": SignalAggregation.MEAN,
        "query": QUERY,
        "query_digest": query_digest(QUERY),
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "timeout_s": 3.0,
        "page_size": 16,
        "max_items": 64,
    }
    values.update(overrides)
    return SignalReadRequest(**values)


def _sample(
    *,
    sample_id: str = "sample:1",
    sequence: int = 1,
    value: float = 42.0,
    sample_time: datetime = NOW - timedelta(seconds=10),
    event_time: datetime | None = None,
    service_scope: str = "service:gateway",
    tenant_scope: str = "tenant:acme",
    unit_id: str = "unit:gateway",
    source_id: str = "source:metrics",
    source_kind: SignalSourceKind = SignalSourceKind.TELEMETRY,
    source_epoch: int = 3,
    auth_evidence_ref: str = "attestation:metrics:3",
    freshness_s: float = 10.0,
    confidence: float = 0.95,
    evidence_ref: str | None = None,
    query: str = QUERY,
    sample_digest: str | None = None,
) -> SignalSample:
    return SignalSample(
        sample_id=sample_id,
        sample_digest=sample_digest or (hex(sequence)[2:] * 64)[:64],
        source_id=source_id,
        source_kind=source_kind,
        source_epoch=source_epoch,
        auth_evidence_ref=auth_evidence_ref,
        service_scope=service_scope,
        tenant_scope=tenant_scope,
        unit_id=unit_id,
        signal=SignalKind.CPU,
        aggregation=SignalAggregation.MEAN,
        value=value,
        sample_time=sample_time,
        event_time=event_time or sample_time,
        freshness_s=freshness_s,
        confidence=confidence,
        evidence_ref=evidence_ref or f"evidence:sample:{sample_id.replace(':', '-')}",
        sequence=sequence,
        query_digest=query_digest(query),
    )


def _provider(
    page: SignalBackendPage,
    *,
    authenticator: object | None = None,
) -> tuple[AuthenticatedSignalProvider, SignalReadRequest]:
    backend = _Backend(page)
    return (
        AuthenticatedSignalProvider(
            backend,
            _binding(),
            authenticator=authenticator,
            clock=lambda: NOW,
        ),
        _request(),
    )


def _page(*samples: SignalSample, **kw: object) -> SignalBackendPage:
    return SignalBackendPage(
        iter(samples),
        next_cursor=kw.get("next_cursor"),
        has_more=bool(kw.get("has_more", False)),
        snapshot_digest=kw.get("snapshot_digest", SNAPSHOT_DIGEST),
    )


class _Rejecting(OpaqueReferenceAuthenticator):
    def verify(self, sample: SignalSample, binding: SignalSourceBinding) -> bool:
        return False


@pytest.mark.parametrize(
    ("build", "match"),
    [
        (lambda: _request(query_digest="c" * 64), "query_digest"),
        (lambda: _request(query="sum(rate(x{" + "token" + '="v"}[5m]))'), "credential"),
        (
            lambda: _request(query="x" * 4_097, query_digest=query_digest("x" * 4_097)),
            "query length",
        ),
        (lambda: _request(window_start=NOW - timedelta(days=2)), "window"),
        (lambda: _request(page_size=257), ""),
        (lambda: _request(signals=(SignalKind.CPU, SignalKind.CPU)), "duplicate"),
        (lambda: _sample(value=-1.0), ""),
        (lambda: _sample(value=math.nan), ""),
        (lambda: _sample(value=math.inf), ""),
        (lambda: _sample(event_time=NOW), "event_time"),
        (lambda: _sample(confidence=1.1), "confidence"),
        (
            lambda: _sample(evidence_ref="bearer=not-a-reference"),
            "unsupported characters",
        ),
    ],
)
def test_invalid_requests_and_samples_fail_closed(build, match) -> None:
    with pytest.raises(ValidationError, match=match):
        build()


def test_batch_read_and_summary_keep_samples_out_of_graph() -> None:
    sample = _sample()
    provider, request = _provider(_page(sample))
    page = provider.read_batch(request)
    assert page.samples == (sample,)
    projection = page.graph_projection()
    assert projection["sample_count"] == 1
    assert "samples" not in projection

    summary, evidence = summarize_signal_page(request, page)
    assert isinstance(summary, SignalSummary)
    assert summary.value == 42.0
    assert {"query", "samples"}.isdisjoint(summary.graph_projection())
    assert evidence.query == QUERY
    assert (evidence.window_start, evidence.window_end) == (WINDOW_START, WINDOW_END)


def test_missing_batch_is_not_interpreted_as_zero() -> None:
    provider, request = _provider(_page())
    page = provider.read_batch(request)
    assert page.samples == ()
    assert summarize_signal_page(request, page) is None


@pytest.mark.parametrize(
    ("sample", "authenticator", "match"),
    [
        (
            _sample(sample_time=NOW - timedelta(minutes=10), freshness_s=10),
            None,
            "stale",
        ),
        (_sample(service_scope="service:other"), None, "scope"),
        (_sample(source_id="source:spoof"), None, "source"),
        (_sample(), _Rejecting(), "authentication"),
    ],
)
def test_scope_source_freshness_and_authentication_are_enforced(
    sample, authenticator, match
) -> None:
    provider, request = _provider(_page(sample), authenticator=authenticator)
    with pytest.raises(ValueError, match=match):
        provider.read_batch(request)


def test_replayed_and_out_of_order_samples_are_rejected() -> None:
    provider, request = _provider(_page(_sample()))
    provider.read_batch(request)
    with pytest.raises(ValueError, match="replayed"):
        provider.read_batch(request)

    first = _sample(sample_id="sample:1", sequence=1)
    second = _sample(sample_id="sample:2", sequence=2, sample_digest="d" * 64)
    provider, request = _provider(_page(second, first))
    with pytest.raises(ValueError, match="replayed"):
        provider.read_batch(request)


def test_snapshot_pagination_is_deterministic_and_pinned() -> None:
    second = _sample(
        sample_id="sample:2",
        sequence=2,
        sample_time=NOW - timedelta(seconds=5),
        sample_digest="d" * 64,
    )

    class _Paged:
        def read(self, request: SignalReadRequest) -> SignalBackendPage:
            if request.cursor is None:
                return _page(_sample(), next_cursor="cursor:1", has_more=True)
            return _page(second)

    provider = AuthenticatedSignalProvider(_Paged(), _binding(), clock=lambda: NOW)
    first_request = _request(page_size=1, max_items=1)
    assert provider.read_batch(first_request).next_cursor == "cursor:1"
    next_request = first_request.model_copy(
        update={"cursor": "cursor:1", "snapshot_digest": SNAPSHOT_DIGEST}
    )
    assert provider.read_batch(next_request).samples == (second,)

    class _Changing:
        def read(self, request: SignalReadRequest) -> SignalBackendPage:
            return _page(second, snapshot_digest="e" * 64)

    provider = AuthenticatedSignalProvider(_Changing(), _binding(), clock=lambda: NOW)
    with pytest.raises(ValueError, match="snapshots"):
        provider.read_batch(next_request)


def test_out_of_policy_requests_never_reach_the_backend() -> None:
    backend = _Backend(_page(_sample()))
    provider = AuthenticatedSignalProvider(backend, _binding(), clock=lambda: NOW)
    with pytest.raises(ValueError, match="service scope"):
        provider.read_batch(_request(service_scope="service:other"))
    provider = AuthenticatedSignalProvider(
        backend,
        _binding(),
        policy=SignalReadPolicy(max_signal_kinds=1),
        clock=lambda: NOW,
    )
    with pytest.raises(ValueError, match="vocabulary"):
        provider.read_batch(_request(signals=(SignalKind.CPU, SignalKind.RAM)))
    assert backend.requests == []

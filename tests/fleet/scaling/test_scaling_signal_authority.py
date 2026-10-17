"""Public-surface behaviour of the authenticated scaling-signal provider."""

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from graph_os.fleet.scaling.scaling_signal_authority import (
    AuthenticatedSignalProvider,
    OpaqueReferenceAuthenticator,
    SignalBackendPage,
    SignalReadPolicy,
    SignalReadRequest,
    SignalSample,
    SignalSourceBinding,
    query_digest,
    summarize_signal_page,
)

pytestmark = pytest.mark.spec("GRAPHOS-CAPACITY-R750.751")

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
QUERY, SNAPSHOT = "sum(cpu_usage_seconds_total)", "b" * 64
SCOPE: dict[str, Any] = {
    "source_id": "source:metrics",
    "service_scope": "service:gateway",
    "tenant_scope": "tenant:acme",
    "unit_id": "unit:gateway",
}
SOURCE: dict[str, Any] = {
    **SCOPE,
    "source_kind": "telemetry",
    "source_epoch": 3,
    "auth_evidence_ref": "attestation:metrics:3",
}
BINDING = SignalSourceBinding.model_validate(SOURCE)


def request(**update: Any) -> SignalReadRequest:
    payload = {
        **SCOPE,
        "request_id": "read:gateway:1",
        "signals": ("cpu",),
        "aggregation": "mean",
        "query": QUERY,
        "query_digest": query_digest(update.get("query", QUERY)),
        "window_start": NOW - timedelta(minutes=2),
        "window_end": NOW,
        "timeout_s": 3.0,
        "page_size": 16,
        "max_items": 64,
    }
    return SignalReadRequest.model_validate({**payload, **update})


def sample(sequence: int = 1, **update: Any) -> SignalSample:
    sample_time = update.pop("sample_time", NOW - timedelta(seconds=10))
    payload = {
        **SOURCE,
        "sample_id": f"sample:{sequence}",
        "sample_digest": str(sequence) * 64,
        "signal": "cpu",
        "aggregation": "mean",
        "value": 42.0,
        "sample_time": sample_time,
        "event_time": sample_time,
        "freshness_s": 10.0,
        "confidence": 0.95,
        "evidence_ref": f"evidence:sample-{sequence}",
        "sequence": sequence,
        "query_digest": query_digest(QUERY),
    }
    return SignalSample.model_validate({**payload, **update})


class Backend:
    """Streaming backend that serves one page per cursor and records reads."""

    def __init__(self, *pages: tuple[SignalSample, ...], snapshot: str = SNAPSHOT):
        self.pages, self.snapshot = pages, snapshot
        self.requests: list[SignalReadRequest] = []

    def read(self, request: SignalReadRequest) -> SignalBackendPage:
        self.requests.append(request)
        index = 0 if request.cursor is None else int(request.cursor.split(":")[1])
        more = index + 1 < len(self.pages)
        return SignalBackendPage(
            iter(self.pages[index] if self.pages else ()),
            next_cursor=f"cursor:{index + 1}" if more else None,
            has_more=more,
            snapshot_digest=self.snapshot,
        )


def provider(backend: Backend, **kw: Any) -> AuthenticatedSignalProvider:
    return AuthenticatedSignalProvider(backend, BINDING, clock=lambda: NOW, **kw)


class Rejecting(OpaqueReferenceAuthenticator):
    def verify(self, sample: SignalSample, binding: SignalSourceBinding) -> bool:
        return False


@pytest.mark.parametrize(
    ("build", "update", "match"),
    [
        (request, {"query_digest": "c" * 64}, "query_digest"),
        (request, {"query": "sum(x{" + "token" + '="v"})'}, "credential"),
        (request, {"query": "x" * 4_097}, "query length"),
        (request, {"window_start": NOW - timedelta(days=2)}, "window"),
        (request, {"page_size": 257}, "page_size"),
        (request, {"signals": ("cpu", "cpu")}, "duplicate"),
        (sample, {"value": -1.0}, "value"),
        (sample, {"value": math.nan}, "value"),
        (sample, {"value": math.inf}, "value"),
        (sample, {"event_time": NOW}, "event_time"),
        (sample, {"confidence": 1.1}, "confidence"),
        (sample, {"evidence_ref": "bearer=not-a-reference"}, "unsupported characters"),
    ],
)
def test_invalid_requests_and_samples_fail_closed(
    build: Any, update: dict[str, Any], match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        build(**update)


def test_batch_read_and_summary_keep_samples_out_of_graph() -> None:
    page = provider(Backend((sample(),))).read_batch(request())
    assert page.samples == (sample(),)
    assert page.graph_projection()["sample_count"] == 1
    assert "samples" not in page.graph_projection()
    summarized = summarize_signal_page(request(), page)
    assert summarized is not None
    summary, evidence = summarized
    assert summary.value == 42.0
    assert {"query", "samples"}.isdisjoint(summary.graph_projection())
    assert (evidence.query, evidence.window_end) == (QUERY, NOW)


def test_missing_batch_is_not_interpreted_as_zero() -> None:
    page = provider(Backend()).read_batch(request())
    assert page.samples == ()
    assert summarize_signal_page(request(), page) is None


@pytest.mark.parametrize(
    ("bad", "kw", "match"),
    [
        (sample(sample_time=NOW - timedelta(minutes=10)), {}, "stale"),
        (sample(service_scope="service:other"), {}, "scope"),
        (sample(source_id="source:spoof"), {}, "source"),
        (sample(), {"authenticator": Rejecting()}, "authentication"),
    ],
)
def test_scope_source_freshness_and_authentication_are_enforced(
    bad: SignalSample, kw: dict[str, Any], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        provider(Backend((bad,)), **kw).read_batch(request())


def test_replayed_and_out_of_order_samples_are_rejected() -> None:
    reader = provider(Backend((sample(),)))
    reader.read_batch(request())
    with pytest.raises(ValueError, match="replayed"):
        reader.read_batch(request())
    with pytest.raises(ValueError, match="replayed"):
        provider(Backend((sample(2), sample(1)))).read_batch(request())


def test_snapshot_pagination_is_deterministic_and_pinned() -> None:
    second = sample(2, sample_time=NOW - timedelta(seconds=5))
    first_request = request(page_size=1, max_items=1)
    paged = provider(Backend((sample(),), (second,)))
    assert paged.read_batch(first_request).next_cursor == "cursor:1"
    pinned = first_request.model_copy(
        update={"cursor": "cursor:1", "snapshot_digest": SNAPSHOT}
    )
    assert paged.read_batch(pinned).samples == (second,)
    changed = provider(Backend((), (second,), snapshot="e" * 64))
    with pytest.raises(ValueError, match="snapshots"):
        changed.read_batch(pinned)


@pytest.mark.parametrize(
    ("kw", "read", "match"),
    [
        ({}, request(service_scope="service:other"), "service scope"),
        (
            {"policy": SignalReadPolicy(max_signal_kinds=1)},
            request(signals=("cpu", "ram")),
            "vocabulary",
        ),
    ],
)
def test_out_of_policy_requests_never_reach_the_backend(
    kw: dict[str, Any], read: SignalReadRequest, match: str
) -> None:
    backend = Backend((sample(),))
    with pytest.raises(ValueError, match=match):
        provider(backend, **kw).read_batch(read)
    assert backend.requests == []

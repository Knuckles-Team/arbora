import datetime as dt
import time
from types import SimpleNamespace

import pytest

from graph_os.observability.readiness import (
    check,
    collect_readiness_snapshot,
    identity_check,
    is_snapshot_ready,
)

NOW = lambda: dt.datetime(2026, 1, 1, tzinfo=dt.UTC)  # noqa: E731
OK = lambda: check("ready")  # noqa: E731


def _slow():
    time.sleep(0.5)
    return check("ready")


def _raise():
    raise RuntimeError("x")


def _snap(probes, **kw):
    return collect_readiness_snapshot(probes, now=NOW, deadline_s=0.2, **kw)


@pytest.mark.spec("GRAPHOS-OPS-R700")
def test_all_ready():
    s = _snap({"engine": OK, "catalog": OK, "synthetic_query": OK})
    assert (s["overall"], s["required_failures"]) == ("ready", [])
    assert is_snapshot_ready(s)


@pytest.mark.parametrize(
    ("bad", "overall", "failures"),
    [
        (lambda: check("unavailable", reason="r"), "unavailable", ["engine"]),
        (_raise, "unavailable", ["engine"]),
        (_slow, "unavailable", ["engine"]),
        (lambda: {"state": "bogus"}, "unavailable", ["engine"]),
    ],
)
@pytest.mark.spec("GRAPHOS-OPS-R700")
def test_required_failure_fails_snapshot(bad, overall, failures):
    s = _snap({"engine": bad, "catalog": OK, "synthetic_query": OK})
    assert (s["overall"], s["required_failures"]) == (overall, failures)
    assert not is_snapshot_ready(s)


@pytest.mark.spec("GRAPHOS-OPS-R700")
def test_optional_degradation_and_missing_required():
    s = _snap(
        {
            "engine": OK,
            "catalog": OK,
            "synthetic_query": OK,
            "dense": lambda: check("degraded"),
        }
    )
    assert (s["overall"], s["degraded_reasons"]) == ("degraded", ["dense"])
    assert _snap({"engine": OK})["required_failures"] == ["catalog", "synthetic_query"]


@pytest.mark.spec("GRAPHOS-OPS-R700")
def test_snapshot_id_is_deterministic_and_identity_needs_verified_actor():
    assert _snap({"engine": OK})["snapshot_id"] == _snap({"engine": OK})["snapshot_id"]
    verified = SimpleNamespace(actor=SimpleNamespace(authenticated=True))
    unverified = SimpleNamespace(actor=SimpleNamespace(authenticated=False))
    assert [identity_check(x)["state"] for x in (None, unverified, verified)] == [
        "not_configured",
        "degraded",
        "ready",
    ]

"""Trace export settings: the endpoint, the header parser and origin checks."""

from __future__ import annotations

import pytest

from arbora.observability import custom_observability as co
from arbora.observability import langfuse_trust as lt
from arbora.observability import telemetry


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, ""),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "collector:4318"}, "http://collector:4318"),
        (
            {
                "MEDULLA_OBS_ADDR": "https://obs",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "x",
            },
            "https://obs",
        ),
        (
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "https://c",
                "OTEL_TRACES_EXPORTER": "none",
            },
            "",
        ),
    ],
)
def test_otel_endpoint_follows_the_standard_variables(monkeypatch, env, expected):
    for name in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "MEDULLA_OBS_ADDR",
        "OTEL_TRACES_EXPORTER",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert telemetry.otel_endpoint() == expected
    if not expected:
        assert telemetry.configure_standard_otel() is False


def test_otlp_headers_parse_and_reject_malformed_input():
    assert co.parse_otlp_headers("a=1,b=two") == {"a": "1", "b": "two"}
    with pytest.raises(ValueError):
        co.parse_otlp_headers("missing-separator")


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        ("https://lf.example/api", "https://lf.example", True),
        ("https://lf.example", "https://other.example", False),
        ("http://lf.example", "https://lf.example", False),
    ],
)
def test_same_origin(left, right, same):
    assert co._same_origin(left, right) is same


@pytest.mark.parametrize(
    ("host", "ok"),
    [
        ("https://lf.example/", True),
        ("http://127.0.0.1:3000", True),
        ("http://lf.example", False),
        ("not-a-url", False),
    ],
)
def test_langfuse_host_requires_https_outside_loopback(host, ok):
    if ok:
        assert lt.resolve_langfuse_host(environ={"LANGFUSE_HOST": host}) == host.rstrip(
            "/"
        )
    else:
        with pytest.raises(ValueError):
            lt.resolve_langfuse_host(environ={"LANGFUSE_HOST": host})

"""The coverage gate: registry reader and the pure comparison.

ARBORA-FLEET-R060.1 and R060.2: the org registry is the ``connectors``
group of ``workspace.yml`` (a repo maps to its ``mcp_server`` key, else its
own name; self-served connectors are excluded), and the pure check reports
``no_endpoint``/``not_admitted`` missing servers and ``zero_tools`` servers
without admitting anything.
"""

from __future__ import annotations

import pytest
import yaml

from arbora.fleet.coverage import (
    CoverageReport,
    FleetCoverageError,
    RegisteredConnector,
    check_fleet_coverage,
    registered_connectors,
)


def _write_manifest(tmp_path, document) -> None:
    path = tmp_path / "workspace.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


@pytest.mark.spec("ARBORA-FLEET-R060.1")
def test_registry_maps_repo_to_server_key(tmp_path) -> None:
    path = _write_manifest(
        tmp_path,
        {
            "connectors": [
                "knuckles-team/gitlab-connector",
                {"repo": "knuckles-team/github-connector", "mcp_server": "github"},
                {"repo": "arbora/own-connector"},
                42,
            ]
        },
        # the bad entry last: the reader is fail-closed before it
    )
    with pytest.raises(FleetCoverageError):
        registered_connectors(path)

    path = _write_manifest(
        tmp_path,
        {
            "connectors": [
                "knuckles-team/gitlab-connector",
                {"repo": "knuckles-team/github-connector", "mcp_server": "github"},
            ]
        },
    )
    registered = registered_connectors(path)
    assert registered == (
        RegisteredConnector(repo="knuckles-team/github-connector", server="github"),
        RegisteredConnector(
            repo="knuckles-team/gitlab-connector",
            server="knuckles-team/gitlab-connector",
        ),
    )

    # self-served connectors are excluded by repo or server key
    path = _write_manifest(
        tmp_path,
        {
            "connectors": [
                "knuckles-team/gitlab-connector",
                {"repo": "arbora/self", "mcp_server": "arbora"},
            ]
        },
    )
    assert registered_connectors(path, exclude=("arbora",)) == (
        RegisteredConnector(
            repo="knuckles-team/gitlab-connector",
            server="knuckles-team/gitlab-connector",
        ),
    )

    # a missing or unparsable manifest is a refusal; an absent group is empty
    with pytest.raises(FleetCoverageError):
        registered_connectors(tmp_path / "absent.yml")
    with pytest.raises(FleetCoverageError):
        registered_connectors(_write_manifest(tmp_path, [1, 2]))
    assert registered_connectors(_write_manifest(tmp_path, {})) == ()
    assert registered_connectors(_write_manifest(tmp_path, {"connectors": None})) == ()
    # duplicate server keys are unparsable
    with pytest.raises(FleetCoverageError):
        registered_connectors(
            _write_manifest(
                tmp_path,
                {"connectors": ["a/repo", {"repo": "b/other", "mcp_server": "a/repo"}]},
            )
        )


def _endpoint(name: str):
    from types import SimpleNamespace

    return SimpleNamespace(name=name)


@pytest.mark.spec("ARBORA-FLEET-R060.2")
def test_missing_and_zero_tool_servers_are_reported() -> None:
    registered = (
        RegisteredConnector(repo="a/repo", server="healthy"),
        RegisteredConnector(repo="b/repo", server="no-endpoint"),
        RegisteredConnector(repo="c/repo", server="rejected"),
        RegisteredConnector(repo="d/repo", server="empty"),
        RegisteredConnector(repo="e/repo", server="absent"),
    )
    # "no-endpoint" has no enabled endpoint at all; "absent" has one but no
    # admitted catalog entry, so the two reasons stay distinguishable
    endpoints = (
        _endpoint("healthy"),
        _endpoint("rejected"),
        _endpoint("empty"),
        _endpoint("absent"),
    )
    tool_counts = {"healthy": 3, "rejected": 0, "empty": 0}

    report = check_fleet_coverage(
        registered, endpoints, tool_counts, unadmitted=("rejected",)
    )
    assert isinstance(report, CoverageReport)
    # the report keeps the registry order the caller passed in
    assert report.registered == (
        "healthy",
        "no-endpoint",
        "rejected",
        "empty",
        "absent",
    )
    assert report.covered == ("healthy",)
    assert report.missing == (
        {"server": "no-endpoint", "reason": "no_endpoint"},
        {"server": "rejected", "reason": "not_admitted"},
        {"server": "absent", "reason": "not_admitted"},
    )
    assert report.zero_tools == ("empty",)
    assert report.ok is False

    # the healthy set alone is ok; the check is pure and reads no clock
    report = check_fleet_coverage((registered[0],), endpoints, {"healthy": 3})
    assert report.ok is True
    assert report.missing == ()
    assert report.zero_tools == ()
    # a server in tool_counts but not admitted is not_admitted, not covered
    report = check_fleet_coverage(
        (RegisteredConnector(repo="d/repo", server="empty"),),
        endpoints,
        {"empty": 0},
    )
    assert report.zero_tools == ("empty",)
    assert report.ok is False

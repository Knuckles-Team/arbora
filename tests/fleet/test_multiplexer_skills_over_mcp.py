"""Skills-over-MCP fleet discovery (CONCEPT:MERISTEM-ECO.mcp.skills-over-mcp-provider).

Covers the client-side half of Skills-over-MCP: probing a fleet server's
``skill://{name}/SKILL.md`` Resources alongside its Tools, bounding that
catalog the same way the tool catalog is bounded, and ranking skills and
tools together in one ``find_tools`` result set — the unified capability
space (CONCEPT:MERISTEM-KG.retrieval.unified-capability-contract).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from arbora.fleet.child_payload_bounds import _bounded_skill_catalog
from tests.fleet.test_multiplexer_dynamic_gateway import (
    CNT,
    CNT_TOOL,
    _fake_tool,
    _mux_with_children,
)

_DOCKER_CONTAINERS_TOOL_ENTRY = {
    "name": CNT_TOOL,
    "description": "manage docker containers",
    "inputSchema": {},
}


def _fake_skill_resource(uri: str, description: str = ""):
    resource = MagicMock()
    resource.uri = uri
    resource.description = description
    return resource


def _fake_resource_body(text: str):
    """A ``resources/read`` result carrying one text content part."""
    content = MagicMock()
    content.text = text
    result = MagicMock()
    result.contents = [content]
    return result


def _fake_session_with_resources(tools, resources, bodies=None):
    sess = AsyncMock()
    tools_result = MagicMock()
    tools_result.tools = [_fake_tool(n, d) for n, d in tools]
    sess.list_tools = AsyncMock(return_value=tools_result)
    resources_result = MagicMock()
    resources_result.resources = resources
    sess.list_resources = AsyncMock(return_value=resources_result)
    # CONCEPT:MERISTEM-ECO.mcp.cross-process-skill-harvest — a probe now also READS
    # each ``skill://`` body, so a fake child must serve one.
    bodies = bodies or {}

    async def _read(uri):
        return _fake_resource_body(bodies.get(str(uri), f"# {uri}\n\nbody"))

    sess.read_resource = AsyncMock(side_effect=_read)
    return sess


def _fake_session_with_only_tools() -> AsyncMock:
    """A child session that serves ``CNT_TOOL`` but has no usable resources
    endpoint yet -- the caller still sets ``list_resources`` to its own
    degrade behavior."""
    sess = AsyncMock()
    tools_result = MagicMock()
    tools_result.tools = [_fake_tool(CNT_TOOL, "manage containers")]
    sess.list_tools = AsyncMock(return_value=tools_result)
    return sess


async def _probe_server_info(tmp_path, open_fn):
    """Mount ``CNT`` with ``open_fn`` installed as ``_open_one_session`` and
    probe it -- shared scaffold for the best-effort-skills degrade tests
    below."""
    mux = _mux_with_children(tmp_path, {CNT: [(CNT_TOOL, "manage containers")]})
    mux.transport._open_one_session = AsyncMock(side_effect=open_fn)
    return await mux.probe_server(CNT)


# --------------------------------------------------------------------------- #
# _bounded_skill_catalog
# --------------------------------------------------------------------------- #


def test_bounded_skill_catalog_extracts_only_skill_resources():
    resources = [
        _fake_skill_resource("skill://release-notes/SKILL.md", "draft notes"),
        _fake_skill_resource("skill://release-notes/_manifest", "manifest, ignored"),
        _fake_skill_resource("docs://readme", "unrelated resource, ignored"),
    ]
    skills = _bounded_skill_catalog(resources)
    assert skills == [
        {
            "name": "release-notes",
            "uri": "skill://release-notes/SKILL.md",
            "description": "draft notes",
        }
    ]


def test_bounded_skill_catalog_rejects_non_list_input():
    import pytest

    with pytest.raises(RuntimeError):
        _bounded_skill_catalog("not-a-list")


def test_bounded_skill_catalog_empty_for_no_skill_resources():
    resources = [_fake_skill_resource("docs://readme", "unrelated")]
    assert _bounded_skill_catalog(resources) == []


# --------------------------------------------------------------------------- #
# probe_server: skill resources alongside tools
# --------------------------------------------------------------------------- #


async def test_probe_server_captures_skill_resources_alongside_tools(tmp_path):
    mux = _mux_with_children(tmp_path, {CNT: [(CNT_TOOL, "manage containers")]})

    async def _open(server, cfg, stack):
        return _fake_session_with_resources(
            [(CNT_TOOL, "manage containers")],
            [_fake_skill_resource("skill://onboarding/SKILL.md", "onboard a user")],
        )

    mux.transport._open_one_session = AsyncMock(side_effect=_open)
    info = await mux.probe_server(CNT)

    assert info["error"] is None
    assert info["tools"][0]["name"] == CNT_TOOL
    assert info["skills"] == [
        {
            "name": "onboarding",
            "uri": "skill://onboarding/SKILL.md",
            "description": "onboard a user",
        }
    ]


async def test_probe_server_lists_skills_for_an_already_mounted_child(tmp_path):
    """D-2.2-2.3-1: an already-mounted child's probe used to skip the ``skills``
    key entirely (only the cold-probe branch called ``_probe_skills``), so a
    mounted server's Skills-over-MCP resources were invisible to
    ``find``/``find_tools`` until it happened to be probed cold at least once.
    A mounted child's live primary session is now reused to list resources."""
    mux = _mux_with_children(tmp_path, {CNT: [(CNT_TOOL, "manage containers")]})
    await mux.mount_child(CNT)
    mux.sessions[CNT] = _fake_session_with_resources(
        [(CNT_TOOL, "manage containers")],
        [_fake_skill_resource("skill://onboarding/SKILL.md", "onboard a user")],
    )

    info = await mux.probe_server(CNT)

    assert info["error"] is None
    assert info["tools"][0]["name"] == CNT_TOOL
    # A body comes only from the resident local catalog (ARBORA-FLEET-R006.3.2);
    # a name it does not admit carries none.
    assert info["skills"] == [
        {
            "name": "onboarding",
            "uri": "skill://onboarding/SKILL.md",
            "description": "onboard a user",
        }
    ]


async def test_probe_server_degrades_when_list_resources_unsupported(tmp_path):
    """A server (or an mcp SDK build) with no ``resources/list`` support must
    still yield its tools — Skills-over-MCP is optional, never load-bearing."""

    async def _open(server, cfg, stack):
        sess = _fake_session_with_only_tools()
        sess.list_resources = AsyncMock(side_effect=RuntimeError("no such method"))
        return sess

    info = await _probe_server_info(tmp_path, _open)

    assert info["error"] is None
    assert info["tools"][0]["name"] == CNT_TOOL
    assert info["skills"] == []


async def test_probe_server_degrades_on_malformed_skill_catalog(tmp_path):
    """A malformed resource catalog must not fail the (already-succeeded) tool
    probe — best-effort skills, load-bearing tools."""

    async def _open(server, cfg, stack):
        sess = _fake_session_with_only_tools()
        resources_result = MagicMock()
        resources_result.resources = "not-a-list"  # malformed
        sess.list_resources = AsyncMock(return_value=resources_result)
        return sess

    info = await _probe_server_info(tmp_path, _open)

    assert info["error"] is None
    assert info["tools"][0]["name"] == CNT_TOOL
    assert info["skills"] == []


# --------------------------------------------------------------------------- #
# discover_tools: one ranked capability space
# --------------------------------------------------------------------------- #


async def test_discover_tools_ranks_skills_and_tools_in_one_result_set(tmp_path):
    mux = _mux_with_children(tmp_path, {CNT: [(CNT_TOOL, "manage docker containers")]})
    mux._kg_call = AsyncMock(return_value=None)
    mux._probe_cache[CNT] = {
        "tools": [_DOCKER_CONTAINERS_TOOL_ENTRY],
        "skills": [
            {
                "name": "container-runbook",
                "uri": "skill://container-runbook/SKILL.md",
                "description": "manage docker containers step by step",
            }
        ],
        "error": None,
    }

    discovery = await mux.discover_tools("manage docker containers", top_k=5)
    results = discovery["results"]
    kinds = {r["kind"] for r in results}

    assert kinds == {"tool", "skill"}
    tool_hit = next(r for r in results if r["kind"] == "tool")
    skill_hit = next(r for r in results if r["kind"] == "skill")
    # A tool binds as "the default delegate, scoped to this one tool" -- the
    # skill_name is load-bearing, not decoration: both execute_capability and
    # run_agent reject tool_server without it.
    assert tool_hit["bind"] == {
        "tool_server": CNT,
        "allowed_tools": [CNT_TOOL],
        "skill_name": "meristem-expert",
    }
    assert skill_hit["bind"] == {
        "tool_server": CNT,
        "skill_name": "container-runbook",
    }


async def test_discover_tools_skill_absent_when_server_has_no_skills(tmp_path):
    """A server with no skill:// resources contributes no skill entries — the
    ``skills`` probe key is optional and must default to empty, not error."""
    mux = _mux_with_children(tmp_path, {CNT: [(CNT_TOOL, "manage docker containers")]})
    mux._kg_call = AsyncMock(return_value=None)
    mux._probe_cache[CNT] = {
        "tools": [_DOCKER_CONTAINERS_TOOL_ENTRY],
        "error": None,
        # no "skills" key at all — mirrors a probe_server in-process-mounted
        # branch or a pre-Skills-over-MCP probe cache entry.
    }

    discovery = await mux.discover_tools("manage docker containers", top_k=5)
    assert all(r["kind"] == "tool" for r in discovery["results"])


async def test_a_skill_and_a_same_named_tool_are_one_ranked_space(tmp_path):
    """Skills and tools go to the ranking port together and come back in one order.

    A skill and a tool may share a name on one server; each stays its own row, and
    the port's order (medulla's choice) decides which leads (ARBORA-FLEET-R083.3).
    """
    mux = _mux_with_children(tmp_path, {CNT: [("deploy", "tool description")]})
    mux._kg_call = AsyncMock(return_value=None)
    mux._probe_cache[CNT] = {
        "tools": [{"name": "deploy", "description": "tool description"}],
        "skills": [{"name": "deploy", "description": "skill description"}],
        "error": None,
    }
    seen: list[tuple[str, str]] = []

    async def prefer_skills(question_id, query, candidates):
        seen.append((question_id, query))
        return [c.id for c in sorted(candidates, key=lambda c: c.kind != "skill")]

    mux.fleet.ranking.bind(prefer_skills)
    results = (await mux.discover_tools("deploy description", top_k=5))["results"]

    assert [r["kind"] for r in results] == ["skill", "tool"]
    assert seen == [("catalog.operation_rank", "deploy description")]

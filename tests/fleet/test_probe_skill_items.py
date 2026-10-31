"""ARBORA-FLEET-R040: a live probe reports skills and URI-keyed resources.

The 2026-10-30 fleet census reported ``skills: not served`` for all 69
connectors although each served ``skill://<name>/SKILL.md`` resources: the
probe projection filed them as plain resources keyed by display name.
"""

from __future__ import annotations

from arbora.fleet.catalog_items import items_from_child_probe
from arbora.fleet.multiplexer_ops import resource_uri
from arbora.fleet.surfaces import count_items


def _info() -> dict:
    return {
        "tools": [{"name": "web_search", "inputSchema": {"type": "object"}}],
        "resources": [
            {"uri": "skill://web-search/SKILL.md", "name": "web-search/SKILL.md"},
            {"uri": "skill://web-search/_manifest", "name": "web-search/_manifest"},
            {"uri": "ontology://s/s.ttl", "name": "s.ttl"},
            {"uri": "resource://status", "name": "_status"},
        ],
        "resource_templates": [
            {"uriTemplate": "skill://web-search/{path*}", "name": "web-search_files"}
        ],
        "skills": [
            {
                "name": "web-search",
                "uri": "skill://web-search/SKILL.md",
                "description": "Search the web.",
            }
        ],
    }


def test_skill_resources_become_skill_items_by_name() -> None:
    items = {item.id: item for item in items_from_child_probe("s", _info())}
    skill = items["fleet:skill:s/web-search"]
    assert skill.kind == "skill"
    assert resource_uri(skill.kind, skill.name) == "skill://web-search/SKILL.md"
    assert not any(
        item.kind == "resource" and item.name.endswith("/SKILL.md")
        for item in items.values()
    )


def test_resources_are_keyed_by_the_uri_they_are_read_by() -> None:
    items = items_from_child_probe("s", _info())
    resources = {item.name for item in items if item.kind == "resource"}
    assert resources == {
        "skill://web-search/_manifest",
        "ontology://s/s.ttl",
        "resource://status",
    }
    templates = {item.name for item in items if item.kind == "resource_template"}
    assert templates == {"skill://web-search/{path*}"}


def test_surface_counts_report_the_skill_and_ontology() -> None:
    counts = count_items(
        (item.kind, item.name) for item in items_from_child_probe("s", _info())
    )
    assert counts["skills"] == 1
    assert counts["ontology"] == 1
    assert counts["tools"] == 1

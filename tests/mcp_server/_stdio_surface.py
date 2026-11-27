"""A stdio MCP process serving Arbora's intent surface, for the transport tests."""

from fastmcp import FastMCP

from arbora.api.mcp.resolve import IntentResolver
from arbora.api.mcp.serving import register_intent_surface
from arbora.api.policy.eunomia import PolicyGate


async def _unused_invoke(*_args, **_kwargs):
    raise AssertionError("not called by this process")


def build() -> FastMCP:
    from types import SimpleNamespace

    mcp = FastMCP("arbora-transport-test")
    register_intent_surface(
        mcp,
        registry=SimpleNamespace(digest="digest-one", api_version="1"),
        services=object(),
        resolver=IntentResolver(),
        caller_for_request=lambda: None,
        policy_gate=PolicyGate("none"),
        invoke=_unused_invoke,
    )
    return mcp


if __name__ == "__main__":
    build().run(transport="stdio")

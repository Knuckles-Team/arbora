"""Native protocol paths exempt from the operation-registry surface gate."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProtocolRoute:
    path: str
    owner: str
    reason: str


PROTOCOL_ROUTES = (
    ProtocolRoute("/a2a", "arbora.a2a", "A2A JSON-RPC transport"),
    ProtocolRoute("/.well-known/agent-card.json", "arbora.a2a", "Agent Card discovery"),
    ProtocolRoute(
        "/.well-known/ai-catalog.json", "arbora.gateway", "AI catalog discovery"
    ),
    ProtocolRoute("/.well-known/jwks.json", "arbora.identity", "Public key discovery"),
    ProtocolRoute(
        "/.well-known/openid-configuration", "arbora.identity", "OIDC discovery"
    ),
    ProtocolRoute("/auth/{path:path}", "arbora.identity", "Browser auth state machine"),
    ProtocolRoute("/scim/v2/{path:path}", "arbora.identity", "SCIM protocol"),
    ProtocolRoute("/oauth/{path:path}", "arbora.identity", "OAuth protocol"),
    ProtocolRoute("/fleet/events", "arbora.fleet", "Signed child webhook ingress"),
    ProtocolRoute("/health", "arbora.deployment", "Liveness probe"),
    ProtocolRoute("/health/ready", "arbora.deployment", "Readiness probe"),
    ProtocolRoute("/metrics", "arbora.gateway", "Authenticated metrics"),
    ProtocolRoute("/mcp", "arbora.mcp_server", "MCP transport"),
    ProtocolRoute("/search", "arbora.gateway", "Agentic RAG transport"),
)

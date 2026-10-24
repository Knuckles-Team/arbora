"""Caller-bound browser state over supported public EG client methods."""

from collections.abc import Callable, Mapping
from typing import Any

from arbora.api.contracts.session import GraphSession, resolve_session
from arbora.security.anonymous import client_scope


class ServedBrowserState:
    """No stored caller or process-authority fallback; EG owns all state."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def _call(
        self,
        operation: Callable[..., Any],
        *,
        required_scope: str,
        session: GraphSession | None = None,
    ) -> Any:
        from arbora.mcp_server.served_engine import _run_blocking, _session_context

        caller = resolve_session(session, required_scope=required_scope)

        async def run():
            resolve_session(caller, required_scope=required_scope)
            context = _session_context(caller)
            client = await self._pool.connected_client(context, caller.graph)
            resolve_session(caller, required_scope=required_scope)
            with client_scope(client, context.to_claims()):
                result = await operation(client)
            resolve_session(caller, required_scope=required_scope)
            return result

        return _run_blocking(run)

    def query_cypher(
        self, query: str, params: Mapping[str, Any] | None = None
    ) -> list[Any]:
        from arbora.mcp_server.served_engine import _bind_parameters

        bound = _bind_parameters(query, params)
        return list(
            self._call(
                lambda client: client.query.cypher_read(bound), required_scope="kg:read"
            )
        )

    def create_node_if_absent(
        self, node_id: str, *, properties: Mapping[str, Any]
    ) -> bool:
        return bool(
            self._call(
                lambda client: client.nodes.create_if_absent(node_id, dict(properties)),
                required_scope="kg:write",
            )
        )

    def compare_and_set_node_fields(
        self, node_id: str, conditions: Mapping[str, Any], updates: Mapping[str, Any]
    ) -> bool:
        return bool(
            self._call(
                lambda client: client.nodes.compare_and_set(
                    node_id, dict(conditions), dict(updates)
                ),
                required_scope="kg:write",
            )
        )

    def batch_typed_mutations(
        self, mutations: list[dict[str, Any]], *, session: GraphSession
    ) -> bool:
        operations = [_batch_operation(mutation) for mutation in mutations]
        result = self._call(
            lambda client: client.lifecycle.batch_update(operations),
            required_scope="kg:write",
            session=session,
        )
        nodes = sum(operation["op"] == "upsert_node" for operation in operations)
        edges = len(operations) - nodes
        return (
            isinstance(result, dict)
            and result.get("errors") == []
            and result.get("upserted_nodes") == nodes
            and result.get("upserted_edges") == edges
        )


def _batch_operation(mutation: dict[str, Any]) -> dict[str, Any]:
    properties = dict(mutation["properties"])
    if mutation["kind"] == "node":
        return {
            "op": "upsert_node",
            "id": mutation["id"],
            "properties": {
                **properties,
                "id": mutation["id"],
                "node_type": mutation["node_type"],
            },
        }
    if mutation["kind"] == "edge":
        return {
            "op": "upsert_edge",
            "source": mutation["source"],
            "target": mutation["target"],
            "properties": {**properties, "relationship": mutation["rel_type"]},
        }
    raise ValueError("Unsupported typed browser mutation")

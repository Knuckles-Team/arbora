# ARBORA-OPS quickstart

Run the focused proofs from a clean checkout after `uv sync --extra test`.

1. Registry, digest and scopes: `uv run pytest tests/api/test_api_registry.py tests/api/test_registry_scopes.py`.
2. Invoke pipeline refusals and shared path across transports: `uv run pytest tests/api/test_invoke_contract.py tests/api/test_invoke_governance.py tests/api/test_mcp_shared_path.py`.
3. REST request authority: `uv run pytest tests/api/test_http_session_scope.py tests/api/test_http_projection.py`.
4. Error envelope and refusal causes: `uv run pytest tests/api/test_errors.py tests/api/test_refusal_causes.py`.
5. Ingest operations: `uv run pytest tests/api/test_ingest_ops.py tests/ingest/test_connector_sources.py`.

Scenario for the served host: start `arbora` with the HTTP transport, call `find` for an operation with the typed client in `arbora.client`, then call it with a bearer token. `tests/api/test_client_http.py` runs that scenario in process against a loopback engine.

## Inventory build and description

Run the served proof: `pytest tests/gateway/test_inventory_hot_path.py`. It builds twice from one snapshot and compares the record digests. It then renders one description and compares it with the golden file.

Run the offline replay: `arbora-inventory build --hosts hosts.yml --tenant demo --replay snapshot.json --records-out records.json`. The command opens no socket.

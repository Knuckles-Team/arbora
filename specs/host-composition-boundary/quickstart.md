# ARBORA-HOST quickstart

Run the focused proofs from a clean checkout after `uv sync --extra test`.

1. Import boundary: `uv run pytest tests/test_package_layout.py::test_arbora_imports_no_au_serving_packages`.
2. Single gateway and sole multiplexer: `uv run pytest tests/architecture/test_single_gateway_implementation.py tests/architecture/test_fleet_sole_multiplexer.py`.
3. Operation registry: `uv run pytest tests/mcp_server/test_operation_registry.py`.
4. Engine wiring gate: `uv run pytest tests/mcp_server/test_engine_wiring_gate.py`.
5. Legacy action mapping: `uv run pytest tests/mcp_server/test_legacy_action_mapping.py tests/mcp_server/test_legacy_analyze_intent_mapping.py`.
6. Persisted shapes and migrations: `uv run pytest tests/test_persisted_shapes.py tests/test_data_migrations.py`.

Scenario for the served host: start `arbora` with the HTTP transport, then call one operation with the typed client in `arbora.client`. `tests/test_served_operation_api.py` runs that scenario in process.

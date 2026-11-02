# Quickstart — ARBORA-FLEET

The shortest path that shows the fleet catalog and tools working is the fleet and API test groups, which provision their own fixtures.

1. Install the development environment of the repository (`uv sync`).
2. Run the catalog and onboarding proofs: `uv run pytest tests/fleet -q`.
3. Run the registry and intent-tool proofs: `uv run pytest tests/api -q`.
4. Run the catalog reader proofs: `uv run pytest tests/test_fleet_catalog_reader.py -q`.

Rows listed under "Planned tests" in `test-spec.md` have no running proof yet. Run the scenario for a planned row once its test file exists, with the same `uv run pytest <path> -q` form.

Run these scenarios from a clean checkout with the fixtures in `tests/fleet/`.

## Pin a tool

1. Start the fixture multiplexer from `tests/fleet/catalog_fixture.py`.
2. Call `manage` with op `fleet.tools.pin` and one tool id.
3. Load more tools than the session cap with `evict` set to `lru`.
4. Call `find` and `ask` with op `fleet.status`. The pinned tool shows `pinned: true`.

Test command: `pytest tests/fleet/test_pinned_loads_served.py::test_pin_survives_restart_and_eviction`.

## Check connector coverage

1. Point `ARBORA_CONFIG_DIR` at a directory with a fixture `workspace.yml` and `mcp_config.json`.
2. Call `ask` with op `fleet.status` and `require_full_coverage` set to true.
3. Read the refusal. It names each connector that is missing or serves zero tools.

Test command: `pytest tests/fleet/test_fleet_coverage_served.py::test_missing_and_empty_connectors_fail_status`.

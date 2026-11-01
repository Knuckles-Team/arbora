# Quickstart — ARBORA-FLEET

The shortest path that shows the fleet catalog and tools working is the fleet and API test groups, which provision their own fixtures.

1. Install the development environment of the repository (`uv sync`).
2. Run the catalog and onboarding proofs: `uv run pytest tests/fleet -q`.
3. Run the registry and intent-tool proofs: `uv run pytest tests/api -q`.
4. Run the catalog reader proofs: `uv run pytest tests/test_fleet_catalog_reader.py -q`.

Rows listed under "Planned tests" in `test-spec.md` have no running proof yet. Run the scenario for a planned row once its test file exists, with the same `uv run pytest <path> -q` form.

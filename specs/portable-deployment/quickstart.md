# ARBORA-DEPLOY quickstart

Run the focused proofs from a clean checkout after `uv sync --extra test`.

1. Entry points: `uv run pytest tests/deployment/test_entrypoints.py tests/deployment/test_release_canary.py`.
2. Profiles and first boot: `uv run pytest tests/deployment/test_genesis_environments.py tests/deployment/test_kubernetes_first_boot.py`.
3. Hosting settings: `uv run pytest tests/deployment/test_host_config.py`.
4. Engine locks: `uv run pytest tests/deployment/test_process_locks.py`.
5. Dependency manifest: `uv run pytest tests/test_dependency_manifest.py tests/architecture/test_dependency_manifest_pins_published_packages.py`.

Scenario for the served surface: install the built wheel in a new environment, run `arbora-release-canary --json`, then `setup-config environments validate dev`. The tests above cover each step in process.

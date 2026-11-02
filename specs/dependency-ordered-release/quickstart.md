# ARBORA-RELEASE — Quickstart

Plan a release candidate and read the exit-criteria matrix:

```bash
uv run setup-config release-plan <candidate.json>
```

Run the release canary against an installed build:

```bash
uv run arbora-release-canary
```

Run the served plan-run proof and the dependency-floor checks:

```bash
uv run pytest tests/api/test_plan_run_ops.py tests/architecture/test_dependency_manifest_pins_published_packages.py
```

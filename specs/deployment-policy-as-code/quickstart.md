# ARBORA-DEPLOY-POLICY — Quickstart

Run the served manifest proof (a hot-path proof through `invoke`):

```bash
uv run pytest tests/api/test_deploy_manifest_served.py
```

Run the served agent surface proof:

```bash
uv run pytest tests/fleet/test_agent_surface_served.py
```

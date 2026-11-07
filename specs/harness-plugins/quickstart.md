# ARBORA-HARNESS-PLUGINS — Quickstart

Run the served proofs:

- Bundle and install: `uv run pytest tests/harness_plugins/test_bundle_served.py -q`
- Workstream over MCP: `uv run pytest tests/api/ops/test_workstreams_served.py -q`
- Generated `opencode` module: `uv run pytest tests/harness_plugins/test_render_opencode.py -q`

Scenario on a developer machine:

1. Run `arbora plugin install --harness claude-code`.
2. Start the harness, sign in, and send one prompt. The first prompt attaches the workstream.
3. Run `arbora plugin install --harness opencode` and start that harness on the same checkout.
4. The first prompt there shows the checkpoint of the first session.

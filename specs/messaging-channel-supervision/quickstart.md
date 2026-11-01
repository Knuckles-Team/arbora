# Quickstart

1. Run `uv run pytest tests/messaging/test_supervision.py tests/messaging/test_health_component.py`.
2. Start Arbora with one enabled channel whose adapter is not installed.
3. Read `GET /api/dashboard/health` with an authenticated session and confirm the `messaging_intake` component reports the typed degraded reason while other channels run.

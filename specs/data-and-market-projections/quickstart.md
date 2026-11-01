# Quickstart

1. Run the schema-context tests: `uv run pytest tests/gateway/test_schema_context_service.py tests/gateway/test_schema_context_api.py`.
2. Run the finance and admission tests: `uv run pytest tests/finance tests/control_plane/admission/test_dry_run.py`.
3. Run the served proof of the market reads once it lands: `uv run pytest tests/mcp_server/test_data_market_reads_served.py`.
4. Start one Arbora server with a loopback graph engine and a mock market provider. Call `markets.list` over REST and MCP with two tenant identities and confirm both transports return the same projection and the second tenant sees no data of the first.

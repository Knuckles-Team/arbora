# ARBORA-ACP — Quickstart

Run the session-policy and coding-session tests:

```bash
uv run pytest tests/acp/test_session_policy.py tests/acp/test_coding_session.py
```

Run the served HTTP and SSE proof against the registered route (a hot-path proof):

```bash
uv run pytest tests/acp/test_http_transport.py tests/acp/test_central_runtime.py
```

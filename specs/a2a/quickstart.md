# ARBORA-A2A quickstart

Run the focused proofs from a clean checkout after `uv sync --extra test`.

1. Unary methods, refusals and the Agent Card: `uv run pytest tests/a2a/test_application.py tests/a2a/test_authority.py tests/a2a/test_models.py`.
2. Non-blocking send over the served HTTP route: `uv run pytest tests/a2a/test_nonblocking_send.py`.
3. Operation bridge on `Surface.A2A`: `uv run pytest tests/a2a/test_operation_bridge.py tests/a2a/test_request_session_authority.py`.
4. Detached approval signature and identity snapshots: `uv run pytest tests/a2a/test_approval_signature.py tests/identity/test_approval_authority.py`.
5. Single-tool task worker: `uv run pytest tests/a2a/test_tool_task.py tests/a2a/test_tool_task_dispatch.py tests/a2a/test_tool_task_requests.py`.
6. Tool-subset admission: `uv run pytest tests/a2a/test_admission.py`.

Scenario for the served agent: start `arbora` with the HTTP transport, fetch `/.well-known/agent-card.json`, then post a `message/send` request to `/a2a`. `tests/a2a/test_nonblocking_send.py` runs that scenario in process.

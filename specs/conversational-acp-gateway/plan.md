# GRAPHOS-ACP — Design and implementation plan

## Existing system and reuse

GraphOS already has one principal/tenant/scope resolution path consumed by REST and MCP ([GRAPHOS-IDENTITY-001](../identity-access/spec.md)). ACP admission must call that same path rather than re-implement verification. `graph_os.identity` is the existing owner of session and principal types. `graph_os.gateway` is the existing owner of serving composition. No ACP session store exists yet; `R001.1` adds the smallest typed model and admission check, not a transport.

## Admission and binding

A session-policy model records the configured idle and absolute limits and the current chat-adapter availability. An admission check takes the resolved principal (or its absence) and the adapter-availability flag, and returns one of: admitted (with an immutable principal/tenant/scope snapshot and both expiry timestamps computed at admission), a typed unauthenticated refusal, or the typed `chat_adapter_unavailable` refusal. No field of the admitted snapshot is mutable after admission; a later call must open a new session rather than widen an existing one.

## Idle and expiry policy

Idle expiry is `last_activity_at + idle_limit`; absolute expiry is `admitted_at + absolute_limit`. A touch/use call evaluates both against the current time before treating a session as usable and refuses with a distinct typed reason for each; it never resets the absolute limit and only advances `last_activity_at` on success.

## Adapter-unavailable refusal

The admission check and the per-message check both consult the same adapter-availability input and return the identical typed refusal code when it is unavailable, before any session is admitted or any reply is attempted. This is the explicit, intentional behavior while agent-utilities' chat adapter does not yet exist or is down — not an error path to be hidden.

## Live integration path

`R001.1` ships the typed model and admission check with no transport wiring. A later child slice wires it behind the actual ACP endpoint once agent-utilities' adapter is reachable. Existing identity resolution and status/doctor reporting are the integration points. No new identity issuer or session cookie is introduced.

## Quality and release gates

Focused tests run via `uv run pytest` against the new module only. Applicable hooks are the repository's existing `complexity-staged`, `kiss-staged`, `dupehound-changed` and `jscpd-differential` fast-subset checks on the changed files. No duplicated identity-resolution or session-store implementation; reuse `graph_os.identity` types where a shared shape already exists.

## Coding session admission (R001.3 to R001.8)

Data flow: `session/new` → `register_acp_routes` handler (new, `graph_os/acp/routes.py`) → principal from the existing identity path → `CodingSessionAdmission.admit` (extends `graph_os/acp/session_policy.py:101`) → `WorkspaceGrant` recorded on the session → admitted snapshot returned. Later: agent capability request → `permission_rule` → `session/request_permission` when needed → forward to the connected client → result back to the agent.

Types and fields: `CodingSessionRequest(workspace_root, client_capabilities)`, `WorkspaceGrant(root, capability, standing)`, `CodingSessionAdmitted(snapshot, grants)`. New refusal reasons extend `AcpRefusalReason` at `graph_os/acp/session_policy.py:21`.

Attach point and caller: the route is registered at `graph_os/gateway/graph_api.py:368` next to `register_dashboard_routes`; `register_graph_routes` is called from `graph_os/webui_host/webui_co_service.py:77`. The client channel is the connection that carried `session/new`.

Failure behavior: an untrusted root, an undeclared capability, a denied permission and a missing client each return a typed refusal. Graph OS never performs a file or terminal action itself.

Existing code:
`graph_os/acp/session_policy.py:89` (AcpSessionAdmission), `graph_os/acp/session_policy.py:101` (admit), `graph_os/acp/session_policy.py:120` (check_message), `graph_os/acp/session_policy.py:21` (AcpRefusalReason), `graph_os/acp/session_policy.py:68` (AcpSessionSnapshot), `graph_os/acp/__init__.py:3` (exports), `graph_os/gateway/graph_api.py:368`, `graph_os/identity/broker.py:256`. 
Reachable today: no. No production caller constructs `AcpSessionAdmission`. No ACP route or `session/new` handler exists. Search terms: `session/new`, `session_new`, `request_permission`, `read_text_file`, `trusted_root`, `AcpSessionAdmission`, `acp`.

Reused: session policy, expiry and the adapter-unavailable refusal from R001.1. New: workspace root, grants, capability forwarding and the route.

Cross-repository: `AU-CONTROL` (`ClientCapabilityToolset`) issues the capability calls through this session; `TUI-RUNTIME` (`CodingSessionView`) implements the client side.

Decision: the client performs file and terminal actions as ACP client capabilities.
Rejected: a Graph OS service that mounts the host checkout. It copies data and needs host credentials on the cluster.


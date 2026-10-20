# ARBORA-ACP — Design and implementation plan

## Existing system and reuse

Arbora already has one principal/tenant/scope resolution path consumed by REST and MCP ([ARBORA-IDENTITY-001](../identity-access/spec.md)). ACP admission must call that same path rather than re-implement verification. `arbora.identity` is the existing owner of session and principal types. `arbora.gateway` is the existing owner of serving composition. No ACP session store exists yet; `R001.1` adds the smallest typed model and admission check, not a transport.

## Admission and binding

A session-policy model records the configured idle and absolute limits and the current chat-adapter availability. An admission check takes the resolved principal (or its absence) and the adapter-availability flag, and returns one of: admitted (with an immutable principal/tenant/scope snapshot and both expiry timestamps computed at admission), a typed unauthenticated refusal, or the typed `chat_adapter_unavailable` refusal. No field of the admitted snapshot is mutable after admission; a later call must open a new session rather than widen an existing one.

## Idle and expiry policy

Idle expiry is `last_activity_at + idle_limit`; absolute expiry is `admitted_at + absolute_limit`. A touch/use call evaluates both against the current time before treating a session as usable and refuses with a distinct typed reason for each; it never resets the absolute limit and only advances `last_activity_at` on success.

## Adapter-unavailable refusal

The admission check and the per-message check both consult the same adapter-availability input and return the identical typed refusal code when it is unavailable, before any session is admitted or any reply is attempted. This is the explicit, intentional behavior while agent-utilities' chat adapter does not yet exist or is down — not an error path to be hidden.

## Live integration path

`R001.1` ships the typed model and admission check with no transport wiring. A later child slice wires it behind the actual ACP endpoint once agent-utilities' adapter is reachable. Existing identity resolution and status/doctor reporting are the integration points. No new identity issuer or session cookie is introduced.

## Quality and release gates

Focused tests run via `uv run pytest` against the new module only. Applicable hooks are the repository's existing `complexity-staged`, `kiss-staged`, `dupehound-changed` and `jscpd-differential` fast-subset checks on the changed files. No duplicated identity-resolution or session-store implementation; reuse `arbora.identity` types where a shared shape already exists.

## Coding session admission (R001.3 to R001.8)

Data flow: `session/new` → `register_acp_routes` handler (new, `arbora/acp/routes.py`) → principal from the existing identity path → `CodingSessionAdmission.admit` (extends `arbora/acp/session_policy.py:101`) → `WorkspaceGrant` recorded on the session → admitted snapshot returned. Later: agent capability request → `permission_rule` → `session/request_permission` when needed → forward to the connected client → result back to the agent.

Types and fields: `CodingSessionRequest(workspace_root, client_capabilities)`, `WorkspaceGrant(root, capability, standing)`, `CodingSessionAdmitted(snapshot, grants)`. New refusal reasons extend `AcpRefusalReason` at `arbora/acp/session_policy.py:21`.

Attach point and caller: the route is registered at `arbora/gateway/graph_api.py:368` next to `register_dashboard_routes`; `register_graph_routes` is called from `arbora/webui_host/webui_co_service.py:77`. The client channel is the connection that carried `session/new`.

Failure behavior: an untrusted root, an undeclared capability, a denied permission and a missing client each return a typed refusal. Arbora never performs a file or terminal action itself.

Existing code:
`arbora/acp/session_policy.py:89` (AcpSessionAdmission), `arbora/acp/session_policy.py:101` (admit), `arbora/acp/session_policy.py:120` (check_message), `arbora/acp/session_policy.py:21` (AcpRefusalReason), `arbora/acp/session_policy.py:68` (AcpSessionSnapshot), `arbora/acp/__init__.py:3` (exports), `arbora/gateway/graph_api.py:368`, `arbora/identity/broker.py:256`.
Reachable today: no. No production caller constructs `AcpSessionAdmission`. No ACP route or `session/new` handler exists. Search terms: `session/new`, `session_new`, `request_permission`, `read_text_file`, `trusted_root`, `AcpSessionAdmission`, `acp`.

Reused: session policy, expiry and the adapter-unavailable refusal from R001.1. New: workspace root, grants, capability forwarding and the route.

Cross-repository: `AU-CONTROL` (`ClientCapabilityToolset`) issues the capability calls through this session; `TUI-RUNTIME` (`CodingSessionView`) implements the client side.

Decision: the client performs file and terminal actions as ACP client capabilities.
Rejected: a Arbora service that mounts the host checkout. It copies data and needs host credentials on the cluster.

## Amendment 2026-10-17: bounded coding admission producer

Implement the frozen R001.3 types and a partial R001.4/R001.6 admission
boundary in `arbora/acp/coding_session.py`. Reuse `AcpSessionAdmission` for
identity binding, adapter availability and expiry. Match workspace root strings
exactly against configured trusted roots; do not resolve paths or access the
client filesystem. This comparison is admission policy, not path containment.
The connected client still owns containment for every eventual host effect.

The per-capability check allows only a declared read on a connected, live
session. Writes, terminal methods and permission requests return
`permission_required` until the real permission exchange exists. That reason
is distinct from `permission_denied`, which requires an actual client decision.
Grants remain empty. Admission and the admitted type accept no grants.
`WorkspaceGrant` describes the future record; constructing one issues no grant.

Focused tests cover immutable collections, exact root matching, and unknown or
undeclared methods. They also cover disconnect, adapter loss and both expiry limits.
They establish no served acceptance. Permission forwarding, standing grant
issuance, routes, traces and the hot path remain open under R001.4 through
R001.8. No local file/process execution or alternative transport is introduced.

## HTTP duplex transport and preview slice — 2026-10-20

R001.9 adds POST JSON-RPC and a GET SSE channel at `/api/acp`. A session is
an ephemeral connection bound to the existing authenticated HTTP caller and the
existing CodingSessionAdmission snapshot. It is not a second identity or
approval authority. `session/new` accepts `cwd` and `clientCapabilities` (the
closed capability method list); subsequent requests and SSE use the returned
sessionId in `Arbora-Acp-Session`. Server client-capability requests carry
JSON-RPC IDs. Client responses POST to the same URL. Each SSE record has a
strict connection-local sequence. Reconnect and Last-Event-ID replay refuse;
they cannot turn transient previews into durable unanswered approvals.

The prompt adapter calls AU's existing received-run-plan path with the bound
transport from AU-BOUNDARY-R043.6. It refuses any non-client toolset or native
tool selection, rather than exposing a server workspace. Missing AU support
refuses before admission. Trusted roots and idle/absolute limits come from
normal Arbora configuration. No trusted roots are configured by default.

Reads reach the connected client. A write first reads the original through
that client, emits a diff preview, and returns permission_required. Commands
emit their exact argv preview and return the same refusal. No write or terminal
request is forwarded, and no approval token or pending record is invented.
The canonical approval owner must supply atomic permission consumption and
durable continuation before these effects can be enabled.

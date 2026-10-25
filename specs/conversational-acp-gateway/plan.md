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

## Amendment 2026-10-25: one harness, two host profiles

Extend R001.5 and R001.9, not a second terminal agent. The default native
`tuig` client connects to central Arbora. An optional local runtime uses the
same Arbora admission and AU harness composition over ACP stdio; it must not
start an AU listener or copy an agent loop into the UI. AU-BOUNDARY-R015 owns
the host boundary, R043.11 owns client tools, and R043.9.1/R043.9.2 own
completed-history composition. The former AU-CONTROL links above are historical
routing references, not proof that a served adapter exists.

The audited producer baseline is Pydantic AI 2.57.0 and harness 0.57.0.
The [Pydantic ACP adapter](https://pydantic.dev/docs/ai/harness/acp/) lives
under `pydantic_ai_harness.experimental.acp` and can change or disappear without
deprecation. Its ACP extra requires `agent-client-protocol>=0.11,<0.12`.
Integration must qualify an exact version before enabling a host profile.
Calling its `Agent` directly is not an acceptable replacement for AU's
`RunRequest`, received limits, run events and history composition.

[ACP v1 transports](https://agentclientprotocol.com/protocol/v1/transports)
define newline-delimited stdio; Streamable HTTP is still a draft. R001.9's
JSON-RPC/SSE profile is therefore an explicitly experimental custom transport,
not a claim of standardized ACP HTTP conformance. A remote connection inherits
Arbora's verified identity and configured transport security. A local subprocess
is an explicit launch choice, never fallback after remote auth or transport
failure. Diagnostics never enter protocol stdout.

Before each remote message, compare the newly verified principal, tenant and
scope snapshot with the admitted snapshot. Missing authority and any changed
binding refuse without touching the session clock. This supplements the
existing immutable admission snapshot; immutability alone does not authenticate
later requests. `AcpSessionAdmission.check_bound_message` is the shared guard
for R001.9. It creates no credential or grant.

Acceptance remains pending until real subprocess stdio and authenticated remote
transport tests cover initialization, session identity, streamed updates,
permissions, cancellation, disconnect and malformed/duplicate messages.
Scripted model responses are permitted in those transport tests but must be
identified as such. In-process policy tests are not transport evidence.
Session reopening requires an admitted host-authorized history producer;
transient connection IDs and Pydantic's in-memory session store are not durable
recovery, effect replay, or restart-safe cursor evidence. No new store is added.

### R001.5/R001.9 bounded protocol projection

Use the ACP SDK's JSON-RPC codec and `Agent` interface with one Arbora-owned
protocol projection over AU `iterate_client_run`. Do not use Pydantic's
autonomous `PydanticAIACPAgent` run loop: it would bypass AU's received plans,
budgets and host-authorized history. Reuse Pydantic native stream event types
and harness presentation helpers, filtering out private reasoning and raw
model/tool data before client publication.

The first projection is text prompts and declared client reads only. Workspace
admission, immutable identity, session limits and typed refusals reuse the
existing policy. An injected trusted request factory supplies the admitted
`RunRequest` for every prompt; no factory means adapter unavailable. Session
IDs identify only a bounded, connection-local map, not durable conversations.
No implicit prior history is reconstructed; a factory must bind supported history
through the existing AU host contract. Unimplemented permission/effect dispatch,
MCP attachment, model switching, session loading and resume refuse explicitly.
The SDK stdio server delegates all execution to AU and closes active runs on
connection teardown. Network registration remains a separate authenticated
custom-transport slice; this projection alone does not enable a remote route.


### Explicit lightweight local profile

`arbora-acp` is an internal subprocess backend for the one user-facing `tuig`
CLI, not another terminal UI. It requires explicit local selection, model and
workspace root. It reuses `mint_local_process_session` and AU's received-plan
run, starts no engine, fleet or network listener, and never retries a remote
authentication failure locally. No credential or permission setting is changed.
The profile enables only explicitly selected client reads. Completed text
turns use the existing Pydantic `InMemorySessionStore` and `StoredSession`, one
bounded store per admitted session. An async completion hook saves the actual
AU terminal result before acknowledging success. The existing AU completed-text
codec receives the actual predecessor run ID; budgets remain fresh. Failed or
cancelled turns do not update history. Save failure blocks continuation rather
than replaying execution or guessing whether history committed. Close and
process teardown release the store. Session, turn and encoded-byte limits
refuse instead of truncating messages. This is process-local user/assistant text
only, not retained tool state, durable recovery or session/load support.
Installed-wheel and native-client qualification remain open.


The pinned SDK's experimental `session/close` is explicitly enabled and tested
on both stdio and HTTP/SSE. Other unsupported unstable methods still refuse in
the projection. This is not a durable session recovery or stable ACP extension
promise; EOF/disconnect remains connection teardown.

### Governed central final-answer projection

Central ACP must invoke the existing `agents.run` operation/HostedInvocation and
native owner/claim authority, preserving server tools and policies. The local
client-only runner is not a central substitute. `native_stream_scope` is trusted,
context-local observation of one verified session and one admitted native run;
it grants neither execution nor client tools. Copied contexts share revocation.
The central profile does not attach a native event observer. It preserves the
configured dependency plan and server tools, then projects only the committed
final answer. Central history remains absent: neither event rows nor transport
session IDs are continuation history. A second prompt refuses explicitly.

The existing task authority notifies an optional synchronous host callback after
it validates durable admission and before execution. The receipt contains the
actual task, run, context and work-item IDs. Client metadata cannot supply these
IDs. Callback failure blocks execution and uses the existing owner cancellation path.
The host allows five seconds for cancellation and checks owner-authorized readback.
It reports settlement unavailable if it cannot confirm the canceled state.
This never undoes admission or replays work. The central projection must also
handle cancellation during admission, before a receipt exists.


Generated status rows link source commits and test bindings. They do not prove
installed release compatibility or prove live owner storage and provider behavior.
The client-workspace preview profile does not replace central server tools.

`HostedInvocation.configure_acp` installs the opt-in central factory with explicit
trusted roots and the existing configured native owner. Without it the route
returns 503. No deployment settings or identity rules change. The factory invokes
`agents.run` through the existing authenticated operation path and retains its
actual task/run receipt. Nonterminal tasks return their state, not a completed
answer. Cancellation requires owner cancellation and readback; an unknown outcome
returns a distinct refusal, never a successful cancellation response. Capacity and
shutdown waits are bounded. Unresolved shutdown reports uncertainty without
canceling the pending admission callback. Process termination cannot prove
settlement. Late admission after disconnect requests settlement
before execution, with no replay.

Each HTTP message authenticates through the existing HostedInvocation authority.
A renewed token with the same verified authorization profile updates the
connection's current caller. Subsequent prompts and cancellation use that caller,
not headers retained from connection creation. The profile retains actor kind,
roles, groups, graph, audience, policy and verified operation claims. It also
pins the trusted JWT verifier configuration. Token expiry and request IDs may
rotate; authorization changes refuse. Renewal does not extend absolute
connection limits or revive a closed session. Changed authority still refuses.
Central token streaming, continuation and installed-wheel acceptance remain open.

# ARBORA-HOST — Implementation plan

## Summary

Delivery phase: P1

Its rows serve P1: the scenario `ARBORA-ACCEPTANCE-R006.1` enters through the host that this spec composes.

This plan serves the rows of `ARBORA-HOST` in requirements.md. It covers arbora host composition and ownership boundary. The design record follows in the sections below.

## Technical Context

- **Language/Version**: Python 3.14, with a 3.12 floor
- **Primary Dependencies**: the arbora served registry and the Medulla client adapter from the SDK
- **Storage**: N/A, because the engine owns every fact
- **Testing**: pytest with one bound test per row, at the proof level the row names
- **Target Platform**: Linux service
- **Project Type**: service
- **Performance Goals**: N/A, because rows state their own limits
- **Constraints**: one door, and the engine owns every decision
- **Scale/Scope**: the rows of this spec only

## Constitution Check

- R-I One door, no policy table: pass. Every decision is read from the engine.
- R-II Every module is reachable from a production caller: pass. Each row names its caller chain.
- R-III Public specs and core skills are complete and self-contained: pass.

## Project Structure

- `specs/host-composition-boundary/` (existing): spec, requirements, plan, tasks, test-spec and checklist.
- Code paths: each row names its files in its `Shape:` sentence. This plan adds no other path.

## Phase 0: Research

No unknowns remain. The sections below record the earlier research and decisions.

## Phase 1: Design

The sections below hold the architecture, ownership, data flow and failure modes of this spec.

### Existing wiring to reuse

| Arbora module | Existing responsibility | Required change |
|---|---|---|
| `arbora.mcp_server.server`, `runtime`, `composition`, `bootstrap` | FastMCP entrypoint, lifecycle and composition | Keep one loop and one multiplexer. Move dependency-specific construction behind public ports and fail closed on unavailable contracts. |
| `arbora.gateway` | REST routes, catalog and widget projection | Adapt routes to the same application operation and authorization decision as MCP. Keep vendor calls in admitted fleet tools. |
| `arbora.a2a` | Agent Card and task projection | Reuse the host application service, never create a task/event store. |
| `arbora.webui_host` | Optional WebUI co-service | Submit to owner loop; keep browser presentation in the WebUI package. |
| `arbora.control_plane` | Policy and reconciliation orchestration | Keep hosted decisions here while durable provenance and graph state use the engine client. |
| `arbora.deployment` | Configuration and operator commands | Reuse one XDG configuration resolver and publicly installable extras. |

`arbora.mcp_server.bootstrap` and `arbora.a2a.routing` currently contain dependency-internal imports. Treat them as migration inventory, not approved architecture. A port is a narrow protocol describing the methods Arbora actually calls; its adapter imports the dependency's supported public API. If a public method is missing, add it to the owning package. Pin a released version before deleting the old import. Do not mirror the private implementation in Arbora.

### Request path

```mermaid
flowchart LR
  C[Client] --> B[Transport identity and admission]
  B --> P[Shared action policy]
  P --> S[Typed application operation]
  S --> E[Graph engine client]
  S --> A[Agent runtime public API]
  S --> K[Connector SDK runner]
  S --> R[One result and receipt mapper]
  R --> C
```

The transport adapter parses and bounds a payload, authenticates identity, applies origin/CSRF requirements where applicable, and constructs an immutable request. `action`, `tenant`, `resource`, `scopes`, `policy_version`, `idempotency_key` and `correlation_id` are typed; the trusted principal comes from transport admission. Policy runs before an effect or delegated call. The operation returns typed data or a typed error; transport adapters only encode them. Redacted structured logs carry the correlation and receipt identity, never secrets.

### Migration sequence

1. Inventory each Arbora-to-AU import and each duplicate host/module. Label its owner and the public port it needs. Cover eager, lazy and type-only imports.
2. Add missing public dependency ports in their owners and test them through installed wheels. Replace Arbora call sites by capability, beginning with identity/config, graph client and agent execution; no package-wide compatibility alias.
3. Reconcile gateway, deployment, MCP, fleet, A2A and messaging copies against the public behavior tests. Delete old duplicate script registration after parity.
4. Make owner-loop submission a single service and verify co-service lifecycle, reload and cancellation under concurrent sessions.
5. Run the boundary census and served parity tests on a clean environment; record exact package versions and source hashes in `tasks.md`.

### Design constraints

New adapters remain small and cohesive. Before adding a class, inspect an existing `arbora.*` port and route; extend the owning module where its contract fits. Avoid duplicate decision trees, hand-maintained action catalogs, hidden fallback authority and broad `except` that turns denial into success. A dependency contract mismatch returns a typed unavailable error and an observable receipt. A temporary migration shim must have one caller, one removal task and a test preventing expansion.

Cross-repository work is sequenced by public package release: engine/SDK/AU contract first, Arbora adapter second, client projection last. Public dependency versions in `pyproject.toml` and `uv.lock` express the minimum required API; no unpublished sibling path is a required test input.

### Startup configuration design (ARBORA-HOST-R010)

Existing code: `core/config_admin.py:51` reads four hosting variables and validates ports and profiles.
`mcp_server/server.py:164` currently loads agent-runtime configuration before constructing the server.
Reachable today: the four-field helper exists; complete declaration composition and startup validation are missing.

The proposed immutable `HostEnvironmentManifest` uses schema version 1 and an ordered declaration list.
Each declaration carries `name`, `component`, `source_revision`, `type`, `required_when`, `default`, `sensitive` and `call_sites`.
Sort by component and name; reject conflicting duplicate names rather than depending on import order.
Types are string, boolean, integer, URI, path and secret reference, with explicit bounds and enum values.
Require public declaration producers for enabled SDK and agent-runtime components before claiming complete ecosystem coverage.
Those producer contracts are prerequisites; the current four-field helper does not provide them.

Resolve configuration once, validate, then bind an immutable snapshot before constructing transports or starting children.
Retain existing XDG precedence; this definition grants no new environment override or permission source.
Named reasons are `environment_undeclared`, `environment_missing`, `environment_malformed`, `environment_conflict` and `environment_manifest_unavailable`.
Return names and reasons without resolved values. Validate ports against 1 through 65535.
Use seeded declaration ordering permutations to prove identical normalized manifest digests and refusal sets.

Preservation: R010.1 retains the complete original ownership and schema acceptance.
All original rows and acceptance remain binding; narrower startup children cannot replace their evidence.

### Detected harness installation (ARBORA-HOST-R008)

Existing code: `deployment/cli.py:67` registers one harness; `:77` delegates a fence installer to agent-runtime internals.
`core/` currently contains the configuration owner, not a complete generic installer or harness detector.
Reachable today: the existing registration command is callable; generic detected-only installation is missing.
Extend that command with a proposed `install-harnesses` subcommand rather than creating another installer service.
Use versioned adapters for detection, parsing, managed-section edits and supported hooks.
Support absence explicitly; do not create a harness directory merely because its adapter exists.

`HarnessInstallation` schema version 1 records adapter, version, executable identity, configuration root and positive detection evidence.
`HarnessConfigurationChange` records destination, before digest, proposed bytes digest, managed sections and installation identity.
Validate roots and all proposed artifacts before applying; use conditional replacement with idempotent managed sections.
Recheck each original digest immediately before replacement. Preserve unknown keys, comments and unrelated instructions.
Multi-file interruption reports which artifacts changed; resume by reading actual digests, never by claiming an atomic transaction.
Reasons are `harness_absent`, `harness_unsupported`, `harness_ambiguous`, `configuration_conflict`, `destination_denied` and `installation_incomplete`.
An absent harness produces an observation without writes; unsupported requested configuration produces a refusal.
Supported-hook absence is reported as a capability limitation, never silently described as installed.
R008.1 preserves the complete original module census and every named owner.

### Terminal start and scoped logs (ARBORA-HOST-R009/R010)

#### Owned shutdown prerequisite

ARBORA-HOST-R009.4.1 adds targeted supervisor shutdown. Stop only the named
service; preserve borrowed siblings. All-service shutdown uses one monotonic
deadline, retains live handles on timeout, and permits a later cleanup retry.
Loopback HTTP tests verify the surviving sibling and bounded shutdown behavior.

Combined startup needs two composition contracts.
First, an authoritative `host.start` grant must bind session, tenant, principal,
canonical workspace, interfaces, policy revision and expiry.
Second, a WebUI factory must accept the provided host authority without resolving global runtime state.
A `host.logs.read` grant cannot authorize startup.
The next increment must validate the start grant before constructing any graph or listener.
It must pass the same authority into HTTP and WebUI composition.
Existing launcher refusals remain until composition uses both contracts and tests verify them.

Existing code: `agent_host/__init__.py:192` starts HTTP synchronously and explicitly excludes the terminal launcher.
`mcp_server/composition.py:65` owns `CoServiceSupervisor`; `:230` starts existing co-services.
`webui_host/webui_co_service.py:104` provides the hosted web service and stop-event lifecycle.
Reachable today: HTTP hosting and supervised web threads exist; terminal start selection and log handoff are missing.

Add `HostLaunchRequest` version 1 with `workspace`, `interfaces`, `web_logs`, `configuration_digest` and authenticated start context.
`interfaces` is exactly `terminal`, `web` or `both`; expose only `--terminal` and `--web` selection flags.
`--terminal --web` chooses both. With neither flag, retain the existing command behavior.
`web_logs` controls the terminal log subscription; it cannot disable the host's required redacted log sink.
Reject unknown fields. Resolve client availability and workspace authority before opening a listener.

Run terminal-mode HTTP in a supervised background thread; retain the single MCP owner loop and multiplexer.
Co-services submit operations to that loop; never block it waiting on another loop or create another authority.
Keep authenticated host context in the background thread; each network request still authenticates its own caller.
The foreground terminal consumes only public start and log contracts, never agent-runtime internals.
`HostLaunchReceipt` version 1 carries host instance, interface mode, endpoint, workspace binding, log reference and readiness observations.
These are proposed contracts, not existing exports or readiness claims.

Use `<workspace>/agent_data/logs/server.log`, with owner-only file access and validated parent directories.
Do not follow escaping symlinks. Reject incompatible ownership before opening the file.
Default bounds are 10 MiB per file, five retained files and 16 KiB per redacted record.
Sequences increase per host instance; rotation increments a generation and exposes retained sequence bounds.
A shared workspace's `server.log` is host-internal; never pass its descriptor to a tenant client.
Tenant tags inside records do not make a shared descriptor safe.
A descriptor is permitted only for a tenant-exclusive projection whose entire readable contents belong to that authenticated tenant.
Otherwise provide an authenticated filtered stream or opaque reference, bound to tenant, principal, workspace, host instance and generation.
Authorize each descriptor grant and every stream/reference read or reconnect; filter before encoding or sending any bytes.
Descriptor grants contain bounded immutable snapshots; new records require a newly authorized grant.
Never accept a client-provided tenant or workspace as authority. Revocation and expiry end further delivery.
Private projections require tenant-specific access control even when two tenants use the same workspace.
If filesystem access cannot enforce that separation, refuse descriptor delivery and use the authenticated filtered path.
Bind rotations to the same tenant; do not expose another tenant's records, aggregate file offsets or shared sequence metadata.
Expose tenant-local sequence and gap metadata without revealing other tenants' activity.
Record tenant, workspace, host instance, request, sequence, timestamp, severity and redacted message in the host-internal sink.
Filter secrets and raw request bodies before writing; protocol stdout and terminal rendering receive no host log output.
Use bounded delivery; a slow reader must not block serving or create an unbounded queue.
Return an explicit gap when requested history was rotated. Shutdown drains logs and joins owned HTTP threads within five seconds.

Reasons are `invalid_launch`, `client_unavailable`, `workspace_denied`, `log_unavailable`, `log_gap`, `bind_failed` and `shutdown_incomplete`.
Preserve R009.1's original A2A, task-lifecycle and ACP acceptance; logs establish none of those independently.
The terminal consumer is specified by TUIG-RUNTIME-R002.2 through R002.6.
Bind R009.5, R009.6 and R010.7 to the public adapters in the amendment below.
Consumer definitions are available; their runtime exports and the authenticated host handoff remain implementation prerequisites.

### Shared transport scaffold (ARBORA-HOST-R006/R023)

Existing code: `mcp_server/server.py:164` exposes standard transport arguments and a shared fleet lifecycle.
`runtime._build_server:2208` still imports the agent-runtime server factory; existing R007 owns replacement of that private coupling.
Reachable today: both transport branches exist; this does not establish served parity or complete scaffold conformance.
Keep one public Arbora entrypoint, one registry and one multiplexer. Do not restore a standalone multiplexer server.
Use typed arguments for transport, bind address, port and security; compare normalized served catalogs under one registry generation.
R006.1 retains all original module ownership; R023.1 retains every tool, ranking, fallback and invocation-policy clause.
The new transport acceptance must use real wire clients, including unauthorized calls and stdout-purity probes.

### Native desktop, visual state, and coding-agent handoff (ARBORA-HOST-R029–R031)

- **R029:** After the SDK publishes a typed grant-bound port and certified provider, bind it through `arbora/api/invoke/pipeline.py:invoke` and `arbora/api/invoke/executor.py:BoundOperationRuntime.dispatch` (line 273). `OperationRuntime.dispatch` is only a protocol.
- **R030:** Bind observation and state through the same composed dispatch path. Do not add a local state store. Wait for SDK observation and EG state/context contracts.
- **R031:** Use `arbora/mcp_server/server.py:mcp_server` for host/admission composition. Wait for EG selection, AU harness, and WebUI renderer contracts; do not advertise the complete flow before all ports exist.

### Amendment: authenticated terminal log preference

Extend ARBORA-HOST-R009.5, ARBORA-HOST-R009.6 and ARBORA-HOST-R010.7.
The consumer contract is [TUIG-RUNTIME-R002.2 through R002.6](https://github.com/Knuckles-Team/tuig/blob/main/specs/terminal-runtime/plan.md).
This amendment provides definitions only. Keep these host rows SPECIFIED with empty acceptance receipts.

The proposed HostLaunchReceipt version 1 must include required `web_logs: bool`.
It is the effective preference resolved from HostLaunchRequest.web_logs and authorized host configuration.
Use a strict boolean; missing, null and nonboolean values are invalid. Never infer true from a log reference.
Bind this value and the entire receipt to the authenticated host session, host instance, workspace, tenant and principal.
Retain the endpoint, interface mode, readiness, opaque log reference, generation and expiry bindings.
Deliver the receipt through the existing authenticated host/client handoff. Receipt fields alone grant no authority.
A changed binding, unknown version, expired session or missing preference refuses subscription before any log read.
The current runtime has no receipt field or served handoff proving this binding. Until implemented, log viewing stays unavailable and performs zero reads.
Do not fill this gap with an unsigned preference, client-selected scope or filesystem path.

R010.7 resolves terminal, web or both before startup and passes the authenticated effective preference to R009.5.
R009.5 provides `run_host_terminal(receipt: HostLaunchReceipt, *, client: AgentClient, log_reader: HostLogReader | None) -> None`.
Inject the existing authenticated client and the Tuig-defined `HostLogReader` implementation.
Its asynchronous method is `read(log_ref: str, *, cursor: str | None, limit: int) -> HostLogBatch`.
Arbora implements the authorized producer side under R009.3 and R009.4; TUI owns rendering, subscription and cancellation under R002.3 through R002.5.
The reader uses only the opaque reference and reauthorizes each read and reconnect. No new route or file reader is implied.
These signatures are specified consumer interfaces, not claims of existing runtime exports.

Only terminal and both modes with authenticated `web_logs=true` permit TUI log reads.
False or absent preference, web mode, unavailable reader or invalid scope yields zero TUI reads.
Agent interaction remains available when log viewing is disabled or unavailable.
The required redacted host sink stays enabled in every case; the preference controls only the TUI subscription.
Retain all existing tenant isolation, bounded delivery, rotation gaps, shutdown and single-authority requirements.
R009.6 proves this through the real host and public adapter together with TUIG-RUNTIME-R002.6.
A controlled adapter can prove only the host handoff, not unified terminal rendering.

### Additional owner requirements

- `ARBORA-HOST-R023.3`: Plan the owner work and dependencies defined in requirements.md.
- `ARBORA-HOST-R008.6`: Plan the owner work and dependencies defined in requirements.md.
- `ARBORA-HOST-R026.6`: Plan the owner work and dependencies defined in requirements.md.
- `ARBORA-HOST-R022.1`: Plan the owner work and dependencies defined in requirements.md.
- `ARBORA-HOST-R010.8`: Plan the owner work and dependencies defined in requirements.md.
- `ARBORA-HOST-R032`: Plan the owner work and dependencies defined in requirements.md.

#### 2026-10-16 — Authorized natural-language fallback

R023 fallback uses the registered `query.nl` operation and shared invocation pipeline under `query:nl`. Delete the synthetic descriptor and direct callback path. Absent or unauthorized query operations refuse before dispatch. Preserve all eight tools and ranking behavior.

#### 2026-10-17: Refuse unavailable interface requests before startup

R009.2 through R009.6 and R010.7 still lack a verified workspace/session start
authority, authenticated HostLaunchReceipt and tenant-filtered HostLogReader.
Do not derive workspace access from generic caller scopes or WebUI AccessContext.
The connector create_agent_server absorbs enable_terminal_ui, enable_web_ui and enable_web_logs.
It does not provide those capabilities.
Reject explicit enabled requests with client_unavailable before graph creation
or listener startup. Reject non-boolean selections with `invalid_launch`.
Omitted, false and the existing optional web default preserve ordinary HTTP
hosting. This validates refusal only; interface selection, scoped log delivery,
rotation, reconnect and the served host/terminal acceptance remain pending.

#### 2026-10-17: Injected workspace log policy (R009.7)

No current EG contract represents host filesystem workspace grants.
Reuse Arbora SessionAuthority for current owner-verified caller, session reference and expiry.
Trusted composition provides a WorkspaceGrantProvider; missing composition denies every read.
The provider returns an exact tenant, principal, session, workspace, action, policy revision, grant revision and expiry.
No generic scope, request field or WebUI context creates a grant.
HostLogAccess binds opaque references to these facts and one configured host instance.
Each read resolves current session and policy before and after awaiting its record source.
Changed bindings, revocation and expiry suppress delivery. Bounds limit references, batches, records and response time.
The existing connector host registers optional reference and read adapters under /host/logs.
The configured source must provide redacted tenant projections and tenant-local opaque cursors; shared file offsets remain forbidden.
Controlled HTTP fixtures prove this composition contract, not deployed grants, filesystem logging, rotation or terminal rendering.
No persistent credential store, local grant issuer or production configuration is added.
R009.2 through R009.6 and R010.7 retain their full served acceptance.

#### 2026-10-17: Required rotating sink and private projections (R009.8)

HostLogRuntime composes the existing session and grant owners with WorkspaceLogSink.
The existing host builder attaches this optional runtime before dispatch.
An authenticated request writes a fixed arrival event before its handler runs.
Bodies, query strings and credential headers never enter the sink.
Sink recording remains active when terminal log viewing is disabled.
Validate ownership, modes and links before opening files relative to a retained directory descriptor.
Rotate at ten MiB and retain five files. Redact records through the existing AU privacy port before writing.
Keep bounded tenant projections and opaque tenant-local cursors; never return shared files or offsets.
This slice uses controlled identity grants with a real sink and HTTP server.
It does not prove deployed policy, durable replay after restart, terminal rendering or full host lifecycle acceptance.

#### Source generation admission

R009.8.1 extends WorkspaceGrantProvider decisions with explicit source generations. Caller session identity cannot establish generation entitlement. R009.8.2 retains durable replay and metadata acceptance.

#### Retained host-file replay

Reuse the five bounded owner-only log files and current exclusive sink lock.
A private integrity key binds resume positions; it grants no read authority.
Require fresh authenticated workspace/source-generation admission on every restart.
Keep version-one message projection separate from full metadata presentation.

#### Startup admission port

R010.7.1 adds injected host.start policy and session-binding owners.
The binding owner attests the exact GraphSession object and current SessionState.
Actor fields and scopes cannot establish that binding.
Admission refreshes identity after awaited owner calls and verifies explicit grant expiry.
The connector entrypoint verifies the requested mode, workspace and preference before admission.
It still refuses client startup until R010.7.2 provides shared-authority client factories.
No deployed grant, credential configuration or listener is added by this increment.

The admission request binds a configuration digest to the policy decision only.
This slice does not compute that digest from graph configuration or prove their agreement.
R010.7.2 must compute the digest from the actual start configuration before any startup effect.

#### Actual declarative startup configuration (2026-10-17)

R010.7.2.1 binds explicit builder inputs, listener settings, workspace and host metadata before calling admission owners.
Canonical JSON rejects unsupported values and nonfinite numbers. Mapping order does not change the digest.
AU execution dictionaries contain live engines, nodes and toolsets; their topology cache key cannot prove startup configuration identity.
This slice rejects provided runtime objects and unused hosting arguments when callers request admission.
R010.7.2.2 retains owner-attested runtime configuration and actual terminal/WebUI factory integration.
Existing unsupported interface refusals remain after successful admission. Controlled owner tests establish boundary behavior, not deployed grants or served clients.

#### 2026-10-17: Versioned retained-log presentation

R009.8.2.2 adds an explicit `version: 2` selector to the existing read request.
The response contains version, records, cursor and gap. Each record carries the
original host_instance, sequence, request, timestamp, severity and message.
Version one remains message-only. No tenant or workspace identifiers are added
to records. Fresh authority and opaque cursor bindings remain.
Both clients require version two, render metadata literally and distinguish the
exact HTTP 409 retention_gap response from refusal. A gap stops polling and
clears protected content; it never resets a cursor or retries automatically.

#### 2026-10-17: Live admission qualification remains open

R010.7.2.2 is not qualified by the metadata work. The WebUI API item 06.1 is available
at merged revision 6a7df660e0048d7453dff2e294c50f7882a5cec8; version 2.7.0 alone
does not establish that contract. Its engine provider returns a borrowed engine
and grants no session authority.

A controlled isolated native engine constructed and created an in-memory graph.
This does not prove admitted client startup. The current startup guard refuses
provided runtime objects before admission owners run. HostSessionBinding binds
GraphSession but does not attest provided engine/configuration ownership. The
qualified session resolver adapter remains unavailable. Acceptance needs a served local fixture with that binding and an explicit session resolver.
Real terminal and WebUI clients must share it. Test readiness, revocation and
borrowed-resource lifetime. No production access or
weakened authentication is an acceptable substitute.


#### Runtime ownership admission (2026-10-17)

Extend the existing construction owner's session binding to cover exact borrowed graph, engine, execution configuration and callback resources.
The owner attests its current configuration revision and the canonical declarative digest; callers cannot mint these facts.
Validate the actual digest before owner calls and after their awaits. Compare resource identity and owner revision across attestations.
A frozen admitted bundle carries authorized resources to the launcher. It transfers no ownership and creates no engine or grant.
Opaque resource internals remain the construction owner's responsibility; object identity alone never establishes permission.
The remaining child retains real client startup and qualified session integration acceptance.

Refresh the owner binding after final session resolution, so that await cannot hide a changed runtime revision.
Accept increased idle expiry only when identity, session reference and MFA facts remain exact. Keep the original admission expiry ceiling.
Shortened expiry or changed authority denies; construction-owner state snapshots follow the same rule.

#### Launcher bundle attachment

ARBORA-HOST-R010.7.2.2.1 connects the existing runtime admission to
create_agent_server through an explicit host_runtime argument. Legacy resource
arguments may be omitted or refer to the same borrowed objects. Conflicts and
unknown arguments refuse before owner calls. No resources are constructed or
closed by this attachment. ARBORA-HOST-R010.7.2.2.2 remains pending.
Missing parts include host.start policy, construction owners, browser caller binding
and authenticated client lifetime composition.

### 2026-10-18 borrowed runtime lifespan

R010.7.2.2.2.1 reuses the checked start and existing owner ports at ASGI startup.
It adds no provider, policy or UI import. R010.7.2.2.2.2 retains live client acceptance.

### 2026-10-18 R010.7.1 access-operation adapter assessment

The existing operations cannot yet provide an authoritative HostStartGrant.
`arbora/access/service.py:check_access` delegates graph access to CheckAccess
and returns only allowed. EG AccessDecision binds agent, graph and read/write
access; it carries no session, workspace, configuration or grant lifetime.
RbacElevation remains graph-access elevation, not a host-start grant.

ActionPolicy PolicyReceipt binds a request digest, disposition, origin and
optional approval id. It does not expose a current exact-session grant with
policy revision, grant revision and expiry. Hashing host-start fields into its
request does not establish those missing authoritative facts.

EG IssueControlLease can store all required fields in its immutable grant map,
with tenant checks, revision and expiry. These fields represent storage facts.
Generic lease permissions and principal-to-kind restrictions do not establish
a qualified host-start issuer.
Arbora's generic approval handlers are also not registered while their required
approval scopes are absent. No existing host-start grant kind, issuer binding
or authoritative projection is wired here. Do not turn a caller-authored lease
map into authority merely because its fields match.

Smallest follow-up under R010.7.1 and ARBORA-IDENTITY-R019: specify the host-start
projection of the existing access owner. Bind its verified issuer and permitted
lease kind. Derive tenant, principal and session from current authority.
Validate workspace, interfaces, web_logs and actual configuration digest.
Return existing HostStartGrant fields including current policy/grant revisions and expiry.
Use the existing native control-lease lifecycle if its issuer restriction is
qualified, without a new lease store. Then implement HostStartGrantProvider as
that owner's consumer, refreshing status/revision/expiry before startup and
re-admission. Until this binding exists, selected interfaces remain unavailable.

This amendment adds no approval policy, role assignment, universal MFA, new TTL,
single-use requirement or live issuer. Preserve human/service-account parity,
existing conditional approval rules, replay-safe restart admission and the
existing five-second ownership-aware shutdown bound. Ordinary no-auth loopback
validation retains bootstrap principal, normal scopes/RBAC, Host/Origin checks
and diagnostics independently of selected-interface grants.


#### Reviewed issuer privilege gap

Independent architecture review rejects a raw ControlLeaseView-to-HostStartGrant
adapter. Generic lease:write can issue a caller-chosen kind; principals absent
from ControlLeaseKindPolicy's restriction map are permitted. ControlLeaseView
contains no authenticated issuer provenance. host.start is currently the action literal required by this host contract. The inspected EG scope registry does not enforce it as a scope. Treating lease:write as host-start issuance introduces a new privilege decision.

The specific pending decision is which existing access-owner permission may
issue host-start authority, with an enforceable issuer-to-kind binding. This
need not introduce a new scope, and does not require approval on every use of
an already valid grant. No particular principal, new approval policy or deployed
permission is selected here.

The existing provider protocol now states fresh exact-read/provenance duties
and requires lease identity plus revision for continuity. Adversarial fixtures reject generic lease maps even when every caller-provided field matches.
ASGI lifespan fixtures reject changed grant revision, session, configuration or expiry before readiness. They do not qualify a production issuer or consumer adapter.

#### 2026-10-22: bounded valid RunEvent JSON

Under ARBORA-HOST-R001.2.1, the run-event sink preserves each complete event
mapping in valid JSON with a shared 1 MiB serialized payload ceiling. It never
cuts encoded JSON or rewrites arbitrary event fields. ASCII escaping preserves
Unicode exactly and gives the sink and reader a byte-exact limit. Events that
cannot serialize as valid JSON or exceed the bound persist an explicit bounded
payload-error representation with a privacy-safe diagnostic. They never persist partial JSON. Engine write
refusals remain visible as missing durable output, never a local substitute.

The A2A reply projection retains its independent 65,536-character bound. The
1 MiB envelope admits the worst-case escaped representation of every reply
within that bound plus normal terminal metadata. Larger replies are unavailable
as artifacts rather than silently shown as complete prefixes. This change does
not add another event store or qualify streaming, terminal receipts or history.

##### Explicit payload-error amendment

The 1 MiB bound does not discard an oversized or unserializable event. Persist a
versioned `arbora-run-event-payload-error-v1` JSON representation with the original
`type`, `run_id`, `step`, optional bounded terminal `status`, and
`payload_error: {code, max_bytes}`. The code is `payload_too_large` or
`payload_serialization_failed`; `max_bytes` is 1,048,576. No `result`, output prefix,
or invented execution outcome is included. Canonical identity fields must be
nonempty bounded strings (run ID 512 characters, type 64); invalid identities
refuse instead of being shortened. This marker records an observed event whose
body is unavailable, not a replacement AU event or native terminal receipt.

#### 2026-10-22: Shared task graph evaluation

`ARBORA-HOST-R001.2.1.1` adds an opt-in HostedInvocation evaluation boundary.
It reuses operation admission and the configured caller client, with immutable
host limits and resident schema/policy references. EG verifies graph content;
Arbora does not reinterpret a content digest as execution authority.
`ARBORA-HOST-R001.2.1.2` retains durable run-fenced publication, planner budget
accounting, AU translation and serving-start configuration as required work.


#### 2026-10-22: Executable-plan publication prerequisite

`ARBORA-HOST-R001.2.1.2` remains blocked. The served path is `agents.run` ->
`WorkItemA2AAuthority._run` -> `AgentRunPort` -> AU `AgentRunExecutor` ->
`agent_runs.decide_run`. The claim remains local to `_run`; AU receives only
the session and the provider returns the single-step default.

Reuse EG's native `SubmitWorkItemRequest.run_id` owner binding and
lease-fenced current-plan transaction. `PublishCurrentPlan` / `GetCurrentPlan`
carry only scaffold text/status, not an executable DAG. The missing producer contract must atomically bind and read back canonical content under that owner and fence. This content includes tasks, edges, output, limits and plan/policy digests. Do not invent a RunCoordinator or encode a DAG in scaffold text. Do not treat evaluation receipts, statistical logs, RunEvents or terminal result references as admission.
Arbora currently submits a chosen WorkItem id without run_id; native run
admission forbids chosen ids. Preserve A2A replay identity when changing this.
Then account for planner usage, reauthorize, publish durably before execution,
and map the returned plan to AU DependencyPlan. Explicit dependency requests
remain unavailable; this amendment adds no mode or silent serial fallback.


Producer attachment points are EG's `crates/medulla-types/src/current_plan.rs`,
`src/redb_store/work_item/current_plan.rs::{publish,read_current}`, and
`src/redb_store/work_item/run_owner.rs::admit_owner`. The native publication request carries tenant, worker_id, lease_epoch, fencing_token and idempotency_key. Authenticated principal and graph come from admission.
`src/server/mutation_batch/commit/work_item.rs` commits through the existing
locked graph mutation batch. `src/server/handlers/work_item_read.rs` reads
current-plan owner, lease and payload from the authoritative snapshot.
The executable-plan extension must preserve these owners and transaction
boundaries, not add an Arbora store or substitute the scaffold payload.


#### 2026-10-22: Shared planner consumption accounting

`ARBORA-HOST-R001.2.1.2` passes measured planner usage and elapsed time to
AU RunPlan initial consumption, retaining the original host-bounded limits.
AU owns aggregate enforcement and result/event accounting; Arbora must not
subtract consumption from limits or add it again to returned usage. Existing
attached-plan retries remain fail closed until protected receipt reuse and
private fence renewal are validated without reconstructing publication times.

Scoped source tests verify original limits, measured initial consumption and
matching aggregate result/event usage. Native first owner submission and claim pass against the rebuilt engine. Replay still fails with an idempotency conflict because the request context timestamps change. Resolve replay via an Medulla-owned protected lookup by admitted run identity. Validate caller, request digest and native run binding. Do not guess engine-assigned ids or timestamps.
The complete native submit/retry/get/list/cancel proof has not passed.

Initial publication uses EG's required empty revision zero. The controlled
transport regression checks this boundary; native publication remains unproven.

#### 2026-10-22: preserve decision receipt wire aliases

ARBORA-HOST-R001.2.1.1 preserves generated-model wire aliases when passing
decoded decisions to EG receipt verification. Nonempty premise records retain
`class`, never Python's `class_`. A typed captured native response regression
checks the strict verifier; native first-dispatch acceptance remains separate.

The 45 focused tests pass with the actual EG strict verifier and a captured
native response containing eleven premises. Native first dispatch now passes
evaluation and reaches publication, which refuses `ACCESS_DENIED: current-plan
owner unavailable`. No plan or child execution was admitted. WorkItem admission
uses the control graph while publication selects the caller graph. Carry the owner graph through the private run context with existing checks.


#### 2026-10-22: Native owner graph binding

`ARBORA-HOST-R001.2.1.2` binds the admitted WorkItem engine graph in the
private native run scope. Current-plan reads and publication use that graph,
not the caller session's possibly different default graph. Caller identity,
policy revalidation and EG graph capabilities remain mandatory. Model input
cannot select the graph; missing native graph bindings fail closed.
#### 2026-10-22: bounded run-event writer decomposition

Under ARBORA-HOST-R001.2.1, extract the unchanged event identity validation and
bounded JSON/error-marker encoding from the persistence dispatcher. Preserve
engine selection precedence, diagnostic behavior, the 1 MiB bound and the
versioned payload-error representation. CCCC 1.6.0 reports
`record_run_event` cyclomatic/cognitive complexity reduced from 16/13 to 6/6;
the identity and encoding helpers measure 6/1 and 6/6 respectively. The focused
payload, agent-run and A2A read suites pass 84 tests; scoped Ruff checks pass.

Scoped mypy (`--follow-imports=silent`) passes for both changed source files.

### Amendment: temporary request-loop clients (2026-10-23)

ARBORA-HOST-R001.2.3 keeps serving-loop clients separate from temporary
request-loop clients. The synchronous bridge owns each temporary client.
It closes that client on its own loop before the loop exits. Repeated calls,
failed requests and cancellation must not displace a serving-loop reader.
The lifecycle tests check reader completion and repeated close behavior.

#### 2026-10-20 — Dedicated managed-start provenance

R010.7.1 uses the native ControlLease owner through `IssueHostStartGrant`, guarded
by `host:issue-start`. The engine records issuer provenance outside caller JSON.
Generic lease issuance cannot create a qualifying `host.start` record.
The issuer reference is the existing native caller fingerprint, not a raw principal name.
Arbora accepts only an active grant from its configured issuer.
It binds the qualified session, principal, tenant, workspace, interfaces, policy and configuration digest.
Existing session and construction checks run before and after reads.
Lease identity and revision both participate in continuity.

The access owner invokes the dedicated issuer under its existing policy.
Unknown dispatch outcomes require reconciliation by lease identity; they never trigger an automatic retry.
Cancellation preserves its semantics and carries that reconciliation identity.
Existing systems-manager and container-manager policy remains unchanged.
This adds no launcher, issuer enrollment or manual approval requirement.
Container output never establishes authority.

The native producer and generated client remain deployment prerequisites.
Controlled tests do not qualify deployed issuance or atomic reservation across stores.
They also do not qualify terminal or WebUI runtime handoff.
Human renewal remains with the identity owner and cannot widen this grant.

#### 2026-10-20 — Owner-attested web application serving

R010.7.2.2.2.1 permits web-only foreground serving of an owner-attested ASGI application.
The runtime binding includes exact application identity, authenticated request handling and engine/configuration binding.
The owner also attests exclusive lifespan ownership.
A callable alone is not WebUI or identity proof.
The application must preserve borrowed resources during shutdown.
Arbora selects no default application or session policy.
Terminal/both modes and web logs remain unavailable in this slice.

An external ASGI wrapper revalidates admission before forwarding lifespan startup.
Uvicorn requires lifespan support. Denial sends startup failure, never readiness.
Each restart revalidates admission.
The application owns its messages, cancellation and shutdown.
Controlled ASGI tests establish this composition behavior only.
They do not qualify deployed WebUI, native issuance or live identity.

### Current-main native host-start reconciliation

This source retains the dedicated native issuer adapter and admission checks without replacing current caller credentials or process refusals. The issuer prerequisite is EG694's exact-source host-start contract and fresh generated bindings. Older generated artifacts and controlled owner fixtures cannot qualify that dependency. Native issuer policy, installed consumer composition and actual host startup remain open. No credentials, grants or infrastructure starts are created by this source reconciliation.

The separate web-only slice retains owner-attested borrowed ASGI resources and revalidates admission at lifespan startup. Its controlled application fixtures do not qualify a deployed WebUI factory or native issuer. R010.7.2.2.2.2 retains live client activation, terminal, combined interfaces and logs acceptance. No listener is started outside bounded local fixtures.

### Unique action-decision audit refusal

For `ARBORA-HOST-R033`, a false atomic-create result means the unique ID already
exists. The action-policy owner must not treat that ID as a new decision receipt.
Both a collision and a write exception leave the action without effect authority.
This bounded refusal repair does not qualify native audit persistence or the
separate approval-grant flow.

### 2026-10-26: Native terminal acceptance migration

R009.8.2.2.1 replaces the retired Python/Textual test consumer with released
Tuig 2.3.6, released from `4f53111e53f6179312995883f4f62c8bb584d502`.
The native executable consumes the existing authenticated HTTP contract and
renders it in a real PTY. This supersedes the Python terminal adapter examples
above; it does not restore another TUI implementation.

Keep exact endpoint and source-generation receipt bindings. The test extra
selects the published Linux x86_64/aarch64 wheels and a terminal-cell decoder.
No source checkout or private consumer implementation replaces that wheel.
The four cases retain real sink, restart, rotation, delayed reads and writes, and clearing
checks with controlled identity/grant owners. They do not establish deployed
grant issuance, complete host startup or Overstory acceptance (R009.8.2.2.2).

The log lifecycle fixture waits for the native startup task request to finish
before opening logs. A visible first-frame footer is not a completion signal.
Opening logs during a pending sink write remains a separate fail-closed storage-contention
case, not a passing lifecycle journey. Keep the busy-storage refusal tests unchanged.

### Loopback skill-certification authority

`ARBORA-HOST-R027` restores the lifecycle-owned certification issuer under
`arbora.certification.oidc`. It reuses the former harness authority's bounded
HTTPS request parser, one-run CA, client-credentials token issuer and cleanup.
The server accepts only the loopback bind and clients verify the run-specific CA.
The certification script reports only the issuer and public CA, handles SIGINT
and SIGTERM, and owns shutdown. The fixture and tests use the real TLS endpoint
and verify the issued token with its served JWKS. This is an ephemeral
certification tool, not a general identity provider or persistent credential store.

#### Host-owned configuration paths

Gateway, fleet, certification and profile consumers use `arbora.core.settings`.
The MCP path is `mcp_config.json` in the active Arbora configuration directory.
These callers do not gain a legacy fallback. Arbora trims leading and trailing
whitespace from `ARBORA_CONFIG_DIR`. The old Meristem helper treated that
whitespace as part of the path. The agent data directory
stays outside this cut. Tests cover the explicit and XDG paths and real reads.
This slice does not establish the R008 package-layout census or all cutovers.

### Amendments

#### 2026-10-29 — R033.1–R033.3 persisted record shapes and data migrations

Root cause of the unreadable action-policy ledger: from `a3967b23` (0.1.0) the served engine wrote labelled nodes with `type` only. The released engine files a Cypher label from `node_type`. `ccce23e3` (0.1.8.dev5) added `node_type` to new writes. Rows written before it stayed type-only. `760ec853` then made accounting fail closed while such rows exist, as `ARBORA-FLEET-R015` requires. It shipped no repair, so production refused every rate-limited `connector_pack_import` while reporting "rate limit exceeded". The label change shipped without a data migration, which is the gap these rows close. `record_body` is the one label model. Migrations are recorded and run at start. The shape gate ties a writer change to its migration.

#### 2026-11-06 — R038 unit layers and ports

Measured on origin/main `81d4d1e0` with `tests/architecture/test_unit_layers.py::scan`, which counts function-local imports too: 30 of 46 units form one cycle group, 99 imports point against the layer order below, and 57 imports reach a leading-underscore module or name of another unit.

**Layer order.** The order follows the measured fan-in, not a guess. `mcp_server` imports `api` 118 times and `api` imports `mcp_server` 5 times, so `api` (the operation registry, contracts and invoke pipeline) sits below the subsystems and `mcp_server` is the top: it is the composition root that builds the `HostContext` of R039. Putting `api` above `fleet` and `mcp_server` would turn about 170 imports into findings to fix 13.

| # | Layer | Units | Owns |
|---|---|---|---|
| 1 | foundation | `architecture`, `content`, `core`, `semantic_content`, `skills`, `unique_records`, `version` | settings, transports, providers, record identity; imports nothing above |
| 2 | security | `security`, `access` | actor and session identity, authentication, action policy |
| 3 | services | `identity`, `observability`, `epistemic`, `run_origin`, `modality`, `messaging`, `ecosystem`, `data_migrations` | single-purpose host services used by every subsystem |
| 4 | operations | `api`, `work_items`, `native_run_admission`, `current_plan_publication`, `agent_runs` | the served operation registry and its contracts |
| 5 | subsystems | `fleet`, `fleet_tool_call`, `gateway`, `deployment`, `browser_control`, `ingest`, `agent_host`, `evolution`, `control_plane`, `finance`, `certification`, `connector_runner`, `acp`, `a2a` | one capability each, bound to the registry |
| 6 | host | `mcp_server`, `host_bindings`, `webui_host`, `console`, `local_browser_boundary`, `local_browser_channel`, `client`, the package `__init__` | composition and serving |

**Resolution of every finding class.** A finding is fixed by one of four moves, chosen by where the code's reason to change lives. No move adds an alias or a re-export of the old path.

| Class | Edges | Move |
|---|---|---|
| Shared type below its users | `security`, `identity`, `access`, `run_origin` → `api.contracts.session` (13) | Move `api/contracts/session.py` to `security/session.py` and `access/priority.py` (no arbora imports) to `core/priority.py`, so the session sits beside the actor context it composes. |
| Composition misplaced low | `core/agent_config_admin.py` → `deployment`, `fleet`, `mcp_server`, `access`, `security` (10); `core/settings.py` → `mcp_server.task_plan` (1) | Move `agent_config_admin.py` to `mcp_server/backing/` beside its only caller `tools/config_tools.py`. Move the `TaskPlanConfig` field shape into `core/settings.py`'s own validator module; `mcp_server/task_plan.py` imports it from there. |
| Shared client misplaced high | `fleet`, `gateway`, `deployment`, `messaging` → `mcp_server.backing.{client_credentials, httpx_boundary, remote_oauth_broker, concurrency, context_helpers}` (31) | `httpx_boundary` and `concurrency` (no arbora imports) move to `core`; `client_credentials` (imports only `core` and `security`) moves to `security`; `remote_oauth_broker` (imports `fleet.discovery_authority`) moves to `fleet`; the two identity helpers of `context_helpers` used by `fleet` move to `security/caller_identity.py`. The MCP task wire types `fleet` takes from `tasks_extension` move to `fleet/task_wire.py`, and `tasks_extension` imports them. |
| Composition misplaced in `api` and `identity` | `api/mcp/registration.py`, `api/mcp/serving.py` → `fleet`, `mcp_server` (6); `api/http/browser_app.py` → `webui_host` (1); `identity/browser_composition.py` → `api` (2) | These modules wire subsystems to the server; they move to `mcp_server` (`mcp_server/registration.py`, `mcp_server/serving.py`, `mcp_server/browser_app.py`, `mcp_server/browser_composition.py`). |
| Operation handler reaching a subsystem | `api/ops/{fleet, capacity, research, ingest, browser, fleet_deploy_watch, plans, memory}.py`, `api/invoke/bootstrap.py`, `api/http/browser_auth.py` → `fleet`, `ingest`, `browser_control`, `control_plane`, `a2a`, `gateway`, `deployment`, `mcp_server` (21) | Port. `api/ops/ports.py` declares one `typing.Protocol` per subsystem the handlers call (`FleetOperations`, `IngestOperations`, `BrowserOperations`, `PlanOperations`, `RateLimiter`, `SemanticProvisioning`) and an `OperationPorts` record. `registry_factory.get_registry(ports)` takes the record; the composition root builds it from the `HostContext` and the subsystems implement the protocols. |
| Subsystem or service reaching the host | `fleet`, `gateway`, `deployment`, `a2a`, `browser_control`, `modality`, `agent_runs`, `current_plan_publication`, `work_items` → `mcp_server.{runtime, served_engine, task_plan, task_plan_runtime, effect_journal, backing.engine_routing, backing.tools}` (35) | Port, resolved with R039. The reached objects are the engine handle, the task-plan runtime and the effect journal: process state that becomes `HostContext` fields. The caller receives the field (or a protocol for it declared in its own layer) as an argument; it no longer imports the module that owns the global. |
| Service reaching a subsystem | `observability.runtime_health` → `deployment.live_mount_guard`, `messaging.lease` → `work_items`, `access.host_start` → `agent_host`, `access.host_start` → `identity.ports` (5) | `live_mount_guard`'s check result is passed to the health probe by the composition root; `work_items` exposes the lease record type from `core`; `access/host_start.py` moves to `agent_host`, its only reason to change. |
| Private reach | 57, including six imports of `_version` | The reached name becomes public in its owner, or moves to the caller when only the caller uses it. `_version.py` becomes `version.py`; the bump-my-version config and the release section of AGENTS.md change with it. |
| Cycle inside a layer | `api` ↔ `agent_runs`, `work_items`, `current_plan_publication`, `native_run_admission`; `fleet` ↔ `deployment`, `gateway`; `mcp_server` ↔ `host_bindings`, `webui_host`, `console` | Each pair gets one direction: the registry (`api`) is imported, never the importer; `deployment` and `gateway` import `fleet`, and the two `fleet` imports of `deployment` and `gateway` (`config_generator`, `parse_mcp_servers` of `gateway/config.py`) move to `fleet`; `mcp_server` is imported by no host peer, which receive the context. |

**Module size.** Seven modules exceed 2500 lines and sixteen exceed 1500. Each split extracts a collaborator with its own reason to change, never a mixin or a file cut that keeps one class: `fleet/multiplexer.py` (7885; `MCPMultiplexer` is 4964 of them) splits into `fleet/child_transport.py` (spawn, environment, remote validation, transports), `fleet/proxied_calls.py` (admission, disclosure, OAuth-gated calls), `fleet/task_forwarding.py`, `fleet/child_mounts.py` (mount, forwarders, tool lists), `fleet/fleet_probe.py` (probe, harvest, cache) and `fleet/tool_discovery.py` (ranking, discovery, catalog listing); the multiplexer keeps construction, lifecycle and status and holds the collaborators. Then `engine_surface_tools.py` (3627), `query_tools.py` (3510), `deployment/venv_sync.py` (2918), `intent_tools.py` (2821), `analysis_tools.py` (2670), `server_factory.py` (2572) and the nine between 1500 and 2500 split the same way, one PR each.

**Sequence.** One PR per row of the table above, smallest blast radius first: session and priority, `agent_config_admin`, shared clients, composition in `api`, private reaches, operation ports. The host-reaching edges close with the R039 migrations. The bound test `test_unit_imports_follow_layers_and_no_cycle_remains` lands with the last edge; until then `test_every_unit_is_named_in_exactly_one_layer` and the scanner fixture test hold the file and the checker. The layers live in the `coupling` gate policy, `[[tool.pipelines_hooks.coupling.layers]]` in `pyproject.toml` (owner ruling 2026-11-07: one standard, the gate's policy location). When `PIPE-GATES-R088` ships, the `coupling` hook reads that table and the repository-local scanner is deleted in the same PR.

#### 2026-11-06 — R039 HostContext shape and migration order

Measured on origin/main `81d4d1e0`: 123 module-level mutable objects and `global` statements (`mcp_server` 28, `observability` 26, `gateway` 22, `security` 13, `fleet` 6, `access` 6, the rest under 5 each), plus 29 mutable module containers that are filled at run time (`_LIVE_MULTIPLEXERS`, `_POLICY_CACHE`, `_HOST_PROBES` and peers). `mcp_server/runtime.py` writes its locks and slots into `bootstrap.__dict__` at import to break a cycle. There are 407 suppression comments: 339 `noqa: BLE001`, 28 `pragma: no cover`, 10 `type: ignore`, 30 others.

**Shape.** `arbora/mcp_server/host_context.py` defines `HostContext`, a frozen, slotted dataclass. Each field is one owned object that holds its own lock; a field is never reassigned, its object changes state through its own methods. `mcp_server/host_context.py::build_host_context(engine_provider)` builds it once, after `load_config()` and before `install_gateway_application` in `server.mcp_server`, and `HostContext.aclose()` closes the fields in reverse order. There is no module-level `current_host()` accessor, because that would be the same global under another name.

| Field | Type | Holds today's module state |
|---|---|---|
| `runs` | `RunState` | `run_origin._runs` and its lock (landed: `RunOriginRegistry`), `a2a.mcp._SERVICE` (landed: `RunState.a2a`), `host_bindings._agent`, `a2a.authority._OUTPUTS`, `modality.normalizer._default` |
| `browser` | `BrowserState` | `browser_control_runtime._owner_loop`, `_service` and the binding lock; `browser_dispatch._capacity` |
| `fleet` | `FleetState` (`arbora/fleet/state.py`) | `multiplexer._LIVE_MULTIPLEXERS` (landed: `FleetState.multiplexers`), `_REMOTE_OAUTH_BROKERS` and lock (landed: `FleetState.oauth_brokers`, `fleet/oauth_brokers.py`), `plan_confirmation._ARMED` and `_LOCKS` (landed: `FleetState.confirmations`), `shared_multiplexer._served`, `declarations._DECLARATIONS` (landed: `FleetState.declarations`; onboarding takes it as an argument, research ingest reads the `fleet_declarations` binding), `access_contracts._CATALOG` (landed: `FleetState.virtual_catalog`; the served invocation receives it as the `fleet_virtual_catalog` binding), `gateway_ops._CONFIRMING_TOOLS` (landed: `FleetState.confirming_tools`), `remote_oauth_broker._TOKEN_LOCKS` (shared with the gateway broker, so it moves with `remote_oauth_api._BROKER` or receives the fleet's instance) |
| `policy` | `PolicyState` | `action_policy._ports`, `_POLICY_CACHE`, `_NO_ENGINE_POLICY`, `external_policy_sync._DIRTY_TENANTS`, `approval._session_scope`, `anonymous._ACTIVE_MODE`, `messaging.host_ports._ports` |
| `observability` | `Observability` | `telemetry._provider`, `langfuse_exporter._EXPORTER`, `trace_ontology` sequence and cursor, `subsystem_health` windows, `runtime_health` readiness authority, host probes, process lease and identity contract, `readiness._PROBES`, `runtime_signals` buffer, `gateway_health` history, buffer, evidence store and tasks, `gateway_metrics._SCRAPE_SAMPLERS`, `custom_observability` flags, `error_surface` detail store and sink, `client_contract._SUPERVISOR` |
| `caches` | `HostCaches` | `runtime.REGISTERED_TOOLS` (the tool table the REST twins dispatch through, 112 references), `security.auth._jwks_cache`, `oidc_discovery._cache`, the six `intent_tools` caches and `_APPROVALS`, `catalog_warmup._CURRENT`, `bootstrap._BUNDLED_SKILL_READINESS`, `runtime._FULL_TOOL_NAMES`, `engine_surface_tools._RAM_CHECKPOINT_STORE`, `analysis_tools._analysis_core_handler` |
| `gateway` | `GatewayState` | the routers (built by `build_<name>_router(host)` instead of at import), `remote_oauth_api._BROKER` and `_SCOPE_POLICY`, `gateway.registry._registry`, `dashboard_api._aggregator`, `graph_api._SPARQL_BRIDGE`, `enhanced_catalog_api._reader`, `gateway.fleet._AUTHORITIES`, `fleet_events` rate counters, `daemon` metrics flag, `gateway.ports._application` |
| `engine` | `EngineHandles` | `served_engine._POOL`, `_ENGINE`, `_LOCK`; `runtime._ENGINE_LOCK`; `engine_tools._CLIENT_POOL` and lock; `gateway.daemon._engine`; `engine_routing` extraction manager, connection registry and tabular query service |
| `authority` | `ProcessAuthority` | `bootstrap` process session, refresh thread and stop event; `runtime._PROCESS_SESSION_REFRESH_LOCK`; `request_identity._system_write_session` and lock; `aggregator._SERVICE_SESSION`; `run_token._EPHEMERAL`; `ard_signing._EPHEMERAL_SEED` |
| `host_lock` | `HostLock` | `deployment.host_lock._lock_fd` and `_effective_role` |
| `config` | `RuntimeConfig` | `core.settings.runtime_config` and `_write_lock` |

**What stays at module level.** A `ContextVar` declaration (16 of them) is a key, not state: the value lives in the request context. Tables filled only from literals at import become immutable (`tuple`, `frozenset`, `MappingProxyType`): `venv_sync.GUARDRAILS`, `ACTIVITY_PROBES`, `VERIFY_PROBES`, `venv_autosync.TRIGGER_BACKENDS`, `contradiction._ANTONYMS`, `actor_context._EDGES_BY_PAIR`. A table that is written after import is a registry and moves. `fleet.protocol_compat` patches the MCP SDK once; the patch moves from import time into `build_host_context`, so no import has a side effect.

**How code reaches a field.** By argument, narrowest field first: a function that needs the engine takes `engine: EngineHandles`, not the whole context. A FastAPI route reads `request.app.state.host`; a FastMCP tool is registered as a closure over its field by the registration function the composition root calls; a thread or background task receives the field when it is started. This is also how the 35 host-reaching imports of R038 close: the caller holds the field and no longer imports the module that owned the global.

**Suppressions.** A suppression goes with the cause it hid, never with a reworded suppression. `noqa: PLW0603`-style and `type: ignore` comments on optional globals go with their global. The 339 `noqa: BLE001` broad catches are narrowed to the exceptions the call can raise (an outbound call catches the transport and protocol errors of its client; a supervisor loop that must survive any failure keeps `except Exception`, re-raises it or logs it with its traceback, which `BLE001` accepts without a comment). `pragma: no cover` lines get a test or are deleted as unreachable. The bound test asserts the count is zero after the last PR.

**Order.** One PR per field, smallest and least shared first, so each PR is reviewable and lands while other work is in flight: `runs` (introduces `HostContext`, `build_host_context` and the bound test with its first field), `fleet`, `policy`, `observability`, `caches`, `browser`, `gateway`, `engine`, `authority` (deletes the `bootstrap.__dict__` injection in `runtime.py`), `host_lock`, then `config`, which every unit reads and so moves last. Suppressions are removed in the PR that moves their cause, and the remaining broad catches by unit after that. `browser` follows `caches`: the browser tool closure lives in `REGISTERED_TOOLS`, and `ensure_tools_registered` rebuilds that table from callers that hold no context (`intent_tools`, `gateway.graph_api`), so a browser binding passed by argument before the table is a field would be rebuilt over an empty binding.

#### 2026-11-07 — R038 and R039 execution order and R040 adoption

Pre-untangled design for the rows R038.1 to R038.12, R039.1 to R039.13 and R040 to R040.4, one line per answer.

- Owner. Arbora, units `api`, `mcp_server`, `fleet`, `gateway`, `security`, `observability`, `core`. The harness and the SDK own only the released paths named in R040.
- Cohesion. A collaborator that changes for one reason (spawn, forwarding, mounts, calls, probe, discovery) is its own module. State that is built together (the engine pool and its lock) is one object.
- Dependency direction. The layer order of R038 holds: `mcp_server` builds the context and passes fields down; no lower unit imports a higher one. A call upward goes through a protocol declared in the lower unit.
- Interface depth. A unit exposes its package `__init__` and named public modules. The scanner finds no private reach.
- Special cases. None added. Every global takes the same route (a field plus an argument), and every host-reaching import takes the same fix.
- State. Run origin, lease, readiness and plan state machines are specified in their own rows; this work does not add a flag.
- Environment. None. The boot proof of R040.4 uses the wheel gate that already exists.

**Order.** The sequence has three tracks that run in parallel, because they touch different files. Track A (ports and moves): R038.6, R038.5, R038.8, then R038.1. Track B (state): R039.1 first, then R039.2, R039.3, R039.4, R039.5, R039.6, R039.7, R039.8, R039.9, R039.11, then R039.12 by unit. Track C (adoption): R040.1, then R040.2 (needs R039.1), R040.3, R040.4. The host-reaching rows R038.2 to R038.4 follow the field they need. Module splits R038.9 to R038.11 run last, one PR per module, because they move code that the earlier PRs edit. R038.12 and R039.13 close the two bounds. A PR touches one unit family and stays under the five-minute lane limit; a row marked L is a list of such PRs, named in the row.

**Open owner call recorded.** The `HostPorts` record is a field of `runs`, not a separate `HostContext` field, because the harness reads it only on a run request.

## Phase 2: Task approach

tasks.md lists one task per row, producer first. Tasks of independent rows run in parallel.

## Complexity Tracking

None. The constitution check passed.

## Build contract

The change must pass the repository pre-commit gates at both stages: `uvx pre-commit run --all-files` and `uvx pre-commit run --all-files --hook-stage pre-push`.
The wiring gate (`scripts/check_wiring.py`) and the public-spec gate (`scripts/check_public_specs.py`) apply. A new module must have a production caller.

## Production caller chain

Each row names its entry point and caller chain in its own text. This plan adds no separate chain.

## Cross-repo seams

| Seam | Contract | Counterpart |
|---|---|---|
| Composition owners (engine, connector SDK, harness, web UI) | [`contracts/composition-owners.md`](contracts/composition-owners.md) | `ARBORA-HOST-R001` |
| Single gateway | [`contracts/single-gateway.md`](contracts/single-gateway.md) | `ARBORA-HOST-R004` |
| Fleet multiplexer ownership | [`contracts/fleet-multiplexer.md`](contracts/fleet-multiplexer.md) | `ARBORA-HOST-R006` |
| Harness host-facing module set (meristem) | [`contracts/harness-host-modules.md`](contracts/harness-host-modules.md) | `ARBORA-HOST-R007` |

The `Depends on` lines of requirements.md name every other dependency by the row ID that owns it in the other repository.

# ARBORA-HOST — Arbora host composition and ownership boundary

**Owner:** arbora. **Requirement IDs:** ARBORA-HOST-R014, ARBORA-HOST-R015, ARBORA-HOST-R001, ARBORA-HOST-R002, ARBORA-HOST-R003, ARBORA-HOST-R004, ARBORA-HOST-R005, ARBORA-HOST-R006, ARBORA-HOST-R007, ARBORA-HOST-R008, ARBORA-HOST-R009, ARBORA-HOST-R010, ARBORA-HOST-R011, ARBORA-HOST-R012, ARBORA-HOST-R013. **Delivery:** implemented in part; **acceptance:** pending. Existing package extraction is released, while the strict import boundary, retired duplicate hosts, and served parity have not been verified as a set.

## State legend

`Proposed` means a reviewed contract awaits implementation; `Implemented` means source exists; `Verified` means tests passed at an identified commit; `Released` means the exact verified source is on the public default branch and installable; `Accepted` means the external behavior and negative cases in this spec have been observed against that release. These states are cumulative only with cited evidence. A source file or passing focused test alone never establishes `Accepted`.

## Outcome and scope

One Arbora process authenticates, authorizes, composes, routes, supervises and projects MCP, REST, A2A, messaging, optional browser UI and deployment actions. The process calls public contracts of the graph engine, connector SDK and agent runtime. It never becomes a second durable graph, task, agent or connector authority. Any first-party surface that exposes the same action must share one application service and authorization decision. External contributors can implement one boundary slice from this repository and public dependency packages without another workspace checkout.

### Requirements

1. **Single host authority.** `arbora.mcp_server` owns the FastMCP loop and multiplexer instance. Gateway, A2A and UI co-services submit work to that owner loop. Startup, shutdown, reload and cancellation are idempotent, bounded and observable; no `Future.result()` blocks another event loop.
2. **Public dependency ports.** Arbora imports the agent runtime only through `agent_utilities.api`, graph records through the installed `medulla` client, and connector lifecycle through the installed SDK's public interfaces. No direct import of a dependency's `knowledge_graph`, private `mcp`, `security`, `core.config`, or server implementation packages remains. The package-layout test enforces this at every Arbora source import site, including lazy imports.
3. **One business path.** MCP, REST, A2A, messaging and optional UI routes adapt a typed request into the same application operation. The request contains verified principal, tenant, scopes, action, target, policy version, idempotency key and correlation ID. A caller-provided field cannot replace the verified boundary principal. The service emits the same result/error class and governed receipt regardless of entrypoint.
4. **One state owner.** Durable task/event/artifact/run, graph and policy records are kept by the graph engine. Agent planning and execution by the agent runtime. Connector effects by the SDK. Arbora may hold ephemeral connection/session/cache state with generation and expiry. It cannot create a parallel task store, ontology writer, source connector or uncorrelated execution path.
5. **Safe source migration.** Existing Arbora copies under `gateway`, `fleet`, `deployment`, `a2a`, `webui_host` and `control_plane` are reconciled against their former AU duplicates. Missing behavior is ported to the owning Arbora package or explicitly retired with a public compatibility decision. There is no second live host, dual script, duplicate route registration or fallback reader left behind.
6. **Independent installation.** `uv sync --extra test` and the documented package extras suffice to import and test Arbora from a clean checkout. Optional WebUI, connector and identity features fail with actionable unavailable errors when their public dependency is absent; they do not require sibling source trees, local operator inventory or live credentials to run unit/contract checks.

7. **ARBORA-HOST-R029 Tenant-bound native desktop actions.** Require a verified sandbox grant and tenant match. Keep cursor/focus unchanged. The SDK provider contract remains unassigned.
8. **ARBORA-HOST-R030 Run-bound visual state.** Preserve source/frame provenance and bind reusable state to identity, tenant, lifetime, and invalidation. The SDK and EG producer contracts remain unassigned.
9. **ARBORA-HOST-R031 Coding-agent discovery and handoff.** Discover the selected executable, apply the EG transformation through AU, and render only in the WebUI sandbox. Keep the flow unavailable until owner ports are published.
## Acceptance

- Import-boundary census reaches zero forbidden dependency-internal imports in `arbora/**`; a planted forbidden import fails the same check in CI.
- A served test invokes the same representative read, governed write, cancellation and refusal through at least two entrypoints and compares principal, tenant, policy decision, receipt ID, result and error code. A2A parity additionally follows the [A2A spec](../a2a/spec.md).
- Removing optional dependencies yields explicit feature-unavailable behavior and leaves core serving operational. A missing graph-engine contract cannot be replaced with a fabricated success.
- Exactly one runtime owns the event loop/multiplexer, and duplicate AU host entrypoints are absent from the packaged command inventory.
- Exact commit, wheel hash, focused/full gate results and served receipts are recorded in `tasks.md` before the state advances to `Verified`, `Released` or `Accepted`.

## Public dependencies

This contract uses the published `medulla`, `agent-connector-sdk` and `meristem` package APIs. Their own specifications explain implementation of the durable engine, connector effects and agent runtime. The Arbora request, composition and parity obligations above are complete here.

Requirement IDs are defined in [requirements.md](requirements.md); delivery state per ID is in `status.json`.

## Complete startup configuration

R010 retains its original ownership contract. Its children define complete environment declarations and validation before service startup.
Arbora validates public declarations provided by enabled components; their owners retain setting semantics and credential resolution.
The new startup scenarios have not been run. Existing helper validation does not establish complete startup coverage.

## Host start and installation boundaries

R008 extends local harness installation; R009 extends hosted HTTP lifecycle and scoped workspace logs.
R010 selects terminal, web or combined interaction; R006 and R023 retain one served MCP surface across transports.
Arbora owns hosting and local installation. Terminal and browser presentation remain with their public clients.
The original parents remain complete contracts. Residual children preserve their acceptance alongside the new behavior.
The proposed installer and terminal journeys have not been run.

## Authenticated terminal log handoff

ARBORA-HOST-R009.5, R009.6 and R010.7 bind the effective strict web_logs boolean into the proposed HostLaunchReceipt v1. Authenticate it with host, workspace, tenant, principal and session scope. Retain generation and expiry. Consume through TUIG-RUNTIME-R002.2 through R002.6 using run_host_terminal and HostLogReader.read as specified in plan.md. Only terminal/both with true permits reads. Disabled, absent or invalid preference and web mode yield zero TUI reads while preserving the required host sink. Runtime receipt and adapter availability remain explicit prerequisites. This definition adds no implementation or served receipt.

## Additional owner requirements

- `ARBORA-HOST-R023.3`: Owner scope and complete acceptance are defined in requirements.md.
- `ARBORA-HOST-R008.6`: Owner scope and complete acceptance are defined in requirements.md.
- `ARBORA-HOST-R026.6`: Owner scope and complete acceptance are defined in requirements.md.
- `ARBORA-HOST-R022.1`: Owner scope and complete acceptance are defined in requirements.md.
- `ARBORA-HOST-R010.8`: Owner scope and complete acceptance are defined in requirements.md.
- `ARBORA-HOST-R032`: Owner scope and complete acceptance are defined in requirements.md.

# Hosted API and intent operations — architecture

Status: **READY FOR IMPLEMENTATION**. Delivery: **NOT ACCEPTED**. Governing [spec](spec.md).

## Existing wiring to reuse

`graph_os.mcp_server.runtime` owns the served FastMCP loop, verified session scope, 320-second bounded dispatch, and `runtime.graph_client(tenant)`. `graph_os.gateway.graph_api.register_graph_routes` composes the REST host. `graph_os.fleet.multiplexer` owns per-session loading, forwarding, notifications, and child lifecycle. `graph_os.a2a` already has authenticated unary task methods. `graph_os.webui_host.mcp_delegation` must use the same public invocation boundary. These are migration seams, not new parallel servers. Engine state belongs to `epistemic-graph`. Decisions, context, traces and durable WorkItems belong to `epistemic-graph`. `agent-utilities` is the agent harness that applies engine decisions. Connector ingestion belongs to `agent-connector-sdk` (SDK-CONNECTOR-CONTROL-R005). Connector effects belong to the SDK and fleet. GraphOS authenticates, composes, supervises, routes and projects.

## Component and data flow

```mermaid
flowchart LR
  C[MCP / HTTP / A2A / fleet forwarder] --> S[verified surface adapter]
  S --> I["invoke: validate, scopes, policy, subject, effect, audit"]
  I --> R[one typed operation registry]
  I --> E[engine client under caller claims]
  I --> A[agent service via public port]
  I --> F[fleet gateway under caller credential]
  I --> D[durable audit and plan lease]
  R --> G["generated OpenAPI, schemas and clients"]
```

Use immutable `OpSpec` data grouped by domain. An engine binding is generated from the versioned packaged method schema; a composite binding names a typed GraphOS service function. A curated alias suppresses the corresponding generated `eg.*` op so each wire method has exactly one owner. `Registry.digest` hashes canonical sorted schema and policy metadata. Route/verb factories create a single HTTP and MCP wrapper per operation family, never duplicated per-domain authorization.

The invocation order is: resolve and allowed surface. Strict params including authority-field rejection. Authenticate. Principal kind. Exact caller scopes. Eunomia decision. Caller-side subject access for service execution. Effect preview/confirmation. Durable audit reservation for governed effect. Execute under caller claims or allowlisted service identity. Record outcome. Map to one envelope. The service executor stamps the verified caller as record owner, refuses caller-supplied owner, and rechecks authorization before deferred work. A post-effect audit failure returns an indeterminate receipt and reconciliation handle.

## Wire contracts

`POST /api/v1/ops/{op_id}` accepts the operation's JSON schema; `Idempotency-Key` is required for non-natural write ops. `GET /api/v1/registry` is caller-filtered and returns a digest ETag. `GET /api/v1/ops` and OpenAPI expose only public schema metadata; sensitive discovery is filtered per caller. Resource routes, including identity and fleet catalog paths, are generated from `HttpShape` and use the same handler. `Graphos-Plan-Ref` resumes a plan. HTTP preview returns 428 with `CONFIRMATION_REQUIRED` or `STEP_UP_REQUIRED`. MCP returns the same envelope in tool result. `meta.registry_digest` and `meta.api_version` are mandatory.

The current MCP surface is GRAPHOS-FLEET-R029: six intent tools plus two Apps launchers. `find` accepts `op` or natural-language `intent`; exact operation IDs and typed params are required before effect. Verb ceilings are enforced (`ask/find/why` read, `write` write, `act` operational effects, `manage` admin). Discovery returns authorized schema descriptors. A bounded cache and tenant/policy-partitioned outcome ranking may improve suggestions but must never turn fuzzy text into an unconfirmed mutation. Fleet discovery and status use `find`, invocation uses `act`, and mounts/releases use `manage` (GRAPHOS-FLEET-R029). Reuse GRAPHOS-FLEET-R011/R013 registry/discovery machinery and GRAPHOS-OPS-R007/R011/R012; add only missing operation wiring and no duplicate registry or disclosure rollup. Native child forwarding calls `invoke("fleet.call", ...)`; no transport calls bypass it. The child call uses the caller's delegated credential or a declared service-execution rule with the same deputy checks. `ToolAnnotations` classify read/destructive effects; unannotated means write; a reviewed manifest can tighten to admin.

Scopes are exact. `mcp:discover` and `mcp:delegate` do not imply each other; `mcp:admin` is not a super-scope. `finance:read`, `fleet:read/control`, `loops:read/control`, `ops:read/admin` and every operation scope must exist in the public engine registry. `EUNOMIA_TYPE=off` still enforces scopes; enabled PDP failure returns `POLICY_UNAVAILABLE`. Decisions cache at most 30 seconds by principal, revision, item, action; admin/destructive are uncached. A revision invalidates loaded visibility and sends list-change notifications. The enabled/disabled mode behavior is testable with an in-process fixture PDP.

Error source is `engine`, `fleet`, or `graphos`. Engine error codes pass through unchanged from the packaged error contract; GraphOS owns one mapping table for its closed enum. Details may include missing public scope names or a public target name, never secrets or private actor/tenant values. Timeouts and unknown outcomes remain typed errors. Stable API compatibility is checked against the merge base of public generated registry artifacts.

## Migration and implementation order

1. Pin a public engine wheel that ships method/error/scope schemas. Add registry data and direct bindings. Check exact coverage and exclusions.
2. Add invocation, policy, error, audit-reservation, and plan-lease services with fixture-backed tests. Reuse verified-session middleware and current server/client composition.
3. Add MCP verbs, multi-kind fleet adapters, HTTP factory and generated clients. Build one end-to-end vertical slice before expanding domain op tables.
4. Add each domain family by moving handlers out of existing registrars. For missing upstream methods, make a typed unavailable op and defer advertisement until a released contract exists.
5. In one integration change, mount the ten resident tools and `/api/v1`, route loaded forwarders through `invoke`, migrate consumers, delete duplicate registrars/routes and stale instructions. Keep one owner loop and one multiplexer instance.
6. Run [test-spec.md](test-spec.md), publish exact revision evidence, then mark individual capabilities `ACCEPTED`.

The retirement inventory is a checked artifact produced from the existing host registration table and the new registry. Each prior tool/route is labeled `mapped` with its new op ID or `dropped` with a reason and consumer search result. The cutover check enumerates FastMCP tools, Starlette routes and A2A methods at runtime and rejects undeclared extras. It also scans source for ad hoc `mcp.tool`, route registration, and child transport dispatch outside the approved factories. A transition is atomic for supported callers: generated client release, server mount, caller migration and old endpoint removal are reviewed together; no permanent fallback dispatcher remains. External clients receive v1 contract and migration note before stable path removal.

## Fresh-checkout and quality design

`uv sync --extra test`, `uv run pytest tests/api tests/fleet`, `uv run ruff check .`, `uv run ruff format --check .`, and `uv run mypy graph_os` must run with public pinned wheels and fixture identities. CI may provision an ephemeral local engine/agent stack for served tests; no check requires a private host or credential. Existing repository hooks provide CCCC, KISS, jscpd and Dupehound scanning; the scoped implementation must satisfy configured thresholds (including zero new clone pairs), not lower or suppress them. Keep registry domain tables small, use a step table rather than nested branching, one endpoint factory per surface, and generated declarative types rather than repeated function bodies. Delete old implementation in the cutover so scanners and reviewers see one authority.

## Gateway carry-over: dashboard, Live Artifacts, widget

Cross-repo IDs: AU-BOUNDARY-R001.2, AU-BOUNDARY-R001.3, AU-BOUNDARY-R001.7 (agent-utilities deletions); GRAPHOS-HOST-R005. Provenance: agent-utilities commits 0eb67c99c (dashboard router), 902f906ca (Live Artifacts router), 7717d2e62 (genius_agent widget).

Deleted dashboard router (prefix `/api/dashboard`, read capability on every route except `/health`; write capability on mutations; capabilities gateway:read, gateway:write, gateway:admin, admin; API-key callers unrestricted):
- GET `/layout` and PUT `/layout`: read and persist the dashboard layout (shared YAML, reads always go back to disk).
- GET `/data`, GET `/data/{service_id}`: widget data for all or one service; the id must match a safe pattern, unknown service returns 404.
- GET `/full`: layout and data in one call. GET `/data-subset?widget_id=...`: only the named widgets, loaded concurrently, never computing unsubscribed ones.
- GET `/widgets`: widget types available. GET `/discover`: layout auto-discovered from the MCP config.
- GET `/health`: always 200 with the shared truthful health report, `Cache-Control: no-store`.
- GET `/daemon/status`, GET `/daemon/shards`, POST `/daemon/start`: consolidated background daemon state, engine shard topology and reachability, ensure daemon running.
- POST `/hydrate/{source}`, POST `/hydrate`, GET `/hydration-status`: trigger hydration for one or all sources, and read source status. 422 on a malformed source, 500 when no active engine, sanitized error payloads.

Deleted Live Artifacts router (no router-level auth in the old module):
- POST `/api/artifacts`: create from name, template, data, source query, source node ids and model. Provenance records model, query and evidence ids. Bounded-JSON violation returns 400. Response is artifact id and rendered output.
- GET `/api/artifacts/{artifact_id}`: full artifact, 404 when absent.
- POST `/api/artifacts/{artifact_id}/refresh`: with inline `data` the body becomes the new derivation. Otherwise a registered source resolver re-derives from the graph (the default resolver preserved prior data). Returns ok, reason and rendered output. 404 when absent. A failed refresh preserves the prior render.
- Stores: the shared Live Artifact store and refresh service, a module singleton. Caller: `server/app.py` mount only; the KG source installer was deleted with the router.

Deleted Genius Agent widget: dashboard tile (type genius_agent, env prefix GENIUS_AGENT, observability category) with fields agents, skills, mcp_tools and status. Its data call returned hardcoded numbers and never contacted anything.

### Design in this repository

- Dashboard: owned by the existing dashboard router module and its aggregator, config manager and registry under the gateway package. No new module; add census and capability tests only.
- Live Artifacts: new router module `graph_os/gateway/artifacts_api.py` (to be created) with typed request model, a store port and a refresh port declared in the gateway ports module, and a register function that mounts the three routes. The store and refresh service come from a public `agent_utilities.api` export (prerequisite row); graph-os never imports the agent-utilities knowledge-graph package directly.
- The source resolver is a port argument, not a module global: the host passes a graph-backed resolver, default behaviour is preserve-prior-data.
- Genius Agent widget: nothing to build.

### Wiring

- Dashboard: the register function in the dashboard router module mounts routes and the websocket; already called by the host.
- Artifacts: a new register function is called from the same host composition point that mounts the graph and usage routes. Authentication: reuse the dashboard capability dependencies unchanged (decision D3 below: gateway:read for GET, gateway:write for POST, plus the gateway:admin and admin supersets). These are identity capabilities, not engine registry scopes. The old router had no router-level check, so this is an intentional tightening, called out in the parity table.
- Config: the artifact store location follows the agent-utilities store default; no new env key.

Out of scope:

Moving the Live Artifact store or refresh service out of agent-utilities, new dashboard widgets, any change to the layout file format, and the MCP tool surface for artifacts (the research-artifact tool is unrelated).

### Caveat resolutions (design, 2026-10-10)

#### D2 Dashboard route parity (CONFIRMED by reading both files)

Compared `git show 0eb67c99c^:agent_utilities/gateway/api.py` (328 lines) with `graph_os/gateway/dashboard_api.py` (389 lines). Both define the same 15 REST routes. The only addition is the websocket `/ws/dashboard` (moved from agent-webui; not part of the old router). Shared helpers are line-for-line equal: `_SERVICE_ID_RE`, the read and write capability sets (`gateway:read`, `gateway:write`, `gateway:admin`, `admin`; write excludes `gateway:read`), API-key callers unrestricted, the `/health` exemption in the read dependency, fail-closed 403 when capability resolution raises, and `Cache-Control: no-store` on health. Supporting modules differ only in import paths, a naive-UTC `_naive_utcnow` replacing deprecated `datetime.utcnow` in `models.py` (same value), and a new service-session in `aggregator.py`.

| Method and path | Auth | Request / response shape | Refusals | Result |
|---|---|---|---|---|
| GET /layout | read | none / `DashboardLayout` | 403 | CONFIRMED-equal |
| PUT /layout | write | `DashboardLayout` / `{"status":"saved"}` | 403 (write), 422 (body) | CONFIRMED-equal |
| GET /data | read | none / `dict[str, WidgetData]` | 403 | CONFIRMED-equal |
| GET /data/{service_id} | read | none / `WidgetData` | 403; 404 on bad id or "not found" error | CONFIRMED-equal |
| GET /full | read | none / `DashboardResponse` | 403 | CONFIRMED-equal |
| GET /data-subset | read | repeated `widget_id` / `dict[str, WidgetData]`; default `[]` became `None` and `set(widget_id or ())`, same empty result | 403 | CONFIRMED-equal |
| GET /widgets | read | none / `list[WidgetListItem]` | 403 | DIFFERS: same shape, but the registry module map lives in `graph_os/gateway/registry.py`. The set of widget types is not compared in any test (row R001.8) |
| GET /health | none | none / health report | none (always 200) | CONFIRMED-equal |
| GET /discover | read | none / `DashboardLayout` | 403 | CONFIRMED-equal |
| GET /daemon/status | read | none / daemon status dict | 403 | DIFFERS: now `graph_os.gateway.daemon.daemon_status`; the module was refactored and its key set is not pinned (row R001.5) |
| GET /daemon/shards | read | none / shard topology dict | 403 | DIFFERS: same wire; still imports `agent_utilities.knowledge_graph.core.shard_topology`, an internal module (row R001.6) |
| POST /daemon/start | write | none / daemon status dict | 403 | DIFFERS: same as /daemon/status (row R001.5) |
| POST /hydrate/{source} | write | none / hydration result dict | 403, 422 bad source, 500 no engine, 400 `ValueError`, 500 sanitized | DIFFERS: refusal codes equal. Engine and hydration come from AU internal modules (row R001.7) |
| POST /hydrate | write | none / hydration result dict | 403, 500 no engine, 500 sanitized | DIFFERS: same (row R001.7) |
| GET /hydration-status | read | none / status dict | 403 | DIFFERS: same (row R001.7) |

No route is MISSING. The capability resolver itself still imports `agent_utilities.core.config` and `agent_utilities.security.identity` (internal modules): DIFFERS in dependency only (row R001.9). Decision on the shard, hydration and active-engine dependency (DECIDED): use the engine client session and source-sync methods that `GRAPHOS-HOST-R002` already requires, not a new public `agent_utilities.api` export, because hydration and shard state are engine concerns. Row R001.6 first adds a contract test that the installed client exposes them; if it does not, the row is BLOCKED on the engine client owner and the route keeps its present imports.

#### D3 Live Artifacts authentication (DECIDED; not blocked)

Evidence: `graph_os/api/registry/scopes.py` exports `FinanceScope`, `FleetScope`, `LoopsScope`, `OpsScope`; `graph_os/api/policy/domain_scopes.py` lists `fleet:events` and `webui:{admin,maintainer,reader,user}`. The pinned engine `contract/scopes.json` (175 rows, the file `eg_binding._load_scopes` reads and `tests/api/test_registry_factory.py` checks for "unregistered EG scope") has no `gateway:*` row. So `gateway:read` and `gateway:write` are NOT registry scopes. They are identity capabilities that the dashboard resolves from the identity group capability map (`_dashboard_capabilities`). The earlier text calling them "already in the engine contract" was wrong and is corrected above.

Decision: the artifact routes use the same dependencies as the dashboard routes (`_require_dashboard_read` for GET, `_require_dashboard_write` for POST, reused from `dashboard_api.py`, not copied). Behaviour matches sibling routes and no engine change is needed. Gateway scopes need no addition to the engine. `EG-IDENTITY-R007` registers `identity:admin`, `loops:read` and `loops:control` and is unrelated. The `loops:*` scopes are absent from the pinned `scopes.json` today. That is the concern of that row. Alternative rejected: map to registry scopes `webui:reader`/`webui:user`; those are the WebUI role namespace. That mapping creates a second authorization model for one gateway.

#### D4 Agent-utilities export for artifacts (CONFIRMED; proposed AU row)

Read `agent_utilities/knowledge_graph/live_artifacts/`. Public names: `LiveArtifact`, `Provenance`, `BoundedJSONError`, `validate_bounded_json`, `render_template` (models.py); `LiveArtifactStore`, `get_live_artifact_store`, `ArtifactWriter` (store.py); `RefreshService`, `RefreshResult`, `DataSource` (refresh.py); `kg_source_resolver` (kg_source.py). `install_kg_artifact_source` imports the deleted `agent_utilities.gateway.artifacts_api` and always logs a failure; it must not be exported.

Proposed agent-utilities row (orchestrator adds it; new file `agent_utilities/api/live_artifacts.py`, re-exported from `agent_utilities/api/__init__.py`):

> `AU-BOUNDARY-R013.<n>` **Export the Live Artifact store, models and refresh service.** Re-export `LiveArtifact`, `Provenance`, `BoundedJSONError`, `validate_bounded_json`, `LiveArtifactStore`, `get_live_artifact_store`, `RefreshService`, `RefreshResult`, `DataSource` and `kg_source_resolver` from `agent_utilities.knowledge_graph.live_artifacts` through `agent_utilities.api.live_artifacts`; delete the dead `install_kg_artifact_source`. Verification: an agent-utilities test imports each name from `agent_utilities.api` and the layout fence still passes.

Because the AU resolver already re-runs `source_query`, GRAPHOS-OPS-R040.8 wires `kg_source_resolver` instead of re-implementing it (no parallel function). The bounded-JSON limits (depth 8, 100 keys, 500 items, 16 KiB string, 256 KiB total) stay in the AU validator. The graph-os request model does not copy them.

## Run read operations

### Data flow

`runs.metrics` or `runs.get` call → invoke pipeline (`GRAPHOS-OPS-R009`) → `handle_runs_metrics` or `handle_runs_get` (`graph_os/api/ops/runs.py`) → `RunTraceReader` port → engine adapter → engine run-trace read → typed outcome → response envelope.

### Declaration to response, traced on an existing operation

`retrieval.search` shows the pattern. `operations()` calls `build_read_op` with params, result, handler path, and scope (`graph_os/api/ops/retrieval.py:112-123`). The handler resolves its port with `bound_service` (`retrieval.py:81`) and returns `{"value": ...}`. An uncomposed port fails closed with `UNAVAILABLE`. `registry_factory.py` lists the module. The `runs` module copies this shape.

### Types and scopes

`RunMetricsOutcome` returns, per model profile and task class, `success_rate`, `cost_per_task_usd`, `overflow_rate`, and `run_count`. `RunSummary` returns `termination_reason`, `peak_context_tokens`, `context_budget_tokens`, and the applied `harness_config`. Names follow the run-trace vocabulary in the EG run-trace row. Scopes: `telemetry:read` for `runs.metrics` (`telemetry.py:55`) and `kg:read` for `runs.get` (`agents.py:131`). No scope class in `graph_os/api/registry/scopes.py` is added, because every scope already exists.

### What exists and what is new

Reused: `build_read_op`, `bound_service`, `Params`, the closed error enum, and the registry generator. New: the `runs` module, the `RunTraceReader` port, and the engine adapter. No engine client method for run traces appears in `graph_os` today. The adapter row `GRAPHOS-OPS-R021.4.2` depends on the engine exposing a wire-callable read method.

### Decisions

- The engine computes success rate, cost, and overflow rate. Rejected: aggregation in graph-os. It creates a second metrics store and a second definition of success.
- `runs.get` reads the stored trace. Rejected: reconstructing the configuration from the A2A task. Only the written trace proves what the run applied.
- A run in another tenant reads as absent, so existence does not leak.

### Cross-repository dependencies

- Depends on the EG run-trace vocabulary row for `terminationReason`, `peakContextTokens`, `contextBudgetTokens`, and `harness_config`.
- Depends on the AU-HARNESS row that writes those fields at run end.
- Depends on the AU evaluation row that stores measured outcomes as engine facts.
- Unblocks the client rows that show cost, context use, and termination reason.

## GRAPHOS-OPS-R042 content screening and redaction

Data flow: POST route (graph_os/api/http/routes.py:127) → `invoke` (graph_os/api/invoke/pipeline.py:810) → screen → effect → redactor → response and trace.

Types: `ContentScreenVerdict`, `ScreenFinding` (class, span, rule id), `ContentOrigin`. The screen attaches in `invoke` after argument validation and before the effect; the redactor attaches after the effect. Failure behavior: quarantine returns a typed refusal, and a redactor error withholds the response.

Reused: the body bound at graph_os/api/http/routes.py:42 and the `OpError` refusal path. New: the screen, the redactor and the origin tag.

Decision: one screen inside the single pipeline boundary. Rejected alternative: per-route filters, which a new route may skip.

Cross-repository: engine memory-write refusal, owner prefix `EG`, contract name memory write origin.

### References

- no-arXiv: AI Agent Traps (Franklin et al., SSRN 6372438, 2026).
## Policy lifecycle operations that guard plans (GRAPHOS-OPS-R050)

Data flow: `policy.create` over POST /api/v1/ops/{op_id} -> `_base_authority` (graph_os/api/invoke/pipeline.py:208) -> `_policy` (:198) -> `PolicyAuthority.authorize` (graph_os/control_plane/policy/repository.py:179) -> audit record.

Types: `PolicyEntity`, `ConstitutionSource`, `PolicyChange`. Failure: `POLICY_DENIED` or `POLICY_UNAVAILABLE`, never a partial change.

Existing code: graph_os/control_plane/policy/repository.py:100, :117, :129, :179; graph_os/control_plane/policy/models.py:203; graph_os/api/ops/policy.py:36; graph_os/api/invoke/pipeline.py:198, :208, :218. Reachable today: no. `PolicyAuthority` has no production constructor.

Decision: GraphOS serves the operation and admits plans, and the engine decides violation. Rejected: a policy evaluator inside GraphOS, which duplicates engine decisions. Cross-repository: EG-DECISION-ENGINE-R001 owns policy decisions; GraphOS owns authenticated operation admission and transport. Clause interpretation requires a published engine producer contract, not an AU policy evaluator.

## Scoped security-evaluation operation (GRAPHOS-OPS-R051)

Data flow: `security.evaluate` over POST /api/v1/ops/{op_id} -> `_effect` (graph_os/api/invoke/pipeline.py:218) -> verified target authorization -> new public EG selection/verdict adapters (EG-DECISION-ENGINE-R001/R138) -> AU harness executes admitted probes -> engine verdict -> audit record. Runtime limits and trace commits use EG-DECISION-ENGINE-R133; Decide itself remains read-only.

Types: `EvaluationGoal`, `EvaluationCatalog`, `EvaluationScope`, `Finding`. Failure: `SUBJECT_ACCESS_DENIED` before any attack runs.

Existing code: graph_os/security/security_policy_middleware.py:52; graph_os/api/ops/registry_factory.py. Reachable today: no. Search terms used: red team, attack, scorer, jailbreak, target scope.

Decision: EG owns catalog facts, selection and probe verdicts (EG-DECISION-ENGINE-R001/R138); AU executes the selected probes; GraphOS admits by scope and serves the result. Reuse this R051 consumer instead of adding another security-evaluation rollup. Missing engine ports remain unavailable.

References:
- arXiv:2605.04019 — Dheekonda et al., "Redefining AI Red Teaming in the Agentic Era: From Weeks to Hours" (2026)

## Authorization-segmented virtual schema projections (GRAPHOS-OPS-R052)

Data flow: `query.projections` over POST /api/v1/ops/{op_id} -> `build_operation_runtime` (graph_os/api/invoke/bootstrap.py:167) -> `scope_covers` -> audit record.

Types: `SchemaProjection`. Failure: `SUBJECT_ACCESS_DENIED` or `UNAVAILABLE`.

Existing code: graph_os/api/ops/_common.py:106; graph_os/api/http/openapi.py:1; graph_os/control_plane/agents/repository.py:81. Reachable today: no for projections. Search terms used: virtual schema, schema_context, projection, authorization boundary.

Decision: GraphOS filters by the caller's verified grants over engine labels. Rejected: GraphOS-side boundary inference. Cross-repository: EG-IDENTITY-R001, EG-FEDERATED-QUERY-R001; GRAPHOS-DATA-MARKET-R005 owns schema_context.

## Bounded interaction space (GRAPHOS-OPS-R053)

Data flow: `retrieval.space` over POST /api/v1/ops/{op_id} -> the governed retriever entry (graph_os/control_plane/retrieval/service.py:61) -> `union_top_k` -> audit record.

Types: `InteractionSpace`, `CorpusBoundary`, `QueryDirectory`. Failure: `LOAD_CAP_EXCEEDED`, `INVALID_ARGUMENT`, `UNAVAILABLE`.

Existing code: graph_os/api/ops/retrieval.py:60, :77; graph_os/control_plane/retrieval/service.py:37, :61. Reachable today: no for the governed retriever; yes for `retrieval.search`. Search terms used: bm25, top_k, interaction space, working dir, corpus.

Decision: the engine ranks and GraphOS bounds the space. Rejected: returning ranked snippets only. Cross-repository: EG-FEDERATED-QUERY-R001 supplies query results, EG-DECISION-ENGINE-R029 chooses retrieval plans, EG-DECISION-ENGINE-R137 supplies context limits, and SDK-CONNECTOR-CONTROL-R005 supplies connector ingestion. AU executes engine-directed exploration.

References:
- arXiv:2606.06880 — Zhuang, Ni, Fun, Lin, Ma, "Towards Retrieving Interaction Spaces for Agentic Search" (2026)

Research citations above describe inspiration only. The engine and transport contracts, verified authority boundaries and served acceptance requirements define the architecture.

# Hosted API and intent operations — tasks

## Delivery tasks

See [design](plan.md) and [tests](test-spec.md).

- [ ] Validate public engine package contracts and exact method/error/scope coverage; identify one existing GraphOS entrypoint per domain.
- [ ] Implement immutable operation registry, digest, bindings, exclusions, and deterministic generation.
- [ ] Implement shared invocation steps, safe error mapping, durable plan lease, audit reservation/outcome, policy and service executor.
- [ ] Implement six MCP intent verbs, typed discovery/ranking, ten resident-tool boot invariant, and multi-kind fleet loading through `invoke`.
- [ ] Implement `/api/v1` operation and resource route factories, protocol-route inventory, generated OpenAPI and Python/TypeScript clients.
- [ ] Build the checked former-tool/route parity inventory, migrate domain handlers and generated-client consumers, then delete obsolete GraphOS registrars/routes and duplicate authority logic in one serving cutover.
- [ ] Prove contract, authorization, policy, idempotency, audit, negative and served cases in [test-spec.md](test-spec.md).
- [ ] Run pinned CCCC, KISS, jscpd, Dupehound, Python, package and CI gates from a clean public checkout; capture exact revision evidence.
- [ ] Review public documentation and mark only individually proven capabilities accepted.
- [x] **GRAPHOS-OPS-R021.1:** Decide operations (service-executed) — decide slice of `GRAPHOS-OPS-R021`.
- [ ] **GRAPHOS-OPS-R021.2:** Retrieval, context, and freshness read operations — retrieval slice of `GRAPHOS-OPS-R021`.
- [ ] **GRAPHOS-OPS-R021.2.1:** `retrieval.search` op
- [ ] **GRAPHOS-OPS-R021.2.2:** `retrieval.freshness` op
- [ ] **GRAPHOS-OPS-R021.3:** Policy and swarm read operations — policy slice of `GRAPHOS-OPS-R021`.
- [ ] **GRAPHOS-OPS-R021.4:** Run read operations — runs slice.
- [ ] **GRAPHOS-OPS-R021.4.1:** `RunTraceReader` port.
- [ ] **GRAPHOS-OPS-R021.4.2:** Engine adapter for run traces.
- [ ] **GRAPHOS-OPS-R021.4.3:** `runs.metrics` models.
- [ ] **GRAPHOS-OPS-R021.4.4:** `runs.metrics` operation.
- [ ] **GRAPHOS-OPS-R021.4.5:** `runs.get` operation.
- [ ] **GRAPHOS-OPS-R021.4.6:** Registration and generated artifacts.
- [ ] **GRAPHOS-OPS-R022.1:** Work-item and offer operations — work slice of `GRAPHOS-OPS-R022`.
- [ ] **GRAPHOS-OPS-R022.2:** Evolution loop, schedule, and proposal operations — evolution slice of `GRAPHOS-OPS-R022`.
- [ ] **GRAPHOS-OPS-R024.1:** Telemetry operations — telemetry slice of `GRAPHOS-OPS-R024`.
- [ ] **GRAPHOS-OPS-R024.2:** Security operations — security slice of `GRAPHOS-OPS-R024`.
- [ ] **GRAPHOS-OPS-R024.3:** Usage operations — usage slice of `GRAPHOS-OPS-R024`.
- [ ] **GRAPHOS-OPS-R024.4:** Memory operations — memory slice of `GRAPHOS-OPS-R024`.
- [ ] **GRAPHOS-OPS-R024.5:** Operational admin operations — admin slice of `GRAPHOS-OPS-R024` (graph/query/search/ontology excluded; owned by `GRAPHOS-HOST-R026.x`).
- [ ] **GRAPHOS-OPS-R026.1:** API-surface drift gate — api-surface slice of `GRAPHOS-OPS-R026`.
- [ ] **GRAPHOS-OPS-R026.2:** Backward-compatibility drift gate — backward-compat slice of `GRAPHOS-OPS-R026`.

## Decomposition children (tracked)

- [x] **GRAPHOS-OPS-R020:** Ingest operations through a typed SDK runner facade
- [x] **GRAPHOS-OPS-R020.1:** Typed ingest runner port and the source-sync entry point
- [x] **GRAPHOS-OPS-R020.2:** Repository indexing and source inventory operations
- [x] **GRAPHOS-OPS-R020.3:** Pack and job management operations
- [ ] **GRAPHOS-OPS-R020.4:** Drift listing and repair-with-approval operations
- [ ] **GRAPHOS-OPS-R020.5:** Embedding admission and re-embedding operations

## Decomposition children (tracked)

- [ ] **GRAPHOS-OPS-R017:** Finance and markets operations with tenant isolation
- [ ] **GRAPHOS-OPS-R017.1:** Remaining scope of GRAPHOS-OPS-R017 (slice .1): Finance and markets operations with tenant isolation
- [ ] **GRAPHOS-OPS-R017.2:** Remaining scope of GRAPHOS-OPS-R017 (slice .2): Finance and markets operations with tenant isolation
- [ ] **GRAPHOS-OPS-R018:** Query, search, ontology, and analytics operations
- [ ] **GRAPHOS-OPS-R018.1:** Remaining scope of GRAPHOS-OPS-R018 (slice .1): Query, search, ontology, and analytics operations
- [ ] **GRAPHOS-OPS-R018.2:** Remaining scope of GRAPHOS-OPS-R018 (slice .2): Query, search, ontology, and analytics operations
- [ ] **GRAPHOS-OPS-R019:** Federation source registration and sharing operations
- [ ] **GRAPHOS-OPS-R019.1:** Remaining scope of GRAPHOS-OPS-R019 (slice .1): Federation source registration and sharing operations
- [ ] **GRAPHOS-OPS-R019.2:** Remaining scope of GRAPHOS-OPS-R019 (slice .2): Federation source registration and sharing operations
- [x] **GRAPHOS-OPS-R020:** Ingest operations through a typed SDK runner facade
- [x] **GRAPHOS-OPS-R020.1:** Typed ingest runner port and the source-sync entry point
- [x] **GRAPHOS-OPS-R020.2:** Repository indexing and source inventory operations
- [x] **GRAPHOS-OPS-R020.3:** Pack and job management operations
- [ ] **GRAPHOS-OPS-R020.3.1:** Remaining scope of GRAPHOS-OPS-R020.3 (slice .1): Pack and job management operations
- [ ] **GRAPHOS-OPS-R020.3.1.1:** `ingest.packs.list` op
- [ ] **GRAPHOS-OPS-R020.3.1.2:** `ingest.jobs.status` op
- [ ] **GRAPHOS-OPS-R020.4:** Drift listing and repair-with-approval operations
- [ ] **GRAPHOS-OPS-R020.5:** Embedding admission and re-embedding operations
- [ ] **GRAPHOS-OPS-R020.3.1:** Remaining scope of GRAPHOS-OPS-R020.3 (slice .1): Pack and job management operations
- [ ] **GRAPHOS-OPS-R022.2.1:** `evolution.loops.status` op
- [ ] **GRAPHOS-OPS-R022.2.2:** `evolution.schedules.list` op
- [ ] **GRAPHOS-OPS-R022.2.3:** `evolution.proposals.list` op
- [ ] **GRAPHOS-OPS-R022.2.4:** `evolution.loops.run` op
- [ ] **GRAPHOS-OPS-R022.2.5:** `evolution.loops.pause` op
- [ ] **GRAPHOS-OPS-R031:** Published tool-to-operation parity inventory
- [ ] **GRAPHOS-OPS-R031.1:** Remaining scope of GRAPHOS-OPS-R031 (slice .1): Published tool-to-operation parity inventory
- [ ] **GRAPHOS-OPS-R032:** Identity dependency inversion via ports
- [ ] **GRAPHOS-OPS-R032.1:** Remaining scope of GRAPHOS-OPS-R032 (slice .1): Identity dependency inversion via ports
- [ ] **GRAPHOS-OPS-R032.2:** Remaining scope of GRAPHOS-OPS-R032 (slice .2): Identity dependency inversion via ports
- [ ] **GRAPHOS-OPS-R032.2.1:** Remaining scope of GRAPHOS-OPS-R032.2: Identity runtime constructor in the composition root.
- [ ] **GRAPHOS-OPS-R032.2.2:** Remaining scope of GRAPHOS-OPS-R032.2: Inject identity ports into mcp_server.
- [ ] **GRAPHOS-OPS-R032.2.3:** Remaining scope of GRAPHOS-OPS-R032.2: Inject identity ports into webui_host and webui_co_service.
- [ ] **GRAPHOS-OPS-R032.2.3.1:** Remaining scope of GRAPHOS-OPS-R032.2.3: compose_web_application requires injected identity ports.
- [ ] **GRAPHOS-OPS-R032.2.3.2:** Remaining scope of GRAPHOS-OPS-R032.2.3: Thread identity ports through run_web_ui and the webui_host package.
- [ ] **GRAPHOS-OPS-R032.2.3.2.1:** Remaining scope of GRAPHOS-OPS-R032.2.3.2: run_web_ui accepts and binds identity ports (with GRAPHOS-OPS-R032.2.3.1).
- [ ] **GRAPHOS-OPS-R032.2.3.2.2:** Remaining scope of GRAPHOS-OPS-R032.2.3.2: Supply identity ports from mcp_server composition (depends on GRAPHOS-OPS-R032.2.2).
- [ ] **GRAPHOS-OPS-R032.2.4:** Remaining scope of GRAPHOS-OPS-R032.2: Integration test of single injected identity runtime.
- [ ] **GRAPHOS-OPS-R035:** Multiplexer registration reduced to four resident tools
- [ ] **GRAPHOS-OPS-R035.1:** Remaining scope of GRAPHOS-OPS-R035 (slice .1): Multiplexer registration reduced to four resident tools
- [ ] **GRAPHOS-OPS-R035.2:** Remaining scope of GRAPHOS-OPS-R035 (slice .2): Multiplexer registration reduced to four resident tools
- [ ] **GRAPHOS-OPS-R039:** Dashboard route parity
- [ ] **GRAPHOS-OPS-R039.5:** Add one test in tests/gateway/test_dashboard_api
- [ ] **GRAPHOS-OPS-R039.6:** Replace the `agent_utilities
- [ ] **GRAPHOS-OPS-R039.7:** Replace `_active_engine` and `_hydration_manager` in graph_os/gateway/dashboard_api
- [ ] **GRAPHOS-OPS-R039.8:** Add one test in tests/gateway/test_registry
- [ ] **GRAPHOS-OPS-R039.9:** Replace the `agent_utilities
- [ ] **GRAPHOS-OPS-R040:** Live Artifacts routes
- [ ] **GRAPHOS-OPS-R040.1:** Cross-repo prerequisite
- [ ] **GRAPHOS-OPS-R040.9:** Cross-repo prerequisite (owner: agent-utilities): add the proposed `AU-BOUNDARY-R013
- [ ] **GRAPHOS-OPS-R040.10:** Add one test in tests/gateway/test_artifacts_route_mount
- [ ] **GRAPHOS-OPS-R041:** Agent summary widget dropped
- [ ] **GRAPHOS-OPS-R050:** Policy lifecycle operations that guard plans.
- [ ] **GRAPHOS-OPS-R050.1:** Model.
- [ ] **GRAPHOS-OPS-R050.2:** Behavior.
- [ ] **GRAPHOS-OPS-R050.3:** Wiring.
- [ ] **GRAPHOS-OPS-R050.4:** Refusal.
- [ ] **GRAPHOS-OPS-R050.5:** Evidence.
- [ ] **GRAPHOS-OPS-R050.6:** Hot-path proof.
- [ ] **GRAPHOS-OPS-R051:** Scoped security-evaluation operation.
- [ ] **GRAPHOS-OPS-R051.1:** Model.
- [ ] **GRAPHOS-OPS-R051.2:** Behavior.
- [ ] **GRAPHOS-OPS-R051.3:** Wiring.
- [ ] **GRAPHOS-OPS-R051.4:** Refusal.
- [ ] **GRAPHOS-OPS-R051.5:** Evidence.
- [ ] **GRAPHOS-OPS-R051.6:** Hot-path proof.
- [ ] **GRAPHOS-OPS-R052:** Authorization-segmented virtual schema projections.
- [ ] **GRAPHOS-OPS-R052.1:** Model.
- [ ] **GRAPHOS-OPS-R052.2:** Behavior.
- [ ] **GRAPHOS-OPS-R052.3:** Wiring.
- [ ] **GRAPHOS-OPS-R052.4:** Refusal.
- [ ] **GRAPHOS-OPS-R052.5:** Evidence.
- [ ] **GRAPHOS-OPS-R052.6:** Hot-path proof.
- [ ] **GRAPHOS-OPS-R053:** Bounded interaction space.
- [ ] **GRAPHOS-OPS-R053.1:** Model.
- [ ] **GRAPHOS-OPS-R053.2:** Behavior.
- [ ] **GRAPHOS-OPS-R053.3:** Wiring.
- [ ] **GRAPHOS-OPS-R053.4:** Refusal.
- [ ] **GRAPHOS-OPS-R053.5:** Evidence.
- [ ] **GRAPHOS-OPS-R053.6:** Hot-path proof.

## Versioned memory consumer sequence

- [ ] **GRAPHOS-OPS-R024.4.1:** Versioned memory port; implement the planned served positive and negative acceptance.
- [ ] **GRAPHOS-OPS-R024.4.2:** Snapshot reads and listing; implement the planned served positive and negative acceptance.
- [ ] **GRAPHOS-OPS-R024.4.3:** Versioned saves and conflicts; implement the planned served positive and negative acceptance.
- [ ] **GRAPHOS-OPS-R024.4.4:** Hosted materialization; implement the planned served positive and negative acceptance.
- [ ] **GRAPHOS-OPS-R024.4.5:** Authority and replay; implement the planned served positive and negative acceptance.
- [ ] **GRAPHOS-OPS-R024.4.6:** Registration and served proof; implement the planned served positive and negative acceptance.

<!-- spec-definition-patch:batch-20261012-tasks -->
- [ ] `GRAPHOS-OPS-R054`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R024.1.1`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R014.1`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R022.3`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R055`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R022.4`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R053.7`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R056`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R021.2.1.1`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R057`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R050.2.1`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R009.1`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R053.8`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R058`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R059`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R060`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R061`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R062`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R063`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R064`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R065`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R066`: Deliver every requirement in requirements.md.
<!-- /spec-definition-patch -->

<!-- spec-definition-patch:dispatch-20261012-hosted-api-operations-tasks -->
- [ ] `GRAPHOS-OPS-R054`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R024.1.1`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R014.1`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R022.3`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R055`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R022.4`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R053.7`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R056`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R021.2.1.1`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R057`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R050.2.1`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R009.1`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R053.8`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R058`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R059`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R060`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R061`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R062`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R063`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R064`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R065`: Deliver every requirement in requirements.md.
- [ ] `GRAPHOS-OPS-R066`: Deliver every requirement in requirements.md.
<!-- /spec-definition-patch -->

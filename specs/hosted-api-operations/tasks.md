# Hosted API and intent operations — tasks

## Delivery tasks

See [design](plan.md) and [tests](test-spec.md).

- [ ] Validate public engine package contracts and exact method/error/scope coverage; identify one existing Arbora entrypoint per domain.
- [ ] Implement immutable operation registry, digest, bindings, exclusions, and deterministic generation.
- [ ] Implement shared invocation steps, safe error mapping, durable plan lease, audit reservation/outcome, policy and service executor.
- [ ] Implement six MCP intent verbs, typed discovery/ranking, ten resident-tool boot invariant, and multi-kind fleet loading through `invoke`.
- [ ] Implement `/api/v1` operation and resource route factories, protocol-route inventory, generated OpenAPI and Python/TypeScript clients.
- [ ] Build the checked former-tool/route parity inventory, migrate domain handlers and generated-client consumers, then delete obsolete Arbora registrars/routes and duplicate authority logic in one serving cutover.
- [ ] Prove contract, authorization, policy, idempotency, audit, negative and served cases in [test-spec.md](test-spec.md).
- [ ] Run pinned CCCC, KISS, jscpd, Dupehound, Python, package and CI gates from a clean public checkout; capture exact revision evidence.
- [ ] Review public documentation and mark only individually proven capabilities accepted.
- [x] **ARBORA-OPS-R021.1:** Decide operations (service-executed) — decide slice of `ARBORA-OPS-R021`.
- [ ] **ARBORA-OPS-R021.2:** Retrieval, context, and freshness read operations — retrieval slice of `ARBORA-OPS-R021`.
- [ ] **ARBORA-OPS-R021.2.1:** `retrieval.search` op
- [ ] **ARBORA-OPS-R021.2.2:** `retrieval.freshness` op
- [ ] **ARBORA-OPS-R021.3:** Policy and swarm read operations — policy slice of `ARBORA-OPS-R021`.
- [ ] **ARBORA-OPS-R021.4:** Run read operations — runs slice.
- [ ] **ARBORA-OPS-R021.4.1:** `RunTraceReader` port.
- [ ] **ARBORA-OPS-R021.4.2:** Engine adapter for run traces.
- [ ] **ARBORA-OPS-R021.4.3:** `runs.metrics` models.
- [ ] **ARBORA-OPS-R021.4.4:** `runs.metrics` operation.
- [ ] **ARBORA-OPS-R021.4.5:** `runs.get` operation.
- [ ] **ARBORA-OPS-R021.4.6:** Registration and generated artifacts.
- [ ] **ARBORA-OPS-R022.1:** Work-item and offer operations — work slice of `ARBORA-OPS-R022`.
- [ ] **ARBORA-OPS-R022.2:** Evolution loop, schedule, and proposal operations — evolution slice of `ARBORA-OPS-R022`.
- [ ] **ARBORA-OPS-R024.1:** Telemetry operations — telemetry slice of `ARBORA-OPS-R024`.
- [ ] **ARBORA-OPS-R024.2:** Security operations — security slice of `ARBORA-OPS-R024`.
- [ ] **ARBORA-OPS-R024.3:** Usage operations — usage slice of `ARBORA-OPS-R024`.
- [ ] **ARBORA-OPS-R024.3.1:** Serve canonical caller-context committed session usage.
- [ ] **ARBORA-OPS-R024.3.2:** Project bounded AU measured request timing for shared clients.
- [ ] **ARBORA-OPS-R024.4:** Memory operations — memory slice of `ARBORA-OPS-R024`.
- [ ] **ARBORA-OPS-R024.5:** Operational admin operations — admin slice of `ARBORA-OPS-R024` (graph/query/search/ontology excluded; owned by `ARBORA-HOST-R026.x`).
- [ ] **ARBORA-OPS-R026.1:** API-surface drift gate — api-surface slice of `ARBORA-OPS-R026`.
- [ ] **ARBORA-OPS-R026.2:** Backward-compatibility drift gate — backward-compat slice of `ARBORA-OPS-R026`.

## Decomposition children (tracked)

- [x] **ARBORA-OPS-R020:** Ingest operations through a typed SDK runner facade
- [x] **ARBORA-OPS-R020.1:** Typed ingest runner port and the source-sync entry point
- [x] **ARBORA-OPS-R020.2:** Repository indexing and source inventory operations
- [x] **ARBORA-OPS-R020.3:** Pack and job management operations
- [ ] **ARBORA-OPS-R020.4:** Drift listing and repair-with-approval operations
- [ ] **ARBORA-OPS-R020.5:** Embedding admission and re-embedding operations

## Decomposition children (tracked)

- [ ] **ARBORA-OPS-R017:** Finance and markets operations with tenant isolation
- [ ] **ARBORA-OPS-R017.1:** Remaining scope of ARBORA-OPS-R017 (slice .1): Finance and markets operations with tenant isolation
- [ ] **ARBORA-OPS-R017.2:** Remaining scope of ARBORA-OPS-R017 (slice .2): Finance and markets operations with tenant isolation
- [ ] **ARBORA-OPS-R018:** Query, search, ontology, and analytics operations
- [ ] **ARBORA-OPS-R018.1:** Remaining scope of ARBORA-OPS-R018 (slice .1): Query, search, ontology, and analytics operations
- [ ] **ARBORA-OPS-R018.2:** Remaining scope of ARBORA-OPS-R018 (slice .2): Query, search, ontology, and analytics operations
- [ ] **ARBORA-OPS-R019:** Federation source registration and sharing operations
- [ ] **ARBORA-OPS-R019.1:** Remaining scope of ARBORA-OPS-R019 (slice .1): Federation source registration and sharing operations
- [ ] **ARBORA-OPS-R019.2:** Remaining scope of ARBORA-OPS-R019 (slice .2): Federation source registration and sharing operations
- [x] **ARBORA-OPS-R020:** Ingest operations through a typed SDK runner facade
- [x] **ARBORA-OPS-R020.1:** Typed ingest runner port and the source-sync entry point
- [x] **ARBORA-OPS-R020.2:** Repository indexing and source inventory operations
- [x] **ARBORA-OPS-R020.3:** Pack and job management operations
- [ ] **ARBORA-OPS-R020.3.1:** Remaining scope of ARBORA-OPS-R020.3 (slice .1): Pack and job management operations
- [ ] **ARBORA-OPS-R020.3.1.1:** `ingest.packs.list` op
- [ ] **ARBORA-OPS-R020.3.1.2:** `ingest.jobs.status` op
- [ ] **ARBORA-OPS-R020.4:** Drift listing and repair-with-approval operations
- [ ] **ARBORA-OPS-R020.5:** Embedding admission and re-embedding operations
- [ ] **ARBORA-OPS-R020.6:** Connector sources served through the composed SDK sync runner
- [ ] **ARBORA-OPS-R020.6.1:** Model
- [ ] **ARBORA-OPS-R020.6.2:** Behavior
- [ ] **ARBORA-OPS-R020.6.3:** Wiring
- [ ] **ARBORA-OPS-R020.6.4:** Refusal
- [ ] **ARBORA-OPS-R020.6.5:** Evidence
- [ ] **ARBORA-OPS-R020.6.6:** Hot-path proof
- [ ] **ARBORA-OPS-R020.3.1:** Remaining scope of ARBORA-OPS-R020.3 (slice .1): Pack and job management operations
- [ ] **ARBORA-OPS-R022.2.1:** `evolution.loops.status` op
- [ ] **ARBORA-OPS-R022.2.2:** `evolution.schedules.list` op
- [ ] **ARBORA-OPS-R022.2.3:** `evolution.proposals.list` op
- [ ] **ARBORA-OPS-R022.2.4:** `evolution.loops.run` op
- [ ] **ARBORA-OPS-R022.2.5:** `evolution.loops.pause` op
- [ ] **ARBORA-OPS-R031:** Published tool-to-operation parity inventory
- [ ] **ARBORA-OPS-R031.1:** Remaining scope of ARBORA-OPS-R031 (slice .1): Published tool-to-operation parity inventory
- [ ] **ARBORA-OPS-R032:** Identity dependency inversion via ports
- [ ] **ARBORA-OPS-R032.1:** Remaining scope of ARBORA-OPS-R032 (slice .1): Identity dependency inversion via ports
- [ ] **ARBORA-OPS-R032.2:** Remaining scope of ARBORA-OPS-R032 (slice .2): Identity dependency inversion via ports
- [ ] **ARBORA-OPS-R032.2.1:** Remaining scope of ARBORA-OPS-R032.2: Identity runtime constructor in the composition root.
- [ ] **ARBORA-OPS-R032.2.2:** Remaining scope of ARBORA-OPS-R032.2: Inject identity ports into mcp_server.
- [ ] **ARBORA-OPS-R032.2.3:** Remaining scope of ARBORA-OPS-R032.2: Inject identity ports into webui_host and webui_co_service.
- [ ] **ARBORA-OPS-R032.2.3.1:** Remaining scope of ARBORA-OPS-R032.2.3: compose_web_application requires injected identity ports.
- [ ] **ARBORA-OPS-R032.2.3.2:** Remaining scope of ARBORA-OPS-R032.2.3: Thread identity ports through run_web_ui and the webui_host package.
- [ ] **ARBORA-OPS-R032.2.3.2.1:** Remaining scope of ARBORA-OPS-R032.2.3.2: run_web_ui accepts and binds identity ports (with ARBORA-OPS-R032.2.3.1).
- [ ] **ARBORA-OPS-R032.2.3.2.2:** Remaining scope of ARBORA-OPS-R032.2.3.2: Supply identity ports from mcp_server composition (depends on ARBORA-OPS-R032.2.2).
- [ ] **ARBORA-OPS-R032.2.4:** Remaining scope of ARBORA-OPS-R032.2: Integration test of single injected identity runtime.
- [ ] **ARBORA-OPS-R035:** Multiplexer registration reduced to four resident tools
- [ ] **ARBORA-OPS-R035.1:** Remaining scope of ARBORA-OPS-R035 (slice .1): Multiplexer registration reduced to four resident tools
- [ ] **ARBORA-OPS-R035.2:** Remaining scope of ARBORA-OPS-R035 (slice .2): Multiplexer registration reduced to four resident tools
- [ ] **ARBORA-OPS-R039:** Dashboard route parity
- [ ] **ARBORA-OPS-R039.5:** Add one test in tests/gateway/test_dashboard_api
- [ ] **ARBORA-OPS-R039.6:** Replace the `agent_utilities
- [ ] **ARBORA-OPS-R039.7:** Replace `_active_engine` and `_hydration_manager` in arbora/gateway/dashboard_api
- [ ] **ARBORA-OPS-R039.8:** Add one test in tests/gateway/test_registry
- [ ] **ARBORA-OPS-R039.9:** Replace the `agent_utilities
- [ ] **ARBORA-OPS-R040:** Live Artifacts routes
- [ ] **ARBORA-OPS-R040.1:** Cross-repo prerequisite
- [ ] **ARBORA-OPS-R040.9:** Cross-repo prerequisite (owner: agent-utilities): add the proposed `AU-BOUNDARY-R013
- [ ] **ARBORA-OPS-R040.10:** Add one test in tests/gateway/test_artifacts_route_mount
- [ ] **ARBORA-OPS-R041:** Agent summary widget dropped
- [ ] **ARBORA-OPS-R050:** Policy lifecycle operations that guard plans.
- [ ] **ARBORA-OPS-R050.1:** Model.
- [ ] **ARBORA-OPS-R050.2:** Behavior.
- [ ] **ARBORA-OPS-R050.3:** Wiring.
- [ ] **ARBORA-OPS-R050.4:** Refusal.
- [ ] **ARBORA-OPS-R050.5:** Evidence.
- [ ] **ARBORA-OPS-R050.6:** Hot-path proof.
- [ ] **ARBORA-OPS-R051:** Scoped security-evaluation operation.
- [ ] **ARBORA-OPS-R051.1:** Model.
- [ ] **ARBORA-OPS-R051.2:** Behavior.
- [ ] **ARBORA-OPS-R051.3:** Wiring.
- [ ] **ARBORA-OPS-R051.4:** Refusal.
- [ ] **ARBORA-OPS-R051.5:** Evidence.
- [ ] **ARBORA-OPS-R051.6:** Hot-path proof.
- [ ] **ARBORA-OPS-R052:** Authorization-segmented virtual schema projections.
- [ ] **ARBORA-OPS-R052.1:** Model.
- [ ] **ARBORA-OPS-R052.2:** Behavior.
- [ ] **ARBORA-OPS-R052.3:** Wiring.
- [ ] **ARBORA-OPS-R052.4:** Refusal.
- [ ] **ARBORA-OPS-R052.5:** Evidence.
- [ ] **ARBORA-OPS-R052.6:** Hot-path proof.
- [ ] **ARBORA-OPS-R053:** Bounded interaction space.
- [ ] **ARBORA-OPS-R053.1:** Model.
- [ ] **ARBORA-OPS-R053.2:** Behavior.
- [ ] **ARBORA-OPS-R053.3:** Wiring.
- [ ] **ARBORA-OPS-R053.4:** Refusal.
- [ ] **ARBORA-OPS-R053.5:** Evidence.
- [ ] **ARBORA-OPS-R053.6:** Hot-path proof.

## Versioned memory consumer sequence

- [ ] **ARBORA-OPS-R024.4.1:** Versioned memory port; implement the planned served positive and negative acceptance.
- [ ] **ARBORA-OPS-R024.4.2:** Snapshot reads and listing; implement the planned served positive and negative acceptance.
- [ ] **ARBORA-OPS-R024.4.3:** Versioned saves and conflicts; implement the planned served positive and negative acceptance.
- [ ] **ARBORA-OPS-R024.4.4:** Hosted materialization; implement the planned served positive and negative acceptance.
- [ ] **ARBORA-OPS-R024.4.5:** Authority and replay; implement the planned served positive and negative acceptance.
- [ ] **ARBORA-OPS-R024.4.6:** Registration and served proof; implement the planned served positive and negative acceptance.

## Owner-bound projections (ARBORA-OPS-R054–R058, R061, R063)

- [ ] **R054:** Resolve the AU-INTEGRATION-R014 producer operation and receipt; keep generic RLM benchmarking separate.
- [ ] **R055:** Resolve AU liveness/proposal contracts before exposing remediation; preserve explicit run/process approval.
- [ ] **R056:** Resolve the EG/AU review operation, finding schema, and impact-filter contract; leave `decide.commit` unchanged.
- [ ] **R057:** Resolve EG R094 and SDK source-effect/receipt contracts before defining the Arbora serializer or difference oracle.
- [ ] **R058:** Resolve WebUI table-control/statistics requirements and EG producer bindings before extending schema-context reads.
- [ ] **ARBORA-OPS-R061:** Define closed indexed-usage query/projection models and the EG reader in usage.py. Bind the tenant to the authenticated caller principal; ignore client-supplied tenant values. Preserve producer counters, exact totals, pinned PriceTableV1, revisions, cursor, unknown reasons, and the R010.101 latency profile. Keep runs.metrics and context budgets distinct.
- [ ] **ARBORA-OPS-R061:** Wire the existing registry/invoke path to ObservabilityView.tsx. Specify served authorization, independent-ledger, pagination, unknown-value, stale-revision, and latency-profile acceptance.
- [ ] **ARBORA-OPS-R063:** Define typed evaluation and context reads in arbora/api/ops/retrieval.py. Bind the EG R100.100 ranking receipt and context method. Require identical inputs across registered algorithms. Preserve original-text UTF-8 offsets, revisions, authentication, and span bounds.
- [ ] **ARBORA-OPS-R063:** Wire the shared registry/invoke route to the WebUI comparison panel. Specify served two-algorithm and independent-metric checks. Cover Unicode offsets, stale or denied access, missing labels, invalid maps, and over-limit spans.

## Additional owner requirements

- [ ] `ARBORA-OPS-R024.1.1`: Deliver every requirement in requirements.md.
- [ ] `ARBORA-OPS-R014.1`: Deliver every requirement in requirements.md.
- [ ] `ARBORA-OPS-R022.3`: Deliver every requirement in requirements.md.
- [ ] `ARBORA-OPS-R022.4`: Deliver every requirement in requirements.md.
- [ ] `ARBORA-OPS-R053.7`: Deliver every requirement in requirements.md.
- [ ] `ARBORA-OPS-R021.2.1.1`: Deliver every requirement in requirements.md.
- [ ] `ARBORA-OPS-R050.2.1`: Deliver every requirement in requirements.md.
- [ ] `ARBORA-OPS-R009.1`: Deliver every requirement in requirements.md.
- [ ] **ARBORA-OPS-R009.2:** Refusal causes are logged
- [ ] **ARBORA-OPS-R009.3:** Declared replay owners
- [ ] `ARBORA-OPS-R053.8`: Deliver every requirement in requirements.md.
- [ ] `ARBORA-OPS-R059`: Resolve the producer contracts named in plan.md. Then implement the existing registry attachment and its served proof. Preserve each specified refusal.
- [ ] `ARBORA-OPS-R060`: Resolve the producer contracts named in plan.md. Then implement the existing registry attachment and its served proof. Preserve each specified refusal.
- [ ] `ARBORA-OPS-R062`: Resolve the producer contracts named in plan.md. Then implement the existing registry attachment and its served proof. Preserve each specified refusal.
- [ ] `ARBORA-OPS-R064`: Resolve the producer contracts named in plan.md. Then implement the existing registry attachment and its served proof. Preserve each specified refusal.
- [ ] `ARBORA-OPS-R065`: Resolve the producer contracts named in plan.md. Then implement the existing registry attachment and its served proof. Preserve each specified refusal.
- [ ] `ARBORA-OPS-R066`: Resolve the producer contracts named in plan.md. Then implement the existing registry attachment and its served proof. Preserve each specified refusal.

- [ ] **ARBORA-OPS-R025.1:** Validate operation membership and response digest in the generated Python invoker.

- [ ] **ARBORA-OPS-R025.2:** Complete deterministic artifact generation and installed-wheel acceptance from the pinned engine contract.

- [ ] **ARBORA-OPS-R018.2.1:** Distinct query declaration, resolver and caller dispatch contracts; live provider planner validation deferred.
- [ ] **ARBORA-OPS-R018.2.2:** Remaining served query, SQL, search, indexes and analytics acceptance.

- [ ] **ARBORA-OPS-R801:** Graph node and relationship browse.
- [ ] **ARBORA-OPS-R802:** Graph level-of-detail view.
- [ ] **ARBORA-OPS-R803:** Graph statistics.
- [ ] **ARBORA-OPS-R804:** Graph schema and node types.
- [ ] **ARBORA-OPS-R805:** Graph edge write.
- [ ] **ARBORA-OPS-R806:** Graph impact analysis.
- [ ] **ARBORA-OPS-R807:** Code navigation context.
- [ ] **ARBORA-OPS-R808:** Knowledge base articles.
- [ ] **ARBORA-OPS-R809:** Spec-driven development documents.
- [ ] **ARBORA-OPS-R810:** Agent library list and write.
- [ ] **ARBORA-OPS-R811:** Agent library reload.
- [ ] **ARBORA-OPS-R812:** Autonomous goal run.
- [ ] **ARBORA-OPS-R813:** Graph maintenance run.
- [ ] **ARBORA-OPS-R814:** Ingestion pipeline run.
- [ ] **ARBORA-OPS-R815:** Graph prompts.
- [ ] **ARBORA-OPS-R816:** Prompt library read.
- [ ] **ARBORA-OPS-R817:** MCP server registry management.
- [ ] **ARBORA-OPS-R818:** Scheduled task read.
- [ ] **ARBORA-OPS-R819:** Workflow run and write.
- [ ] **ARBORA-OPS-R820:** Ontology object explorer.
- [ ] **ARBORA-OPS-R821:** Markets signals read.
- [ ] **ARBORA-OPS-R822:** Bearer identity verification.
- [ ] **ARBORA-OPS-R823:** Claim lifecycle and perspective arbitration.
- [ ] **ARBORA-OPS-R824:** Skill evolution promotion gate.
- [ ] **ARBORA-OPS-R825:** Feedback corrections.
- [ ] **ARBORA-OPS-R826:** Fleet event remediation routing.
- [ ] **ARBORA-OPS-R827:** Comparative-analysis skill.
- [ ] **ARBORA-OPS-R828:** Usage ledger and price catalog.
- [ ] **ARBORA-OPS-R829:** Incident and health-anomaly records.
- [ ] **ARBORA-OPS-R830:** Graph ownership inference.
- [ ] **ARBORA-OPS-R831:** Retrieval search composition.
- [ ] **ARBORA-OPS-R832:** UQL shape validation stage.
- [ ] **ARBORA-OPS-R833:** Ontology-grounded extraction.
- [ ] **ARBORA-OPS-R834:** Run provenance and trace views.
- [ ] **ARBORA-OPS-R835:** Orchestration competency questions.
- [ ] **ARBORA-OPS-R836:** Placement and rebalance plans.
- [ ] **ARBORA-OPS-R837:** Service-ownership links for repositories.
- [ ] **ARBORA-OPS-R838:** Foreign source registration and intake.
- [ ] **ARBORA-OPS-R839:** Code health sweep.
- [ ] **ARBORA-OPS-R840:** Session transcript and feed intake.
- [ ] **ARBORA-OPS-R841:** Skill pack validation.
- [ ] **ARBORA-OPS-R842:** Governed actions.

- [ ] **ARBORA-OPS-R843:** Served engine operations replace the in-run agent-utilities tool wrappers.
- [x] **ARBORA-OPS-R843.1:** Delivered wrapper behaviour, bound to `tests/api/test_au_tool_parity.py`.
- [ ] **ARBORA-OPS-R843.2:** Re-point the remaining modules to their composed rows or the harness.

## Web research operation (R067)

- [ ] **ARBORA-OPS-R067:** Web research operation composes search, selection, fetch and ingest.
- [ ] **ARBORA-OPS-R067.1:** Model. Add the params and the result with citations.
- [ ] **ARBORA-OPS-R067.2:** Behavior. Compose the four steps and the deadline.
- [ ] **ARBORA-OPS-R067.3:** Backend. Add the explicit choice, the fallback and the recorded backend.
- [ ] **ARBORA-OPS-R067.4:** Wiring. Register the operation behind `invoke`.
- [ ] **ARBORA-OPS-R067.5:** Refusal. Add the typed refusals.
- [ ] **ARBORA-OPS-R067.6:** Evidence. Record the trace fields.
- [ ] **ARBORA-OPS-R067.7:** Hot-path proof. Add the served three-call test.

## Served harness component and improvement library operations (R068)

- [ ] **ARBORA-OPS-R068:** Serve the harness component and improvement library operations.
- [ ] **ARBORA-OPS-R068.1:** Component reads. Add `harness.components.list` and `harness.components.get`.
- [ ] **ARBORA-OPS-R068.2:** Lesson query. Add `harness.lessons.retrieve`.
- [ ] **ARBORA-OPS-R068.3:** Evolution report. Add `harness.evolution.report`.
- [ ] **ARBORA-OPS-R068.4:** Adoption. Add the service-only `harness.evolution.adopt`.
- [ ] **ARBORA-OPS-R068.5:** Registration gate. Keep `harness.*` unregistered until the engine release publishes their senders.


- [ ] **ARBORA-OPS-R021.4.5.1.1:** Compose the stateless native reader and qualify source-level authority/projection checks; retain the installed-EG acceptance blocker.
- [ ] **ARBORA-OPS-R021.4.5.1.2:** Compose native run admission, lease-bound scaffold publication and matching graph reads; qualify a real native lifecycle.

## Served source operations (R848)

- [x] **ARBORA-OPS-R848.1:** Serve the source reads: `source.tree`, `source.blob`, `source.history` and `source.blame` through the typed `SourceReader` port under the caller's engine scopes.
- [ ] **ARBORA-OPS-R848.2:** Serve the source writes: `source.commit`, `source.land` and `source.revert` with destructive hints (pending the engine changeset substrate).
- [ ] **ARBORA-OPS-R848.3:** Serve the `source.diff` read.
- [ ] **ARBORA-OPS-R849:** Apply the server-side landing policy with fast and full gate phases.
- [x] **ARBORA-OPS-R850:** Stream the virtual checkout endpoint, recursively and resumably.
- [ ] **ARBORA-OPS-R851:** Serve the git smart-HTTP read bridge over EG trees.

- [ ] **ARBORA-OPS-R848.4:** Graph-native symbol edits.
- [ ] **ARBORA-OPS-R852:** Harness optimize, promote and rollback operations (rollup).
- [ ] **ARBORA-OPS-R852.1:** Wiring.
- [ ] **ARBORA-OPS-R852.2:** Hot-path proof.
- [ ] **ARBORA-OPS-R853:** Lifecycle operations (rollup).
- [ ] **ARBORA-OPS-R853.1:** Wiring.
- [ ] **ARBORA-OPS-R853.2:** Hot-path proof.
- [ ] **ARBORA-OPS-R854:** Context engine operations (rollup).
- [ ] **ARBORA-OPS-R854.1:** Wiring.
- [ ] **ARBORA-OPS-R854.2:** Hot-path proof.
- [ ] **ARBORA-OPS-R855:** Catalog and governance operations (rollup).
- [ ] **ARBORA-OPS-R855.1:** Wiring.
- [ ] **ARBORA-OPS-R855.2:** Hot-path proof.
- [ ] **ARBORA-OPS-R856:** Quality and lineage operations (rollup).
- [ ] **ARBORA-OPS-R856.1:** Wiring.
- [ ] **ARBORA-OPS-R856.2:** Hot-path proof.
- [ ] **ARBORA-OPS-R857:** SPARQL 1.1 protocol endpoint (rollup).
- [ ] **ARBORA-OPS-R857.1:** Wiring.
- [ ] **ARBORA-OPS-R857.2:** Hot-path proof.
- [ ] **ARBORA-OPS-R858:** Outside-reader roles for the row engine (rollup).
- [ ] **ARBORA-OPS-R858.1:** Wiring.
- [ ] **ARBORA-OPS-R858.2:** Hot-path proof.

- [ ] **ARBORA-OPS-R855.3:** Trust flag, curation review and query workspace operations.

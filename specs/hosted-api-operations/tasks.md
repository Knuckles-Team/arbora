# Hosted API and intent operations — tasks

## Delivery tasks

See [design](plan.md) and [tests](test-spec.md).

Each task line reads `- [ ] T001 [P] [US1] <ID>-R012.4 description`. The task number and the row ID are required. `[P]` marks a task that may run in parallel. Order tasks producer first.

- Validate public engine package contracts and exact method/error/scope coverage; identify one existing Arbora entrypoint per domain.
- Implement immutable operation registry, digest, bindings, exclusions, and deterministic generation.
- Implement shared invocation steps, safe error mapping, durable plan lease, audit reservation/outcome, policy and service executor.
- Implement six MCP intent verbs, typed discovery/ranking, ten resident-tool boot invariant, and multi-kind fleet loading through `invoke`.
- Implement `/api/v1` operation and resource route factories, protocol-route inventory, generated OpenAPI and Python/TypeScript clients.
- Build the checked former-tool/route parity inventory, migrate domain handlers and generated-client consumers, then delete obsolete Arbora registrars/routes and duplicate authority logic in one serving cutover.
- Prove contract, authorization, policy, idempotency, audit, negative and served cases in [test-spec.md](test-spec.md).
- Run pinned CCCC, KISS, jscpd, Dupehound, Python, package and CI gates from a clean public checkout; capture exact revision evidence.
- Review public documentation and mark only individually proven capabilities accepted.
- [x] T001 ARBORA-OPS-R021.1 Decide operations (service-executed) — decide slice of `ARBORA-OPS-R021`.
- [ ] T002 ARBORA-OPS-R021.2 Retrieval, context, and freshness read operations — retrieval slice of `ARBORA-OPS-R021`.
- [ ] T003 ARBORA-OPS-R021.2.1 retrieval.search` op
- [ ] T004 ARBORA-OPS-R021.2.2 retrieval.freshness` op
- [ ] T005 ARBORA-OPS-R021.3 Policy and swarm read operations — policy slice of `ARBORA-OPS-R021`.
- [ ] T006 ARBORA-OPS-R021.4 Run read operations — runs slice.
- [ ] T007 ARBORA-OPS-R021.4.1 RunTraceReader` port.
- [ ] T008 ARBORA-OPS-R021.4.2 Engine adapter for run traces.
- [ ] T009 ARBORA-OPS-R021.4.3 runs.metrics` models.
- [ ] T010 ARBORA-OPS-R021.4.4 runs.metrics` operation.
- [ ] T011 ARBORA-OPS-R021.4.5 runs.get` operation.
- [ ] T012 ARBORA-OPS-R021.4.6 Registration and generated artifacts.
- [ ] T013 ARBORA-OPS-R022.1 Work-item and offer operations — work slice of `ARBORA-OPS-R022`.
- [ ] T014 ARBORA-OPS-R022.2 Evolution loop, schedule, and proposal operations — evolution slice of `ARBORA-OPS-R022`.
- [ ] T015 ARBORA-OPS-R024.1 Telemetry operations — telemetry slice of `ARBORA-OPS-R024`.
- [ ] T016 ARBORA-OPS-R024.2 Security operations — security slice of `ARBORA-OPS-R024`.
- [ ] T017 ARBORA-OPS-R024.3 Usage operations — usage slice of `ARBORA-OPS-R024`.
- [ ] T018 ARBORA-OPS-R024.3.1 Serve canonical caller-context committed session usage.
- [ ] T019 ARBORA-OPS-R024.3.2 Project bounded AU measured request timing for shared clients.
- [ ] T020 ARBORA-OPS-R024.4 Memory operations — memory slice of `ARBORA-OPS-R024`.
- [ ] T021 ARBORA-OPS-R024.5 Operational admin operations — admin slice of `ARBORA-OPS-R024` (graph/query/search/ontology excluded; owned by `ARBORA-HOST-R026.x`).
- [ ] T022 ARBORA-OPS-R026.1 API-surface drift gate — api-surface slice of `ARBORA-OPS-R026`.
- [ ] T023 ARBORA-OPS-R026.2 Backward-compatibility drift gate — backward-compat slice of `ARBORA-OPS-R026`.

## Decomposition children (tracked)

- [x] T024 ARBORA-OPS-R020 Ingest operations through a typed SDK runner facade
- [x] T025 ARBORA-OPS-R020.1 Typed ingest runner port and the source-sync entry point
- [x] T026 ARBORA-OPS-R020.2 Repository indexing and source inventory operations
- [x] T027 ARBORA-OPS-R020.3 Pack and job management operations
- [ ] T028 ARBORA-OPS-R020.4 Drift listing and repair-with-approval operations
- [ ] T029 ARBORA-OPS-R020.5 Embedding admission and re-embedding operations

## Decomposition children (tracked)

- [ ] T030 ARBORA-OPS-R017 Finance and markets operations with tenant isolation
- [ ] T031 ARBORA-OPS-R017.1 Remaining scope of ARBORA-OPS-R017 (slice .1): Finance and markets operations with tenant isolation
- [ ] T032 ARBORA-OPS-R017.2 Remaining scope of ARBORA-OPS-R017 (slice .2): Finance and markets operations with tenant isolation
- [ ] T033 ARBORA-OPS-R018 Query, search, ontology, and analytics operations
- [ ] T034 ARBORA-OPS-R018.1 Remaining scope of ARBORA-OPS-R018 (slice .1): Query, search, ontology, and analytics operations
- [ ] T035 ARBORA-OPS-R018.2 Remaining scope of ARBORA-OPS-R018 (slice .2): Query, search, ontology, and analytics operations
- [ ] T036 ARBORA-OPS-R019 Federation source registration and sharing operations
- [ ] T037 ARBORA-OPS-R019.1 Remaining scope of ARBORA-OPS-R019 (slice .1): Federation source registration and sharing operations
- [ ] T038 ARBORA-OPS-R019.2 Remaining scope of ARBORA-OPS-R019 (slice .2): Federation source registration and sharing operations
- [x] T039 ARBORA-OPS-R020 Ingest operations through a typed SDK runner facade
- [x] T040 ARBORA-OPS-R020.1 Typed ingest runner port and the source-sync entry point
- [x] T041 ARBORA-OPS-R020.2 Repository indexing and source inventory operations
- [x] T042 ARBORA-OPS-R020.3 Pack and job management operations
- [ ] T043 ARBORA-OPS-R020.3.1 Remaining scope of ARBORA-OPS-R020.3 (slice .1): Pack and job management operations
- [ ] T044 ARBORA-OPS-R020.3.1.1 ingest.packs.list` op
- [ ] T045 ARBORA-OPS-R020.3.1.2 ingest.jobs.status` op
- [ ] T046 ARBORA-OPS-R020.4 Drift listing and repair-with-approval operations
- [ ] T047 ARBORA-OPS-R020.5 Embedding admission and re-embedding operations
- [ ] T048 ARBORA-OPS-R020.6 Connector sources served through the composed SDK sync runner
- [ ] T049 ARBORA-OPS-R020.6.1 Model
- [ ] T050 ARBORA-OPS-R020.6.2 Behavior
- [ ] T051 ARBORA-OPS-R020.6.3 Wiring
- [ ] T052 ARBORA-OPS-R020.6.4 Refusal
- [ ] T053 ARBORA-OPS-R020.6.5 Evidence
- [ ] T054 ARBORA-OPS-R020.6.6 Hot-path proof
- [ ] T055 ARBORA-OPS-R020.6.7 Pack inventory
- [ ] T056 ARBORA-OPS-R020.6.8 Failure cause on the receipt, the job and the log; child SSE event bound
- [ ] T057 ARBORA-OPS-R020.3.1 Remaining scope of ARBORA-OPS-R020.3 (slice .1): Pack and job management operations
- [ ] T058 ARBORA-OPS-R022.2.1 evolution.loops.status` op
- [ ] T059 ARBORA-OPS-R022.2.2 evolution.schedules.list` op
- [ ] T060 ARBORA-OPS-R022.2.3 evolution.proposals.list` op
- [ ] T061 ARBORA-OPS-R022.2.4 evolution.loops.run` op
- [ ] T062 ARBORA-OPS-R022.2.5 evolution.loops.pause` op
- [ ] T063 ARBORA-OPS-R031 Published tool-to-operation parity inventory
- [ ] T064 ARBORA-OPS-R031.1 Remaining scope of ARBORA-OPS-R031 (slice .1): Published tool-to-operation parity inventory
- [ ] T065 ARBORA-OPS-R032 Identity dependency inversion via ports
- [ ] T066 ARBORA-OPS-R032.1 Remaining scope of ARBORA-OPS-R032 (slice .1): Identity dependency inversion via ports
- [ ] T067 ARBORA-OPS-R032.2 Remaining scope of ARBORA-OPS-R032 (slice .2): Identity dependency inversion via ports
- [ ] T068 ARBORA-OPS-R032.2.1 Remaining scope of ARBORA-OPS-R032.2: Identity runtime constructor in the composition root.
- [ ] T069 ARBORA-OPS-R032.2.2 Remaining scope of ARBORA-OPS-R032.2: Inject identity ports into mcp_server.
- [ ] T070 ARBORA-OPS-R032.2.3 Remaining scope of ARBORA-OPS-R032.2: Inject identity ports into webui_host and webui_co_service.
- [ ] T071 ARBORA-OPS-R032.2.3.1 Remaining scope of ARBORA-OPS-R032.2.3: compose_web_application requires injected identity ports.
- [ ] T072 ARBORA-OPS-R032.2.3.2 Remaining scope of ARBORA-OPS-R032.2.3: Thread identity ports through run_web_ui and the webui_host package.
- [ ] T073 ARBORA-OPS-R032.2.3.2.1 Remaining scope of ARBORA-OPS-R032.2.3.2: run_web_ui accepts and binds identity ports (with ARBORA-OPS-R032.2.3.1).
- [ ] T074 ARBORA-OPS-R032.2.3.2.2 Remaining scope of ARBORA-OPS-R032.2.3.2: Supply identity ports from mcp_server composition (depends on ARBORA-OPS-R032.2.2).
- [ ] T075 ARBORA-OPS-R032.2.4 Remaining scope of ARBORA-OPS-R032.2: Integration test of single injected identity runtime.
- [ ] T076 ARBORA-OPS-R035 Multiplexer registration reduced to four resident tools
- [ ] T077 ARBORA-OPS-R035.1 Remaining scope of ARBORA-OPS-R035 (slice .1): Multiplexer registration reduced to four resident tools
- [ ] T078 ARBORA-OPS-R035.2 Remaining scope of ARBORA-OPS-R035 (slice .2): Multiplexer registration reduced to four resident tools
- [ ] T079 ARBORA-OPS-R039 Dashboard route parity
- [ ] T080 ARBORA-OPS-R039.5 Add one test in tests/gateway/test_dashboard_api
- [ ] T081 ARBORA-OPS-R039.6 Replace the `agent_utilities
- [ ] T082 ARBORA-OPS-R039.7 Replace `_active_engine` and `_hydration_manager` in arbora/gateway/dashboard_api
- [ ] T083 ARBORA-OPS-R039.8 Add one test in tests/gateway/test_registry
- [ ] T084 ARBORA-OPS-R039.9 Replace the `agent_utilities
- [ ] T085 ARBORA-OPS-R040 Live Artifacts routes
- [ ] T086 ARBORA-OPS-R040.1 Cross-repo prerequisite
- [ ] T087 ARBORA-OPS-R040.9 Cross-repo prerequisite (owner: meristem): add the proposed `MERISTEM-BOUNDARY-R013
- [ ] T088 ARBORA-OPS-R040.10 Add one test in tests/gateway/test_artifacts_route_mount
- [ ] T089 ARBORA-OPS-R041 Agent summary widget dropped
- [ ] T090 ARBORA-OPS-R050 Policy lifecycle operations that guard plans.
- [ ] T091 ARBORA-OPS-R050.1 Model.
- [ ] T092 ARBORA-OPS-R050.2 Behavior.
- [ ] T093 ARBORA-OPS-R050.3 Wiring.
- [ ] T094 ARBORA-OPS-R050.4 Refusal.
- [ ] T095 ARBORA-OPS-R050.5 Evidence.
- [ ] T096 ARBORA-OPS-R050.6 Hot-path proof.
- [ ] T097 ARBORA-OPS-R051 Scoped security-evaluation operation.
- [ ] T098 ARBORA-OPS-R051.1 Model.
- [ ] T099 ARBORA-OPS-R051.2 Behavior.
- [ ] T100 ARBORA-OPS-R051.3 Wiring.
- [ ] T101 ARBORA-OPS-R051.4 Refusal.
- [ ] T102 ARBORA-OPS-R051.5 Evidence.
- [ ] T103 ARBORA-OPS-R051.6 Hot-path proof.
- [ ] T104 ARBORA-OPS-R052 Authorization-segmented virtual schema projections.
- [ ] T105 ARBORA-OPS-R052.1 Model.
- [ ] T106 ARBORA-OPS-R052.2 Behavior.
- [ ] T107 ARBORA-OPS-R052.3 Wiring.
- [ ] T108 ARBORA-OPS-R052.4 Refusal.
- [ ] T109 ARBORA-OPS-R052.5 Evidence.
- [ ] T110 ARBORA-OPS-R052.6 Hot-path proof.
- [ ] T111 ARBORA-OPS-R053 Bounded interaction space.
- [ ] T112 ARBORA-OPS-R053.1 Model.
- [ ] T113 ARBORA-OPS-R053.2 Behavior.
- [ ] T114 ARBORA-OPS-R053.3 Wiring.
- [ ] T115 ARBORA-OPS-R053.4 Refusal.
- [ ] T116 ARBORA-OPS-R053.5 Evidence.
- [ ] T117 ARBORA-OPS-R053.6 Hot-path proof.

## Versioned memory consumer sequence

- [ ] T118 ARBORA-OPS-R024.4.1 Versioned memory port; implement the planned served positive and negative acceptance.
- [ ] T119 ARBORA-OPS-R024.4.2 Snapshot reads and listing; implement the planned served positive and negative acceptance.
- [ ] T120 ARBORA-OPS-R024.4.3 Versioned saves and conflicts; implement the planned served positive and negative acceptance.
- [ ] T121 ARBORA-OPS-R024.4.4 Hosted materialization; implement the planned served positive and negative acceptance.
- [ ] T122 ARBORA-OPS-R024.4.5 Authority and replay; implement the planned served positive and negative acceptance.
- [ ] T123 ARBORA-OPS-R024.4.6 Registration and served proof; implement the planned served positive and negative acceptance.

## Owner-bound projections (ARBORA-OPS-R054–R058, R061, R063)

- **R055:** Resolve AU liveness/proposal contracts before exposing remediation; preserve explicit run/process approval.
- **R056:** Resolve the EG/AU review operation, finding schema, and impact-filter contract; leave `decide.commit` unchanged.
- **R057:** Resolve EG R094 and SDK source-effect/receipt contracts before defining the Arbora serializer or difference oracle.
- **R058:** Resolve WebUI table-control/statistics requirements and EG producer bindings before extending schema-context reads.
- [ ] T124 ARBORA-OPS-R061 Define closed indexed-usage query/projection models and the EG reader in usage.py. Bind the tenant to the authenticated caller principal; ignore client-supplied tenant values. Preserve producer counters, exact totals, pinned PriceTableV1, revisions, cursor, unknown reasons, and the R010.101 latency profile. Keep runs.metrics and context budgets distinct.
- [ ] T125 ARBORA-OPS-R061 Wire the existing registry/invoke path to ObservabilityView.tsx. Specify served authorization, independent-ledger, pagination, unknown-value, stale-revision, and latency-profile acceptance.
- [ ] T126 ARBORA-OPS-R063 Define typed evaluation and context reads in arbora/api/ops/retrieval.py. Bind the EG R100.100 ranking receipt and context method. Require identical inputs across registered algorithms. Preserve original-text UTF-8 offsets, revisions, authentication, and span bounds.
- [ ] T127 ARBORA-OPS-R063 Wire the shared registry/invoke route to the WebUI comparison panel. Specify served two-algorithm and independent-metric checks. Cover Unicode offsets, stale or denied access, missing labels, invalid maps, and over-limit spans.

## Additional owner requirements

- [ ] T128 ARBORA-OPS-R024.1.1 Deliver every requirement in requirements.md.
- [ ] T129 ARBORA-OPS-R014.1 Deliver every requirement in requirements.md.
- [ ] T130 ARBORA-OPS-R022.3 Deliver every requirement in requirements.md.
- [ ] T131 ARBORA-OPS-R022.4 Deliver every requirement in requirements.md.
- [ ] T132 ARBORA-OPS-R053.7 Deliver every requirement in requirements.md.
- [ ] T133 ARBORA-OPS-R021.2.1.1 Deliver every requirement in requirements.md.
- [ ] T134 ARBORA-OPS-R050.2.1 Deliver every requirement in requirements.md.
- [ ] T135 ARBORA-OPS-R009.1 Deliver every requirement in requirements.md.
- [ ] T136 ARBORA-OPS-R009.2 Refusal causes are logged
- [ ] T137 ARBORA-OPS-R009.3 Declared replay owners
- [ ] T138 ARBORA-OPS-R009.4 Typed refusal details (fields, engine reason code, prompt arguments)
- [ ] T139 ARBORA-OPS-R053.8 Deliver every requirement in requirements.md.
- [ ] T140 ARBORA-OPS-R059 Resolve the producer contracts named in plan.md. Then implement the existing registry attachment and its served proof. Preserve each specified refusal.
- [ ] T141 ARBORA-OPS-R060 Resolve the producer contracts named in plan.md. Then implement the existing registry attachment and its served proof. Preserve each specified refusal.
- [ ] T142 ARBORA-OPS-R062 Resolve the producer contracts named in plan.md. Then implement the existing registry attachment and its served proof. Preserve each specified refusal.
- [ ] T143 ARBORA-OPS-R064 Resolve the producer contracts named in plan.md. Then implement the existing registry attachment and its served proof. Preserve each specified refusal.
- [ ] T144 ARBORA-OPS-R065 Resolve the producer contracts named in plan.md. Then implement the existing registry attachment and its served proof. Preserve each specified refusal.
- [ ] T145 ARBORA-OPS-R066 Resolve the producer contracts named in plan.md. Then implement the existing registry attachment and its served proof. Preserve each specified refusal.

- [ ] T146 ARBORA-OPS-R025.1 Validate operation membership and response digest in the generated Python invoker.

- [ ] T147 ARBORA-OPS-R025.2 Complete deterministic artifact generation and installed-wheel acceptance from the pinned engine contract.

- [ ] T148 ARBORA-OPS-R018.2.1 Distinct query declaration, resolver and caller dispatch contracts; live provider planner validation deferred.
- [ ] T149 ARBORA-OPS-R018.2.2 Remaining served query, SQL, search, indexes and analytics acceptance.


- [ ] T150 ARBORA-OPS-R843 Served engine operations replace the in-run meristem tool wrappers.
- [x] T151 ARBORA-OPS-R843.1 Delivered wrapper behaviour, bound to `tests/api/test_au_tool_parity.py`.
- [ ] T152 ARBORA-OPS-R843.2 Re-point the remaining modules to their composed rows or the harness.

## Web research operation (R067)

- [ ] T153 ARBORA-OPS-R067 Web research operation composes search, selection, fetch and ingest.
- [ ] T154 ARBORA-OPS-R067.1 Model. Add the params and the result with citations.
- [ ] T155 ARBORA-OPS-R067.2 Behavior. Compose the four steps and the deadline.
- [ ] T156 ARBORA-OPS-R067.3 Backend. Add the explicit choice, the fallback and the recorded backend.
- [ ] T157 ARBORA-OPS-R067.4 Wiring. Register the operation behind `invoke`.
- [ ] T158 ARBORA-OPS-R067.5 Refusal. Add the typed refusals.
- [ ] T159 ARBORA-OPS-R067.6 Evidence. Record the trace fields.
- [ ] T160 ARBORA-OPS-R067.7 Hot-path proof. Add the served three-call test.
- [ ] T161 ARBORA-OPS-R067.8 Web-source contract. Call `web_extract` with `{"request": ...}` and read `WebPageResult`/`WebFetchRefusal`.
- [ ] T162 ARBORA-OPS-R067.9 Bounded page fetch. Bound each `web_extract` call and fall back on `timeout`.
- [ ] T163 ARBORA-OPS-R067.10 Bind EG web conformance evidence to the existing fleet decision and live revision before dispatch.

## Served harness component and improvement library operations (R068)

- [ ] T164 ARBORA-OPS-R068 Serve the harness component and improvement library operations.
- [ ] T165 ARBORA-OPS-R068.1 Component reads. Add `harness.components.list` and `harness.components.get`.
- [ ] T166 ARBORA-OPS-R068.2 Lesson query. Add `harness.lessons.retrieve`.
- [ ] T167 ARBORA-OPS-R068.3 Evolution report. Add `harness.evolution.report`.
- [ ] T168 ARBORA-OPS-R068.4 Adoption. Add the service-only `harness.evolution.adopt`.
- [ ] T169 ARBORA-OPS-R068.5 Registration gate. Keep `harness.*` unregistered until the engine release publishes their senders.


- [ ] T170 ARBORA-OPS-R021.4.5.1.1 Compose the stateless native reader and qualify source-level authority/projection checks; retain the installed-EG acceptance blocker.
- [ ] T171 ARBORA-OPS-R021.4.5.1.2 Compose native run admission, lease-bound scaffold publication and matching graph reads. Add explicit trusted scaffold on/off configuration, default off. Qualify both modes through the actual AU callback and real native lifecycle.

## Served source operations (R848)

- [x] T172 ARBORA-OPS-R848.1 Serve the source reads: `source.tree`, `source.blob`, `source.history` and `source.blame` through the typed `SourceReader` port under the caller's engine scopes.
- [ ] T173 ARBORA-OPS-R848.2 Serve the source writes: `source.commit`, `source.land` and `source.revert` with destructive hints (pending the engine changeset substrate).
- [ ] T174 ARBORA-OPS-R848.3 Serve the `source.diff` read.
- [ ] T175 ARBORA-OPS-R849 Apply the server-side landing policy with fast and full gate phases.
- [x] T176 ARBORA-OPS-R850 Stream the virtual checkout endpoint, recursively and resumably.
- [ ] T177 ARBORA-OPS-R851 Serve the git smart-HTTP read bridge over EG trees.

- [ ] T178 ARBORA-OPS-R848.4 Graph-native symbol edits.
- [ ] T179 ARBORA-OPS-R852 Harness optimize, promote and rollback operations (rollup).
- [ ] T180 ARBORA-OPS-R852.1 Wiring.
- [ ] T181 ARBORA-OPS-R852.2 Hot-path proof.
- [ ] T182 ARBORA-OPS-R853 Lifecycle operations (rollup).
- [ ] T183 ARBORA-OPS-R853.1 Wiring.
- [ ] T184 ARBORA-OPS-R853.2 Hot-path proof.
- [ ] T185 ARBORA-OPS-R854 Context engine operations (rollup).
- [ ] T186 ARBORA-OPS-R854.1 Wiring.
- [ ] T187 ARBORA-OPS-R854.2 Hot-path proof.
- [ ] T188 ARBORA-OPS-R855 Catalog and governance operations (rollup).
- [x] T189 ARBORA-OPS-R855.1 Routing. Evidence: `tests/api/test_resolve.py::test_catalog_class_ask_routes_to_the_engine_planner_ahead_of_ranking`.
- [ ] T190 ARBORA-OPS-R855.2 Hot-path proof.
- [ ] T191 ARBORA-OPS-R856 Quality and lineage operations (rollup).
- [ ] T192 ARBORA-OPS-R856.1 Wiring.
- [ ] T193 ARBORA-OPS-R856.2 Hot-path proof.
- [ ] T194 ARBORA-OPS-R857 SPARQL 1.1 protocol endpoint (rollup).
- [ ] T195 ARBORA-OPS-R857.1 Wiring.
- [ ] T196 ARBORA-OPS-R857.2 Hot-path proof.
- [ ] T197 ARBORA-OPS-R858 Outside SQL client identities for the engine wire front end (rollup).
- [ ] T198 ARBORA-OPS-R858.1 Wiring.
- [ ] T199 ARBORA-OPS-R858.2 Hot-path proof.

- [ ] T200 ARBORA-OPS-R855.3 Trust flag, curation review and query workspace operations.
- [ ] T201 ARBORA-OPS-R855.4 Ask answers from the graph.
- [x] T202 ARBORA-OPS-R855.5 Find surfaces catalog assets. Evidence: `tests/api/test_resolve.py::test_find_for_catalog_assets_points_at_the_graph_query_ops_first`.
- [ ] T203 ARBORA-OPS-R855.6 Governed routing only.

## Served program optimization operations (R860)

- [ ] T204 ARBORA-OPS-R860 Served program optimization, promotion and rollback operations (rollup).
- [ ] T205 ARBORA-OPS-R860.1 Model.
- [ ] T206 ARBORA-OPS-R860.2 Behavior.
- [ ] T207 ARBORA-OPS-R860.3 Wiring.
- [ ] T208 ARBORA-OPS-R860.4 Refusal.
- [ ] T209 ARBORA-OPS-R860.5 Evidence.
- [ ] T210 ARBORA-OPS-R860.6 Hot-path proof.
- [ ] T211 ARBORA-OPS-R021.4.7 Serve owner-scoped durable public replay through shared HTTP/MCP admission; qualify native ordering and bounded cursors.

## Served dashboard operations (R861)

- [ ] T212 ARBORA-OPS-R861 Served dashboard operations (rollup). (rollup)
- [ ] T213 ARBORA-OPS-R861.1 Model.
- [ ] T214 ARBORA-OPS-R861.2 Save.
- [ ] T215 ARBORA-OPS-R861.3 Validation and size cap.
- [ ] T216 ARBORA-OPS-R861.4 Read.
- [ ] T217 ARBORA-OPS-R861.5 Delete.
- [ ] T218 ARBORA-OPS-R861.6 Share.
- [ ] T219 ARBORA-OPS-R861.7 Scope and audit.
- [ ] T220 ARBORA-OPS-R861.8 Wiring.
- [ ] T221 ARBORA-OPS-R861.9 Hot-path proof.

## Shared execution explorer

- [ ] T222 ARBORA-OPS-R021.4.8 — Add cursor-bound graph projection to existing run reads; qualify the read-only flat-plan slice before nested lineage.
- [ ] T223 ARBORA-OPS-R021.4.9 — Add owner-bound public node records and separate capability flags; keep unsupported interaction unavailable.
- [ ] T224 ARBORA-OPS-R021.4.8.1 — Qualify the negotiated prompt-free flat snapshot with pure, served-source and generated-contract checks; native-service acceptance remains separate.
- [ ] T225 ARBORA-OPS-R021.4.8.2 — Qualify producer-declared nested lineage and snapshot/update reconciliation through installed owners and both transports; retain all parent acceptance.

## Medulla companion rows (2026-10-28)

- [ ] T226 ARBORA-OPS-R069 Deliver the requirement in requirements.md.
- [ ] T227 ARBORA-OPS-R070 Deliver the requirement in requirements.md.
- [ ] T228 ARBORA-OPS-R071 Deliver the requirement in requirements.md.
- [ ] T229 ARBORA-OPS-R072 Deliver the requirement in requirements.md.
- [ ] T230 ARBORA-OPS-R073 Deliver the requirement in requirements.md.
- [ ] T231 ARBORA-OPS-R009.6 Operator hook model and configuration. Deliver as written in requirements.md.
- [ ] T232 ARBORA-OPS-R009.7 Input hook runs before the input is admitted. Deliver as written in requirements.md.
- [ ] T233 ARBORA-OPS-R009.8 Output hook runs before a response leaves. Deliver as written in requirements.md.
- [ ] T234 ARBORA-OPS-R009.9 Hooks fail closed and record their decisions. Deliver as written in requirements.md.
- [ ] T235 ARBORA-OPS-R024.6 Derivation status read. Deliver as written in requirements.md.
- [ ] T236 ARBORA-OPS-R897 Participant memory, scopes, profiles and document sets are served operations (rollup). Deliver as written in requirements.md.
- [ ] T237 ARBORA-OPS-R897.1 Model and the shared engine send helper. Deliver as written in requirements.md.
- [ ] T238 ARBORA-OPS-R897.2 Participant and membership operations. Deliver as written in requirements.md.
- [ ] T239 ARBORA-OPS-R897.3 Scope and observation operations. Deliver as written in requirements.md.
- [ ] T240 ARBORA-OPS-R897.4 Turn append operation. Deliver as written in requirements.md.
- [ ] T241 ARBORA-OPS-R897.5 Ask and card operations. Deliver as written in requirements.md.
- [ ] T242 ARBORA-OPS-R897.6 Assistant profile and document set operations. Deliver as written in requirements.md.
- [ ] T243 ARBORA-OPS-R897.7 Registration and error mapping. Deliver as written in requirements.md.
- [ ] T244 ARBORA-OPS-R897.8 Hot-path proof. Deliver as written in requirements.md.



## Tasks for the remaining rows

Each line below names one active row that no earlier task covers. The description is the row title.

- [ ] T245 ARBORA-OPS-R001 Native MCP/REST facade with A2A unary operations then streaming.
- [ ] T246 ARBORA-OPS-R002 Scoped A2A task routing and fleet tool exposure.
- [ ] T247 ARBORA-OPS-R003 Remove multiplexer skill/prompt harvest path.
- [ ] T248 ARBORA-OPS-R004 FastMCP 4 bridge for the MCP server.
- [ ] T249 ARBORA-OPS-R005 Stable auth error codes from the engine contract.
- [ ] T250 ARBORA-OPS-R006 Consume the engine's packaged wire-callable contract.
- [ ] T251 ARBORA-OPS-R007.1 REST request authority.
- [ ] T252 ARBORA-OPS-R010 Closed error enum and one response envelope.
- [ ] T253 ARBORA-OPS-R011 Six resident MCP verbs with typed discovery.
- [ ] T254 ARBORA-OPS-R012 Bounded resolver for natural-language operation lookup.
- [ ] T255 ARBORA-OPS-R013 Generated HTTP API for every registry operation.
- [ ] T256 ARBORA-OPS-R023 Agent, browser, RLM, and fleet call operations.
- [ ] T257 ARBORA-OPS-R844 Served memory read and write over EG KV.
- [ ] T258 ARBORA-OPS-R845 Served memory list and delete.
- [ ] T259 ARBORA-OPS-R846 Explicit shared memory scope.
- [ ] T260 ARBORA-OPS-R847 KV methods in the registry.
- [ ] T261 ARBORA-OPS-R027 Authority-parity test across principals and surfaces.
- [ ] T262 ARBORA-OPS-R028 Exported domain scope classes.
- [ ] T263 ARBORA-OPS-R029 Single resident-tool server cutover.
- [ ] T264 ARBORA-OPS-R030 Accurate MCP server and API documentation.
- [ ] T265 ARBORA-OPS-R033 Dynamic multiplexer discovery, load, and call.
- [ ] T266 ARBORA-OPS-R034 Eunomia narrowing-only policy enforcement.
- [ ] T267 ARBORA-OPS-R036 Pre-dispatch audit reservation for governed effects.
- [ ] T268 ARBORA-OPS-R037 Native first-party A2A facade.
- [ ] T269 ARBORA-OPS-R038 Generated OpenAPI served through Swagger UI.
- [ ] T270 ARBORA-OPS-R039.1 Dashboard route census.
- [ ] T271 ARBORA-OPS-R039.2 Dashboard write capability.
- [ ] T272 ARBORA-OPS-R039.3 Add one test in tests/gateway/test_dashboard_api.py asserting GET /health returns 200 without a capability and sets Cache-Control no-store.
- [ ] T273 ARBORA-OPS-R039.4 Dashboard input refusals.
- [ ] T274 ARBORA-OPS-R040.2 Create-artifact request model.
- [ ] T275 ARBORA-OPS-R040.3 Artifact store and refresh ports.
- [ ] T276 ARBORA-OPS-R040.4 Create artifact route.
- [ ] T277 ARBORA-OPS-R040.5 Get artifact route.
- [ ] T278 ARBORA-OPS-R040.6 Refresh artifact route.
- [ ] T279 ARBORA-OPS-R040.7 Mount the artifact routes.
- [ ] T280 ARBORA-OPS-R040.8 Wire the source resolver.
- [ ] T281 ARBORA-OPS-R041.1 Add one test in tests/gateway/test_registry.py asserting genius_agent is absent from the widget registry and no fixed-value widget is registered.
- [ ] T282 ARBORA-OPS-R042.1 Screening model.
- [ ] T283 ARBORA-OPS-R042.2 Default screen rules and output redactor.
- [ ] T284 ARBORA-OPS-R042.3 Screening wiring.
- [ ] T285 ARBORA-OPS-R042.4 Screening refusals.
- [ ] T286 ARBORA-OPS-R042.5 Screening evidence.
- [ ] T287 ARBORA-OPS-R042.6 Screening hot-path proof.
- [ ] T288 ARBORA-OPS-R009.5 Forward the approval reference to connector calls.
- [ ] T289 ARBORA-OPS-R700 Readiness snapshot joins independent probes.
- [ ] T290 ARBORA-OPS-R054.1 Technique activation model and behavior.
- [ ] T291 ARBORA-OPS-R054.2 Technique activation wiring and proof.
- [ ] T292 ARBORA-OPS-R055.1 Process remediation proposal.
- [ ] T293 ARBORA-OPS-R055.2 Process termination bound to approval.
- [ ] T294 ARBORA-OPS-R059.1 Evolution metrics models.
- [ ] T295 ARBORA-OPS-R059.2 Evolution metrics behavior and wiring.
- [ ] T296 ARBORA-OPS-R059.3 Evolution metrics refusals.
- [ ] T297 ARBORA-OPS-R059.4 Evolution metrics hot-path proof.
- [ ] T298 ARBORA-OPS-R060.1 Session and harness models.
- [ ] T299 ARBORA-OPS-R060.2 Harness manifest and discovery status operations.
- [ ] T300 ARBORA-OPS-R060.3 Session browse, search and cost operations.
- [ ] T301 ARBORA-OPS-R060.4 Session operation refusals.
- [ ] T302 ARBORA-OPS-R060.5 Session projection hot-path proof.
- [ ] T303 ARBORA-OPS-R061.1 Usage index models.
- [ ] T304 ARBORA-OPS-R061.2 Usage index forwarding.
- [ ] T305 ARBORA-OPS-R061.3 Usage index wiring.
- [ ] T306 ARBORA-OPS-R061.4 Usage index refusals and latency profile.
- [ ] T307 ARBORA-OPS-R061.5 Usage index hot-path proof.
- [ ] T308 ARBORA-OPS-R062.1 Graph change models.
- [ ] T309 ARBORA-OPS-R062.2 Subscribe and stream.
- [ ] T310 ARBORA-OPS-R062.3 Snapshot recovery.
- [ ] T311 ARBORA-OPS-R062.4 Revocation, refusals and visibility limit.
- [ ] T312 ARBORA-OPS-R062.5 Graph change hot-path proof.
- [ ] T313 ARBORA-OPS-R063.1 Retrieval evaluation and context models.
- [ ] T314 ARBORA-OPS-R063.2 Retrieval evaluation forwarding.
- [ ] T315 ARBORA-OPS-R063.3 Authorized context search forwarding.
- [ ] T316 ARBORA-OPS-R063.4 Retrieval evaluation refusals.
- [ ] T317 ARBORA-OPS-R063.5 Retrieval evaluation hot-path proof.
- [ ] T318 ARBORA-OPS-R064.1 Viewport models.
- [ ] T319 ARBORA-OPS-R064.2 Viewport behavior and wiring.
- [ ] T320 ARBORA-OPS-R064.3 Viewport refusals.
- [ ] T321 ARBORA-OPS-R064.4 Viewport hot-path proof.
- [ ] T322 ARBORA-OPS-R065.1 Meeting models.
- [ ] T323 ARBORA-OPS-R065.2 Transcript stream.
- [ ] T324 ARBORA-OPS-R065.3 Providers and summary operations.
- [ ] T325 ARBORA-OPS-R065.4 Meeting egress refusals.
- [ ] T326 ARBORA-OPS-R065.5 Meeting hot-path proof.
- [ ] T327 ARBORA-OPS-R066.1 Bookmark models.
- [ ] T328 ARBORA-OPS-R066.2 Bookmark search and relationships.
- [ ] T329 ARBORA-OPS-R066.3 Bookmark export.
- [ ] T330 ARBORA-OPS-R066.4 Bookmark refusals.
- [ ] T331 ARBORA-OPS-R066.5 Bookmark hot-path proof.
- [ ] T332 ARBORA-OPS-R855.3.1 arbora consumes catalog trust-flag events and notifies stored-query owners.
- [ ] T333 ARBORA-OPS-R862 Surface a recommendation only after a passing safety-bound verdict.
- [ ] T334 ARBORA-OPS-R863 Issue a recommendation approval receipt bound to a record digest.
- [ ] T335 ARBORA-OPS-R864 Execute only the bound retrieval provider.
- [ ] T336 ARBORA-OPS-R865 Approve queued write-back proposals.
- [ ] T337 ARBORA-OPS-R866 Serve tool search from the Medulla tool ranking.
- [ ] T338 ARBORA-OPS-R867 Block an action until approval when medulla requires confirmation.
- [ ] T339 ARBORA-OPS-R868 Carry the table-of-contents identity in space directory listings.
- [ ] T340 ARBORA-OPS-R869 Gate generated-code materialization on a committed admission record.
- [ ] T341 ARBORA-OPS-R870 Serve authenticated changeset review and call medulla Decide.
- [ ] T342 ARBORA-OPS-R871 Accept a playbook write step only with a committed Decide record.
- [ ] T343 ARBORA-OPS-R872 Check the certification attestation before workflow invocation.
- [ ] T344 ARBORA-OPS-R873 Start the workflow engine lazily on first invocation.
- [ ] T345 ARBORA-OPS-R874 Return registry identities from the host semantic workflow match.
- [ ] T346 ARBORA-OPS-R875 Run the upgrade admission before the invoke certification step.
- [ ] T347 ARBORA-OPS-R876 Report the loop continuation from the latest committed record.
- [ ] T348 ARBORA-OPS-R877 Write team records and apply the coordination decision.
- [ ] T349 ARBORA-OPS-R878 Read operation `models.list` lists the model profiles a caller may prefer.
- [ ] T350 ARBORA-OPS-R879 Serve hook install and uninstall as registry operations.
- [ ] T351 ARBORA-OPS-R880 Serve the cross-source report as a registered operation.
- [ ] T352 ARBORA-OPS-R881 Artifact operations in the registry for eg authentication mode.
- [ ] T353 ARBORA-OPS-R882 Agent bus operations over medulla topic, roster and work-item methods.
- [ ] T354 ARBORA-OPS-R883 Retrieval operations carry the verified agent reference.
- [ ] T355 ARBORA-OPS-R884 Meeting summary orchestration with provider admission.
- [ ] T356 ARBORA-OPS-R885 Review a policy revision and return a review approval.
- [ ] T357 ARBORA-OPS-R886 Admitted ingest maintenance operation.
- [ ] T358 ARBORA-OPS-R887 Served operation entity.approve_link.
- [ ] T359 ARBORA-OPS-R888 Admit data-product access requests against the steward role.
- [ ] T360 ARBORA-OPS-R889 Serve an OpenLineage-compatible run-event route that calls `LineageIngest`.
- [ ] T361 ARBORA-OPS-R890 Bind the engine decision port at host start.
- [ ] T362 ARBORA-OPS-R891 Step usage events reach the usage ledger.
- [ ] T363 ARBORA-OPS-R893 arbora serves the credential access authority port to the SDK.
- [ ] T364 ARBORA-OPS-R894 Register the changeset submitter and gate runner at host start.
- [ ] T365 ARBORA-OPS-R895 Summarize a finalized meeting under an admitted provider.
- [ ] T366 [US3] ARBORA-OPS-R896.1 Artifact-graph reads resolve to registered engine competency queries.
- [ ] T367 [US3] ARBORA-OPS-R896.2 Project mode is read and changed through served operations.
- [ ] T368 [US3] ARBORA-OPS-R896.3 An orchestrated implementation run starts through a served act operation.
- [ ] T369 [US3] ARBORA-OPS-R896.4 An implementation run is observed through a served ask operation.
- [ ] T370 [US3] ARBORA-OPS-R896.5 Proposals are listed and reviewed through served operations and land only as pull requests.
- [ ] T371 [US3] ARBORA-OPS-R896.6 Hot-path proof: the artifact graph drives a run over MCP.

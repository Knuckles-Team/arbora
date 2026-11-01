# Delivery tasks and evidence

Status vocabulary is defined in [spec.md](spec.md). Check a task only after its listed proof exists; source presence alone is not a check mark.

- [x] Extract and publish the Arbora serving, gateway, fleet, A2A and deployment package boundary. Evidence: package modules under `arbora/`, public `arbora` console script in `pyproject.toml`. **State: Implemented; release/served acceptance for this whole spec still pending.**
- [x] `ARBORA-HOST-R001.1`: move the meristem MCP, API and WebUI server code that arbora uses into `arbora/mcp_server/backing/`, `arbora/api/contracts/` and `arbora/webui_host/`; require `meristem>=3.0.0,<4.0.0`. Evidence: `tests/test_package_layout.py::test_arbora_imports_no_au_serving_packages`.
- [x] `ARBORA-HOST-R001.2`: `tests/mcp_server/test_harness_host.py` proves fail-closed before registration, bound tools/fleet/keepalive after, and cleared bindings after shutdown.
- [x] `ARBORA-HOST-R001.3`: `tests/gateway/test_gateway_client.py` passes against `arbora.gateway.client_sdk`.
- [x] `ARBORA-HOST-R001.4`: `tests/deployment/test_arbora_deploy_artifacts.py` asserts the `agent_connector_sdk.skill_providers` entry-point group in `pyproject.toml`.
- [x] `ARBORA-HOST-R001.5`: The moved test files pass on FastMCP 4.x with `medulla[server]==2.27.1`.
- [x] `ARBORA-HOST-R001.6`: `tests/mcp_server/test_operation_registry.py` proves the packaged document is current, names exactly the served operations and loads through the harness reader.
- [x] `ARBORA-HOST-R001.8`: `tests/mcp_server/test_engine_wiring_gate.py` passes against the installed medulla.
- [x] `ARBORA-HOST-R001.9`: `tests/integration/profiles/test_profile_tiny_zero_dep.py::test_tiny_profile_serves_kg_over_gateway_with_zero_containers`.
- [x] `ARBORA-HOST-R001.10`: port provider discovery into `arbora/core/providers.py` over the `agent_connector_sdk.*_providers` groups and repoint every Arbora caller. Evidence: `tests/core/test_providers.py`.
- [ ] `ARBORA-HOST-R007.2`: rollup; deliver `ARBORA-HOST-R007.2.1` to `ARBORA-HOST-R007.2.68` (one rewiring per violating module, 68 modules at split time) and then the fence test `ARBORA-HOST-R007.2.69`.
- [ ] Inventory all dependency-internal imports and duplicate host scripts; attach a machine-readable owner/port map and the initial forbidden-import count.
- [ ] Add missing public engine, SDK and AU ports in their owner packages and release/install them.
- [ ] Replace private imports throughout Arbora with public ports and enforce a zero-import architecture test.
- [ ] Route MCP, REST, A2A, messaging and UI requests through one typed application service and one authorization decision.
- [ ] Reconcile duplicate gateway/deployment/MCP/fleet/A2A behavior and remove old host entrypoints after parity tests.
- [ ] Prove one event-loop owner and bounded lifecycle under simultaneous co-services, reload and cancellation.
- [ ] Run clean-checkout contract/full/quality gates and a wheel install smoke; link exact commit, CI run and wheel hash.
- [ ] Run served authorization and receipt parity against an isolated release environment. Attach redacted evidence and mark Accepted only after this passes.
- [x] ARBORA-HOST-R019: Mount `/api/dashboard/*` and `/ws/dashboard` from `arbora.gateway.dashboard_api`. Evidence: `tests/gateway/test_dashboard_api.py`. **State: Building.** Remaining: served check of `/api/dashboard/full` (200) and `/ws/dashboard` (snapshot frame) after rollout.
- [x] ARBORA-HOST-R019: Mount `/api/dashboard/*` and `/ws/dashboard` from `arbora.gateway.dashboard_api`. Evidence: `tests/gateway/test_dashboard_api.py`. **State: Building.** Remaining: served check of `/api/dashboard/full` (200) and `/ws/dashboard` (snapshot frame) after rollout.
- [x] ARBORA-HOST-R014: `pyproject.toml` pins `meristem`/`agent-connector-sdk`/`medulla`/`agent-webui` as plain PEP 508 version ranges; `[tool.uv.sources]` stays a local-dev-only editable override that the `setuptools.build_meta` backend never consults. Evidence: `tests/test_dependency_manifest.py` (6 cases, checked against setuptools' own pyproject resolver). **State: Implemented for the manifest; the connector SDK/engine/AU packages are not yet published to an index, so this proves the manifest shape, not index availability.**
- [x] ARBORA-HOST-R020: Install the decide consumers at serving start. Evidence: `tests/mcp_server/test_decide_wiring.py`. **State: Building.** Remaining: after rollout, confirm a live plan reports no `assembler_not_installed` gap.
- [x] ARBORA-HOST-R021: Bind Arbora commit and publish providers to the assembler; install the topology asker over the published reference templates. Evidence: `tests/mcp_server/test_decide_wiring.py`. **State: Building.** Remaining: after rollout, confirm a live plan's assembly commits and publishes against the served engine.
- [x] ARBORA-HOST-R022: Bind capability-search, guardrail and workflow lookups to the task planner. Evidence: `tests/mcp_server/test_decide_wiring.py`. **State: Building.** Remaining: the capability-search provider's exact candidate shape from `AgentClient.agents()` is unverified against a live engine. After rollout, confirm a live plan's reuse hits and guardrail rows are non-degenerate.
- [x] ARBORA-HOST-R008 (slice 1 of N): move `security/security_policy_middleware.py` from the agent runtime into `arbora/security/`. Evidence: `arbora/security/security_policy_middleware.py`, `tests/security/test_security_policy_middleware.py` (10 cases). The AU copy had zero in-repo importers and zero tests, so nothing there needed rewiring. Its deletion is tracked in a separate `meristem` PR. **State: Building.** Remaining (per `agent_utilities/architecture/component-registry-boundary-seeds.yml` MERISTEM-BOUNDARY-R014): `security/request_identity.py`, `security/error_surface.py`, `security/browser_auth.py`, `security/auth.py`, `security/conformance/**`, the `observability` health/metrics modules, and the `core`/`ecosystem`/`knowledge_graph` modules in that seed group — one module per slice.
- [x] ARBORA-HOST-R008 (slice 2 of N): move `ecosystem/agent_manager_dashboard.py` from the agent runtime into `arbora/ecosystem/`. Evidence: `arbora/ecosystem/agent_manager_dashboard.py`, `tests/ecosystem/test_agent_manager_dashboard.py` (5 cases, 4 ported + 1 new for the duck-typed engine param). The AU copy's only in-repo "importer" was its own package's `__init__.py` re-export (no real consumer); a companion `meristem` PR deletes the AU copy and that re-export together. **State: Building** (PR #68, open).
- [x] ARBORA-HOST-R008 (slice 3 of N): move `observability/gateway_health.py` into `arbora/observability/`. Evidence: `arbora/observability/gateway_health.py`, `tests/observability/test_gateway_health_evidence_live_path.py` (5 of the AU file's 5 tests ported; the 5th AU test, which proved `agent_utilities.observability.gateway_metrics`' middleware calls this module, stays in the agent runtime since that module has not moved). The AU copy's only in-repo importer is a function-local, try/except-wrapped, explicitly best-effort call inside `gateway_metrics.py` — deleting the AU copy exercises that module's own documented fallback path rather than breaking it; a companion `meristem` PR adds a regression test proving exactly that. **State: Building** (PR #71, open). Remaining (per `agent_utilities/architecture/component-registry-boundary-seeds.yml` MERISTEM-BOUNDARY-R014): `security/request_identity.py`, `security/error_surface.py`, `security/browser_auth.py`, `security/auth.py`, `security/conformance/**`, `observability/gateway_metrics.py`, `observability/runtime_health.py`, `observability/health_ingest.py`, and the remaining `core`/`knowledge_graph` modules in that seed group — one module per slice, ascending by in-repo importer count, re-verified by bare-name grep each time (a dotted-path grep misses relative imports).
- [x] ARBORA-HOST-R008 (slice 4 of N): move `observability/health.py` into `arbora/observability/`. Evidence: `arbora/observability/health.py` (18 tests ported). The AU copy's only real importer is `gateway_health.py`'s two function-local, try/except-wrapped, best-effort lazy imports. Its one hard dependent, `test_gateway_health_evidence_live_path.py`, is deleted in the companion AU PR and replaced with a graceful-degradation proof. **State: Building** (PR #76, open).
- [x] ARBORA-HOST-R008 (slice 5 of N): move `security/conformance/**` (surface_manifest + surface_inventory, the GOC-62 auth conformance suite) into `arbora/security/conformance/`. Evidence: `arbora/security/conformance/`, 9 tests ported (8 pass; 1 fails identically on AU main today — pre-existing, BUG-PE-006, not introduced here). Zero real production importers in AU (only its own tests). Not a bare copy: `surface_inventory.QUERY_TOOLS_MODULE` and the proof-citation paths in `test_known_fail_opens.py` assumed same-repo-relative paths into `agent_utilities/mcp/tools/query_tools.py` and AU's own `tests/` tree; both now resolve through the installed `agent_utilities` distribution (`importlib.util.find_spec`), with the AU-`tests/`-relative checks skipping (not failing) when AU isn't present as an editable sibling checkout — extending this file's own existing graceful-skip convention for the medulla/agent-webui sibling checks. **State: Building** (PR #78, open).

**Current evidence gap:** No exact commit and served receipt set is recorded here for the strict public-port cutover or the complete parity matrix. Preserve `Implemented in part / acceptance pending` until those are attached.

## Decomposition children (tracked)

- [x] **ARBORA-HOST-R005:** arbora.mcp_server.runtime serves retired MCP actions
- [ ] **ARBORA-HOST-R005.1:** Remaining scope of ARBORA-HOST-R005 (slice .1): arbora.mcp_server.runtime serves retired MCP actions
- [ ] **ARBORA-HOST-R005.2:** Remaining scope of ARBORA-HOST-R005 (slice .2): arbora.mcp_server.runtime serves retired MCP actions
- [x] **ARBORA-HOST-R007:** Agent runtime imported only through its public API
- [ ] **ARBORA-HOST-R007.1:** Remaining scope of ARBORA-HOST-R007 (slice .1): Agent runtime imported only through its public API
- [ ] **ARBORA-HOST-R007.2:** Remaining scope of ARBORA-HOST-R007 (slice .2): Agent runtime imported only through its public API
- [x] **ARBORA-HOST-R005.1:** Landed typed approved action-to-operation mapping model. Evidence: `tests/mcp_server/test_legacy_action_mapping.py`.
- [ ] **ARBORA-HOST-R005.2:** Parity test proves no served action is dropped (rollup of .2.1-.2.8)
- [ ] **ARBORA-HOST-R005.2.1:** Approved mapping for the analyze action family
- [ ] **ARBORA-HOST-R005.2.1.1:** Add arbora operation for code navigation actions
- [ ] **ARBORA-HOST-R005.2.1.2:** Add arbora operation for impact actions
- [ ] **ARBORA-HOST-R005.2.1.3:** Add arbora operation for `similar_code` and the remaining `graph_analyze` actions
- [ ] **ARBORA-HOST-R005.2.1.4:** Add arbora operation for `process_writeback`
- [ ] **ARBORA-HOST-R005.2.1.5:** Approve the analyze mappings
- [ ] **ARBORA-HOST-R005.2.2:** Approved mapping for the configure action family
- [ ] **ARBORA-HOST-R005.2.3:** Approved mapping for the ingest action family
- [ ] **ARBORA-HOST-R005.2.3.1:** Map `sync` to `ingest.sources.sync`
- [ ] **ARBORA-HOST-R005.2.3.2:** Add arbora operation for ingest submission actions
- [ ] **ARBORA-HOST-R005.2.3.3:** Add arbora operation for `corpus`
- [ ] **ARBORA-HOST-R005.2.3.4:** Add arbora operation for job lifecycle actions
- [ ] **ARBORA-HOST-R005.2.3.5:** Add arbora operation for `rebuild_indexes` and `observe`
- [ ] **ARBORA-HOST-R005.2.3.6:** Add arbora operation for `materialize` and `materialize_source`
- [ ] **ARBORA-HOST-R005.2.3.7:** Add arbora operation for `reflect` and `agent_toolkit`
- [ ] **ARBORA-HOST-R005.2.3.8:** Approve the ingest mappings
- [ ] **ARBORA-HOST-R005.2.4:** Approved mapping for the query-memory action family
- [ ] **ARBORA-HOST-R005.2.5:** Approved mapping for the orchestrate action family
- [ ] **ARBORA-HOST-R005.2.6:** Approved mapping for the mine-learn action family
- [ ] **ARBORA-HOST-R005.2.7:** Approved mapping for the governance-ops action family
- [ ] **ARBORA-HOST-R005.2.8:** Parity test over the full legacy action surface
- [ ] **ARBORA-HOST-R026:** Durable A2A task persistence in the arbora agent host.
- [ ] **ARBORA-HOST-R026.1:** Define the typed A2A task-store port.
- [ ] **ARBORA-HOST-R026.2:** Implement the engine-backed A2A task store.
- [ ] **ARBORA-HOST-R026.3:** Wire the hosted A2A app to the task store, fail-closed when absent.
- [ ] **ARBORA-HOST-R026.4:** Add the doctor check for A2A task persistence.
- [ ] **ARBORA-HOST-R026.5:** Point the `g34-a2a` certification gate at arbora tests.
- [ ] **ARBORA-HOST-R027:** Loopback OIDC authority for skill certification.
- [ ] **ARBORA-HOST-R027.1:** Add the loopback OIDC authority owner module for skill certification.
- [ ] **ARBORA-HOST-R027.2:** Add the loopback OIDC authority test fixture.
- [ ] **ARBORA-HOST-R027.3:** Add the loopback OIDC authority script.
- [ ] **ARBORA-HOST-R028:** Bump the pinned meristem source for the new `agent_utilities.api.*` export modules.

- [ ] Define and prove preserve configuration ownership (`ARBORA-HOST-R010.1`) through the owning production entrypoint.

- [ ] Define and prove declare every consumed environment name (`ARBORA-HOST-R010.2`) through the owning production entrypoint.

- [ ] Define and prove validate before any service starts (`ARBORA-HOST-R010.3`) through the owning production entrypoint.

- [ ] Define and prove keep conditional configuration precise (`ARBORA-HOST-R010.4`) through the owning production entrypoint.

- [ ] Define and prove bind environment guidance to declarations (`ARBORA-HOST-R010.5`) through the owning production entrypoint.

- [ ] Define and prove prove startup validation through the launcher (`ARBORA-HOST-R010.6`) through the owning production entrypoint.

- [ ] Define and prove preserve every boundary module (`ARBORA-HOST-R008.1`) through the owning production entrypoint.

- [ ] Define and prove detect installed harnesses without effects (`ARBORA-HOST-R008.2`) through the owning production entrypoint.

- [ ] Define and prove bind exact harness configuration changes (`ARBORA-HOST-R008.3`) through the owning production entrypoint.

- [ ] Define and prove apply harness edits without overwriting unrelated content (`ARBORA-HOST-R008.4`) through the owning production entrypoint.

- [ ] Define and prove prove detected-harness installation (`ARBORA-HOST-R008.5`) through the owning production entrypoint.

- [ ] Define and prove preserve hosted protocols and task authority (`ARBORA-HOST-R009.1`) through the owning production entrypoint.

- [ ] Define and prove select terminal web or combined start (`ARBORA-HOST-R010.7`) through the owning production entrypoint.

- [ ] Define and prove run terminal-mode http in a supervised background thread (`ARBORA-HOST-R009.2`) through the owning production entrypoint.

- [ ] Define and prove project scoped workspace logs (`ARBORA-HOST-R009.3`) through the owning production entrypoint.

- [ ] Define and prove bound log delivery and shutdown (`ARBORA-HOST-R009.4`) through the owning production entrypoint.

- [ ] Define and prove publish the terminal log-consumer handoff (`ARBORA-HOST-R009.5`) through the owning production entrypoint.

- [ ] Define and prove prove start and logs through real processes (`ARBORA-HOST-R009.6`) through the owning production entrypoint.

- [ ] Define and prove preserve the sole multiplexer owner (`ARBORA-HOST-R006.1`) through the owning production entrypoint.

- [ ] Define and prove use one standard transport scaffold (`ARBORA-HOST-R006.2`) through the owning production entrypoint.

- [ ] Define and prove preserve the entire intent surface (`ARBORA-HOST-R023.1`) through the owning production entrypoint.

- [ ] Define and prove prove transport parity on the served surface (`ARBORA-HOST-R023.2`) through the owning production entrypoint.

## Owner-bound capabilities (ARBORA-HOST-R029–R031)

- [ ] **R029:** Obtain the SDK native-desktop port/provider contract; bind verified grant, tenant, and no-capture behavior through composed dispatch.
- [ ] **R030:** Obtain the SDK observation producer and EG state/context contract; bind run/source state and stale/cross-tenant refusal through composed dispatch.
- [ ] **R031:** Obtain the EG, AU, and WebUI owner ports; bind discovery/admission at the server composition boundary.

- [ ] Bind the effective authenticated web_logs receipt field and public run_host_terminal/HostLogReader.read interfaces for ARBORA-HOST-R009.5, R009.6 and R010.7. Implement producer prerequisites before claiming readiness.
- [ ] Run the three named authenticated terminal-log cases in test-spec.md with TUIG-RUNTIME-R002.2 through R002.6, including enabled reads and disabled/invalid zero-read observations. Preserve all existing lifecycle and scope acceptance.

## Additional owner requirements

- [ ] `ARBORA-HOST-R023.3`: Deliver every requirement in requirements.md.
- [ ] `ARBORA-HOST-R008.6`: Deliver every requirement in requirements.md.
- [ ] `ARBORA-HOST-R026.6`: Deliver every requirement in requirements.md.
- [ ] `ARBORA-HOST-R022.1`: Deliver every requirement in requirements.md.
- [ ] `ARBORA-HOST-R010.8`: Deliver every requirement in requirements.md.
- [ ] `ARBORA-HOST-R032`: Deliver every requirement in requirements.md.
- [x] `ARBORA-HOST-R033`: Route every unique-id record write through `create_record` (EG `CreateNodeIfAbsent`).
- [x] `ARBORA-HOST-R033.1`: Build every labelled body in `record_body`; one `ActionDecision` writer.
- [x] `ARBORA-HOST-R033.2`: Run recorded, idempotent data migrations at serving start; label type-only `ActionDecision` rows.
- [x] `ARBORA-HOST-R033.3`: Gate persisted shapes on a declared migration.

- [x] `ARBORA-HOST-R010.9`: Refuse unsupported explicit interfaces before workspace, graph or listener effects.

- [ ] `ARBORA-HOST-R009.7`: Inject and verify bounded host log authority.

- [ ] `ARBORA-HOST-R009.8`: Record authenticated requests through the production sink.
- [ ] `ARBORA-HOST-R009.8.2.2.1`: Qualify all four real native terminal cases against the pinned released wheel.
- [ ] `ARBORA-HOST-R009.8.2.2.2`: Qualify retained metadata and gap clearing in installed Overstory.
- [x] `ARBORA-HOST-R004`: keep one gateway package. Evidence: `tests/architecture/test_single_gateway_implementation.py::test_single_gateway_and_retired_script_and_extra_absent`.
- [ ] `ARBORA-HOST-R007.2.69`: add the declared-module fence test `tests/test_package_layout.py::test_arbora_imports_only_meristem_public_modules` and its planted-violation proof.
- [x] `ARBORA-HOST-R012`: publish the Arbora and connector SDK boundary seeds. Evidence: `tests/test_component_registry_boundary_seeds.py`.

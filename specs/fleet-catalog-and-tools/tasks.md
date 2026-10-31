# Implementation tasks

Check a task only after its linked code and tests land. A checked source task is not acceptance; the final release task records served evidence.

- T01 — Inventory existing fleet/MCP code and generated engine/SDK contracts; record current behavior and one owner per interface. Cover ARBORA-FLEET-R001, ARBORA-FLEET-R002, ARBORA-FLEET-R004, ARBORA-FLEET-R006, ARBORA-FLEET-R007.
- T02 — Define frozen `OpSpec`, stable IDs, exact scope/effect metadata, canonical digest, reviewed engine exclusions, and generator checks. Cover ARBORA-FLEET-R011, ARBORA-FLEET-R012, ARBORA-FLEET-R016.
- T03 — Implement typed `invoke` with caller validation, narrowing policy, service executor, effect plans, audit, and stable errors. Move child dispatch behind one fleet gateway. Cover ARBORA-FLEET-R015, ARBORA-FLEET-R021, ARBORA-FLEET-R003.
- T04 — Project the six intent verbs and four resident fleet tools, generated schemas/resources, cross-surface discovery, and bounded resolver. Remove obsolete harvest and granular registration in the same cutover. Cover ARBORA-FLEET-R013, ARBORA-FLEET-R014, ARBORA-FLEET-R005, ARBORA-FLEET-R006, ARBORA-FLEET-R007, ARBORA-FLEET-R020. ARBORA-FLEET-R006 is now a rollup over ARBORA-FLEET-R006.1-.5; ARBORA-FLEET-R007 is now a rollup over ARBORA-FLEET-R007.1-.3.
- **ARBORA-FLEET-R014.1:** `ask` does not route a plain question to an operation it cannot run. Evidence: `tests/api/test_resolve.py`.
- ARBORA-FLEET-R005.1 (child, producer): `arbora/a2a/work_items.py` (`WorkItemView`, `work_item_view`, `parse_work_item_view`, `list_work_item_views`) + `tests/a2a/test_work_items.py`.
- ARBORA-FLEET-R006.1 (child, producer): `tests/fleet/test_harvest_inventory.py` pins the current harvest call-site baseline in `arbora/fleet/multiplexer.py`.
- ARBORA-FLEET-R006.2 (child, consumer of .1): cut `_probe_skills`/`_probe_prompts` over to the resident `build_local_skill_catalog` path for an already-admitted child, keeping fallback for a non-admitted one.
- ARBORA-FLEET-R006.3 (rollup over .3.1-.3.4, consumer of .2): remove the harvest helpers in `arbora/fleet/multiplexer.py` once no caller remains.
- ARBORA-FLEET-R006.3.1 (child): route `_probe_protocol_families`'s two harvest calls through `_resolve_via_local_catalog_or_harvest`.
- ARBORA-FLEET-R006.3.2 (child, consumer of .3.1): delete the non-admitted fallback and `_harvest_resource_bodies` once every mounted child is catalog-admitted.
- ARBORA-FLEET-R006.3.3 (child, consumer of .3.2): delete `_harvest_deadline`, `_HARVEST_DEADLINE_SHARE`, `_harvest_error_reason`.
- ARBORA-FLEET-R006.3.4 (child, consumer of .3.3): delete `_SKILL_HARVEST_SPEC`, `_PROMPT_HARVEST_SPEC`.
- ARBORA-FLEET-R006.4 (child, consumer of .3): rename `_harvest_probe_results` to `_settled_probe_results` in `arbora/fleet/multiplexer.py`.
- ARBORA-FLEET-R006.5 (child, closing audit, consumer of .1-.4): `tests/fleet/test_no_harvest_remains.py` asserts the R006.1 baseline is now zero.
- ARBORA-FLEET-R007.1 (child, producer, cross-repo: agent-connector-sdk): `agent_connector_sdk/mcp/server.py`'s `create_mcp_server()` registers a native `.prompt()`/`.resource()`/`.resource_template()`.
- ARBORA-FLEET-R007.2 (child, consumer of .1, cross-repo: meristem): `agent_utilities/mcp/arbora_surface.py`'s `register_arbora_surface()` backing `FastMCP("arbora-backing")` registers native prompts/resources/templates.
- ARBORA-FLEET-R007.3 (child, consumer of .2, this repo): `arbora/mcp_server/server.py` serves the composed loop end to end + `tests/mcp/test_fastmcp4_native.py`.
- T05 — Build one filtered catalog for all six item kinds, native per-session mounts, cap/expiry/eviction, queued notifications, fallback calls, and credential delegation. Cover ARBORA-FLEET-R018, ARBORA-FLEET-R001, ARBORA-FLEET-R004.
- T06 — Install embedded/remote Eunomia policy in arbora. Prove exact scopes and principal rules at discover/load/call and fail-closed outage behavior. Cover ARBORA-FLEET-R019, ARBORA-FLEET-R021.
- T07 — Add atomic generation refresh, durable ordered delta receipts, last-known-good fallback, in-flight drain, replay, and replica convergence. Cover ARBORA-FLEET-R022 and ARBORA-FLEET-R051.
- T08 — Certify actual connector schemas and annotations; expose typed write-back and sanitized telemetry/security/CI feed metadata through registry ops. Cover ARBORA-FLEET-R002, ARBORA-FLEET-R003, ARBORA-FLEET-R008.
- T08 — Certify actual connector schemas and annotations; expose typed write-back and sanitized telemetry/security/CI feed metadata through registry ops. Cover ARBORA-FLEET-R002, ARBORA-FLEET-R003, ARBORA-FLEET-R008.
- T09 — Correct generated assembly agent parsing. Cover ARBORA-FLEET-R010. (ARBORA-FLEET-R009 split into T09.1-T09.3 below.)
- T09.1 (ARBORA-FLEET-R009.1): Build the typed capacity-acquisition primitive (ledger): all-or-nothing acquire, exactly one re-decision on denial, release on stop.
- T09.2 (ARBORA-FLEET-R009.2): Wire the capacity ledger into `control_plane/runs/admission.py`'s `admit_once`.
- T09.3 (ARBORA-FLEET-R009.3): Wire the capacity ledger into `arbora/a2a/routing.py` and `composition.py`'s publish step.
- T08 — Certify actual connector schemas and annotations; expose typed write-back and sanitized telemetry/security/CI feed metadata through registry ops. Cover ARBORA-FLEET-R002, ARBORA-FLEET-R003, ARBORA-FLEET-R008. ARBORA-FLEET-R003 is now a rollup over ARBORA-FLEET-R003.1-.3.
- ARBORA-FLEET-R002.1 (child, producer): `arbora/fleet/catalog_items.py` (`compute_schema_fingerprint`, `CatalogItem.schema_fingerprint`, wired into `_probed_item`) + `tests/fleet/test_schema_fingerprint.py`.
- ARBORA-FLEET-R003.1 (child, producer): `arbora/api/ops/write_back.py` (`WriteBackPreviewRequest`, `WriteBackPreviewResult`, `preview_write_back`) + `tests/api/test_write_back_preview.py`.
- ARBORA-FLEET-R003.2 (child): bind the ARBORA-FLEET-R003.1 preview model into the operation registry as an `OpSpec` (`Confirm.PLAN`, `Idempotency.KEY_REQUIRED`), plus a restart/fault test.
- ARBORA-FLEET-R008.1 (child, producer): `arbora/fleet/feeds.py` (`FeedEvent`, `FeedEventRejected`, `rum_event`, `security_audit_event`, `cicd_event`) + `tests/fleet/test_feeds.py`.
- ARBORA-FLEET-R008.2 (child): wire real connector ingestion for each feed and pass events through `connector_items()`/`_pack_annotations()` into the registry/tenant-boundary check.
- T09 — Correct generated assembly agent parsing and all-or-nothing capacity admission, one re-decision, and release on stop. Cover ARBORA-FLEET-R009, ARBORA-FLEET-R010.
- ARBORA-FLEET-R009.1 (child): `arbora/a2a/routing.py` evaluates a candidate run against the current admission state.
- ARBORA-FLEET-R009.2 (child): `arbora/a2a/composition.py` commits an evaluated run together with its synthesis evidence.
- ARBORA-FLEET-R009.3 (child): `control_plane/runs/admission.py` publishes a committed run only after an all-or-nothing capacity acquisition, with one re-decision on denial and release on stop.
- ARBORA-FLEET-R003.1 (child, producer): `arbora/api/ops/write_back.py` (`WriteBackPreviewParams`, `WriteBackCommitParams`, `WriteBackPreview`, `WriteBackReceipt`, `derive_write_back_idempotency_key`) + `tests/api/test_write_back_models.py`.
- ARBORA-FLEET-R003.2 (child, consumer of .1): register `fleet.write_back.preview`/`fleet.write_back.commit` as typed `OpSpec` entries in `arbora/api/ops/write_back.py`, wired into `arbora/api/ops/registry_factory.py` + `tests/api/test_write_back_ops.py`.
- ARBORA-FLEET-R003.3 (child, consumer of .2): restart/fault test `tests/fleet/test_write_back_restart.py` proving idempotent replay and preview gating survive a service restart.
- T09 — Correct generated assembly agent parsing and all-or-nothing capacity admission, one re-decision, and release on stop. Cover ARBORA-FLEET-R009, ARBORA-FLEET-R010.
- T09.1 — Capacity-acquisition primitive (acquire/release, one re-decision) in `arbora/control_plane/runs/admission.py`. Cover ARBORA-FLEET-R009.1.
- T09.2 — Wire `arbora/a2a/routing.py`/`composition.py` evaluate/commit/publish through the ARBORA-FLEET-R009.1 primitive. Cover ARBORA-FLEET-R009.2.
- T10 — Run clean-checkout format/lint/types/tests, generated artifact and public-surface checks, CCCC, KISS, jscpd, dupehound, authority parity, and local served protocol matrix. Cover ARBORA-FLEET-R016, ARBORA-FLEET-R017.
- T11 — Land reviewed code and record merge commits per slice in `spec.md`; source evidence alone does not establish completed acceptance.
- T12 — Run the disposable two-replica reload and fault probe against the landed revision. Attach generation, receipt, trace, timing, and policy evidence. Record completed acceptance only for passing slices.
- T13 — Add or verify a check script that scans every fleet connector count-pin site (compatibility matrix, bundle-catalog schema, check scripts, `ontology.lock`, federated IRI, `genesis.yaml`) and fails when any site disagrees with the live connector count. Cover ARBORA-FLEET-R023, not covered by T01–T12.
- T14 (ARBORA-FLEET-R024): Take over fleet reconciliation, autoscaling, scaling authority and deploy watch from the agent runtime as typed Arbora operations with parity and authorization tests. Split into T14.1–T14.3 below; ARBORA-FLEET-R024 is now a rollup over ARBORA-FLEET-R024.1–R024.3.
- ARBORA-FLEET-R023.1 (child, producer): `arbora/fleet/connector_count_pins.py` + `scripts/check_connector_count_pins.py` + `tests/test_check_connector_count_pins.py`. Real sites (compatibility matrix, bundle-catalog schema, `ontology.lock`, federated IRI) remain cross-repo/AU-owned follow-up.
- T14 (ARBORA-FLEET-R024): Take over fleet reconciliation, autoscaling, scaling authority and deploy watch from the agent runtime as typed Arbora operations with parity and authorization tests.
- T14 (ARBORA-FLEET-R024): Take over fleet reconciliation, autoscaling, scaling authority and deploy watch from the agent runtime as typed Arbora operations with parity and authorization tests. Split into T14.1–T14.3 below; ARBORA-FLEET-R024 is now a rollup over ARBORA-FLEET-R024.1–R024.3.
- T14.1 (ARBORA-FLEET-R024.1): Port the deploy-watch verdict decision (`agent_utilities.orchestration.deploy_watch.run_deploy_watch`'s probe-reduction rule) as a typed `fleet.deploy_watch.evaluate` Arbora operation behind the shared registry caller/scope/policy chokepoint, with parity tests against the original success/failed/unobserved rule and a negative test refusing a caller without the `mcp:delegate` scope.
- T14.2 (ARBORA-FLEET-R024.2): Port the remaining fleet reconciler, autoscaler, scaling-authority and deploy-watch scheduling/rollback-dispatch behavior as typed Arbora operations, wired into the served operation registry, with parity and authorization tests matching T14.1's pattern.
- T14.3 (ARBORA-FLEET-R024.3): Cross-repo — remove the superseded reconciler/autoscaler/scaling-authority/deploy-watch modules from meristem's `orchestration/` package and redirect its agent runner and planning code to call the Arbora operations from T14.1–T14.2. Blocked on T14.2 landing; out of scope for this (arbora-only) lane.
- T19 (ARBORA-FLEET-R029): Serve the meristem intent contract, register A2A, browser and RLM as `act` operations, and remove the fleet meta-tools and visibility middleware. Merge together with meristem PR #54. (Landed via PR #51, merge commit `db8d44259c3da06c2b0a48b685ca87a01debaaca`; `arbora/mcp_server/server.py` calls `backing_server(mcp)` from `agent_utilities.mcp.arbora_surface`; no `SessionVisibilityMiddleware` class remains; `tests/mcp_server/test_graph_tool_surface.py` passes.)
- T20 (ARBORA-FLEET-R029): Remove `MCP_TOOL_MODE` from the fleet configuration: the MCP config files, deployment manifests, config generator and README environment tables.
- T21 (ARBORA-FLEET-R029): Retire `MCP_ALWAYS_LOAD` and `MCP_ALWAYS_LOAD_TOOLS` and the eager-mount path in `arbora/fleet/multiplexer.py`. A fleet call mounts what it needs.
- T22 (ARBORA-FLEET-R029): Rename the internal `find_tools`/`load_tools`/`unload_tools`/`multiplexer_status` methods and comments in `arbora/fleet/` and `arbora/api/ops/fleet.py` to the intent operation names.
- **ARBORA-FLEET-R029.1:** Refuse an empty unscoped `fleet.catalog.search` as `INVALID_ARGUMENT` (HTTP 400), not an internal error.
- **ARBORA-FLEET-R015.1:** Classify a failed `fleet.call` by the cause inside the task group. Log that cause. Keep a remote tracing-service child in the catalog without keys.
- **ARBORA-FLEET-R015.3:** Deny an unreadable policy ledger as `ledger_unreadable` with its cause, never as a spent rate budget.
- T15 (ARBORA-FLEET-R026): Skip a registration without a component, log it, and report it in `multiplexer_status`.
- T16 (ARBORA-FLEET-R027): Add `arbora/fleet/onboarding.py`, the `onboard-fleet` command and the background boot pass.
- T17 (ARBORA-FLEET-R028): Renew fleet and self-served leases each pass with windowed idempotency keys; refresh the catalog after admission.
- T18 (ARBORA-FLEET-R027): Bind Arbora as the fleet importer in the deployment, roll out, and record the live `multiplexer_status` child count.
- T24 (ARBORA-FLEET-R027): Skip re-attest and re-import for an already-admitted server whose captured pack digest is unchanged; renew it instead of re-onboarding. Re-attesting unchanged content reused attest's digest-derived idempotency key under a freshly randomized request body, which EG refused as `IDEMPOTENCY_CONFLICT` on every pass after the first.
- T18 (ARBORA-FLEET-R030): Register each connector access contract as an unapproved virtual mapping during onboarding.
- T19 (ARBORA-FLEET-R030): Read ontology entries from the real pack archive accessor; confirm the duck-typed `entries` read against the SDK archive.
- T23 (ARBORA-FLEET-R030): Parse only ontology bodies that use the access-contract vocabulary. Log and skip a body the parser rejects.
- T20 (ARBORA-FLEET-R030): Bind each source to a live operation call and add an operator approval path.
- T24 (ARBORA-FLEET-R031): Add `MCPMultiplexer._catalog_tool_probe_info` reading tool descriptors off the installed EG catalog snapshot, and fall back to it in `discover_tools` only for a server whose live probe errored.

## MCPProjection reachability follow-up (2026-10-08, this lane)

Investigated whether `arbora/api/mcp/verbs.py`'s `MCPProjection` (R013) should
get a real caller outside `arbora/mcp_server/server.py`'s boot sequence, per
R012/R013's spec text. Finding: no, not as written. `agent_utilities.mcp.arbora_surface`
— the module PR #51 actually wired into the served bridge (`backing_server(mcp)`
in `server.py`) — imports only `agent_utilities.mcp._arbora_action_manifest`,
`intent_contract` and `tool_specs`. It does not import anything from
`arbora.api.*`. Confirmed with `grep -n "^from\|^import"` on
`agent_utilities/mcp/arbora_surface.py`.

That means the whole `arbora/api/registry/`, `arbora/api/invoke/` and
`arbora/api/mcp/` tree (R011, R012, R013, R014, R015, plus R007's
`arbora/api/mcp/resources.py` native `@mcp.resource(...)` registrations) has
no production caller and no architectural reason to gain one: the six-verb
surface it was built to serve is already shipped through a separate,
self-contained implementation. Wiring `MCPProjection` into any serving path
now would stand up a second, competing tool surface next to the one actually
in production — not a fix, a new problem. Recommend the orchestrator choose
one of:
- delete `arbora/api/registry/`, `arbora/api/invoke/`, `arbora/api/mcp/`
  and their tests (confirm no other reachable caller first), or
- mark R011/R012/R013/R014/R015 and R007's resource-registration clause
  `SUPERSEDED` by R029, the same disposition already due for R018/R020.

Also checked ARBORA-FLEET-R002's "empty placeholder" tool-schema fingerprint
language. The one hit, `policy.fingerprint_catalog(catalog)`
(`arbora/fleet/multiplexer.py:2918`), calls a method that
`_RUNTIME_CHILD_POLICY_METHODS` (same file, line 1093) requires any runtime
child policy to implement, but this is a *catalog*-level fingerprint on the
transport/spawn policy protocol, not the per-tool-pin schema fingerprint
R002 describes. Did not find where a per-tool-pin schema fingerprint is
computed or defaulted to empty; R002's concrete gap still needs locating
before it can be fixed.

## Spec-sweep audit notes (2026-10-08, this lane)

- ARBORA-FLEET-R026/R027/R028/R029/R030 have source on `origin/main` as of `7e3628a`: `arbora/fleet/onboarding.py` matches their descriptions line for line, `tests/fleet/test_fleet_onboarding.py` + `tests/fleet/test_fleet_access_contracts.py` + `tests/test_fleet_catalog_reader.py` + `tests/mcp_server/test_graph_tool_surface.py` all pass (33 tests) through the shared venv. The run used this worktree's checkout. Nothing further to do here.
- ARBORA-FLEET-R029's own spec text says it "supersedes the four resident tools in ARBORA-FLEET-R018 and ARBORA-FLEET-R020." PR #51 deleted the `SessionVisibilityMiddleware` class and the five fleet meta-tools. Review R018 and R020's old "replace the live multiplexer's own registered meta-tools" language. The replacement uses `agent_utilities.mcp.arbora_surface`. R018's `arbora/fleet/catalog_composition.py`/`multiplexer_ops.py` module still has no non-test caller.
- Flag for re-audit: `status.json` currently marks ARBORA-FLEET-R012/R013/R015 `LANDED`, but `MCPProjection` (`arbora/api/mcp/verbs.py`) is constructed only in `tests/api/test_mcp_verbs.py` — grep for a non-test importer finds none. The six-verb surface that actually ships is the separate `agent_utilities.mcp.arbora_surface` path from PR #51, not this `arbora/api/mcp/` package. Per the "landed means reachable" rule, this looks like a promotion based on merged-commit evidence alone, not reachability; the orchestrator should re-check before trusting these three as done, or confirm `arbora/api/mcp/` is deliberately superseded like R018/R020.
- Remaining items (R003, R011, R016, R017, R022) need composition into the served app's boot sequence or a design reconciliation. The `arbora/mcp_server/server.py` bootstrap block handles that composition, outside this lane's scope. Their recorded branch commits (`9a9b3f5c8e`, `3f5f354a36`, `fb06321fe1`, `1f3b30ff69`) conflict against current `main`. R022's conflict represents a behavioral regression. Its branch raises `FleetCatalogIntegrityError` on an unadmitted server. Main's R026 behavior skips that server and warns, so boot never fails. Porting that branch unchanged would restore the boot-failure mode R026 removed. This lane did not change those items.

Checked the remaining requirements (R004, R005, R006, R008, R009)
for existing partial code to extend instead of a fresh guess: none has one.
R004's capability/digest/modality/cost/latency fields do not exist anywhere
in `arbora/fleet/catalog_items.py`; R006's skill/prompt harvest removal
would cut deep into `arbora/fleet/multiplexer.py`'s probe-budget and
security-sensitive harvest machinery (not a small deletion); R009's capacity
admission has no code under `arbora/control_plane/runs/` or `arbora/a2a/`
beyond an unrelated audit-chain capacity check. None of these is a safe
10-minute slice without a real design decision first; none was attempted.

## Decomposition children (tracked)

- **ARBORA-FLEET-R002.2:** Remaining scope of ARBORA-FLEET-R002 (slice .2): Tool schema fingerprints recomputed from served schemas (rollup)
- **ARBORA-FLEET-R005.2:** Remaining scope of ARBORA-FLEET-R005 (slice .2): A2A task routing with typed work-item and lease operations (rollup)
- **ARBORA-FLEET-R009.2:** Routing and composition publish only through acquired capacity
- **ARBORA-FLEET-R008:** RUM, security-audit and CI/CD feeds join the catalog (rollup)

## Decomposition children (tracked)

- **ARBORA-FLEET-R051:** Catalog reload rejects invalid candidates, proves replica convergence
- **ARBORA-FLEET-R051.1:** Remaining scope of ARBORA-FLEET-R051 (slice .1): Catalog reload rejects invalid candidates, proves replica convergence
- **ARBORA-FLEET-R051.2:** Remaining scope of ARBORA-FLEET-R051 (slice .2): Catalog reload rejects invalid candidates, proves replica convergence
- **ARBORA-FLEET-R051.1:** Remaining scope of ARBORA-FLEET-R051 (slice .1): Catalog reload rejects invalid candidates, proves replica convergence (rollup)
- **ARBORA-FLEET-R051.2:** Remaining scope of ARBORA-FLEET-R051 (slice .2): Catalog reload rejects invalid candidates, proves replica convergence (rollup)
- **ARBORA-FLEET-R033:** Fleet-catalog `servers` read owned by arbora.
- **ARBORA-FLEET-R033.1:** Define the typed fleet-catalog `servers` reader.
- **ARBORA-FLEET-R033.2:** Add refusal tests for the `servers` reader.

## Certified fleet execution definitions

All tasks below remain unimplemented. The runtime integration tests have not run.

- **ARBORA-FLEET-R034.7:** Model: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R034.8:** Behavior: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R034.9:** Wiring: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R034.10:** Refusal: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R034.11:** Evidence: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R034.12:** Hot-path proof: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R024.4:** Model: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R024.5:** Behavior: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R024.6:** Wiring: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R024.7:** Refusal: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R024.8:** Evidence: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R024.9:** Hot-path proof: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R002.3:** Model: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R002.4:** Behavior: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R002.5:** Wiring: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R002.6:** Refusal: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R002.7:** Evidence: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R002.8:** Hot-path proof: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R027.1:** Model: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R027.2:** Behavior: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R027.3:** Wiring: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R027.4:** Refusal: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R027.5:** Evidence: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R027.6:** Hot-path proof: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R019.1:** Behavior: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R021.1:** Behavior: implement the requirement and bind its served acceptance test.
- **ARBORA-FLEET-R005.3:** Behavior: implement the requirement and bind its served acceptance test.

## Original scope residual acceptance

Historical parent evidence does not bind these new child IDs. Preserve the complete acceptance below before marking any child delivered.

- **ARBORA-FLEET-R019.2:** Original policy contract; bind and verify the complete acceptance cases.
- **ARBORA-FLEET-R021.2:** Original fleet authority contract; bind and verify the complete acceptance cases.
- **ARBORA-FLEET-R027.7:** Original fleet onboarding contract; bind and verify the complete acceptance cases.

## Repository source-effect composition units

- **ARBORA-FLEET-R003.4:** Implement original writable-fleet acceptance residual and bind the served acceptance defined in test-spec.md.
- **ARBORA-FLEET-R003.5:** Implement complete repository request binding and bind the served acceptance defined in test-spec.md.
- **ARBORA-FLEET-R003.6:** Implement trusted composition through shared invoke and bind the served acceptance defined in test-spec.md.
- **ARBORA-FLEET-R003.7:** Implement preview and approval bind the commit operation and bind the served acceptance defined in test-spec.md.
- **ARBORA-FLEET-R003.8:** Implement served effect and independent readback evidence and bind the served acceptance defined in test-spec.md.
- **ARBORA-FLEET-R003.9:** Implement zero-effect refusal parity and bind the served acceptance defined in test-spec.md.
- **ARBORA-FLEET-R003.10:** Implement durable replay across host and connector restart and bind the served acceptance defined in test-spec.md.

Preserve all existing R003 tasks and receipts. These added runtime cases have not run for this definition change.


## OpenAPI operation schema preflight

- [ ] T001 ARBORA-FLEET-R035 Bind schema-bound OpenAPI operation aliases to the admitted manifest and complete ordered SDK reconstruction on the invocation session. Compare both input/output schemas and action. Recheck authority/generation for every read and call. Disable uncertain-write replay, update the MCP dependency floor/lock, and record focused-vs-live evidence.

## Additional owner requirements

- [ ] T002 ARBORA-FLEET-R024.10 Deliver every requirement in requirements.md.
- [ ] T003 ARBORA-FLEET-R024.11 Deliver every requirement in requirements.md.
- [ ] T004 ARBORA-FLEET-R002.9 Deliver every requirement in requirements.md.
- [ ] T005 ARBORA-FLEET-R034.13 Deliver every requirement in requirements.md.
- [ ] T006 ARBORA-FLEET-R034.14 Deliver every requirement in requirements.md.

- **ARBORA-FLEET-R036 (rollup):** The connector contract is established on first connect (rollup).
- **ARBORA-FLEET-R036.1 (Model):** Add the typed model.
- **ARBORA-FLEET-R036.2 (Behavior):** Implement the behavior.
- **ARBORA-FLEET-R036.3 (Wiring):** Wire the production call site.
- **ARBORA-FLEET-R036.4 (Refusal):** Add the typed refusals.
- **ARBORA-FLEET-R036.5 (Evidence):** Emit the evidence.
- **ARBORA-FLEET-R036.6 (Hot-path proof):** Add the served hot-path test.
- **ARBORA-FLEET-R037 (rollup):** Tools load per job from the engine answer (rollup).
- **ARBORA-FLEET-R037.1 (Model):** Add the typed model.
- **ARBORA-FLEET-R037.2 (Behavior):** Implement the behavior.
- **ARBORA-FLEET-R037.3 (Wiring):** Wire the production call site.
- **ARBORA-FLEET-R037.4 (Refusal):** Add the typed refusals.
- **ARBORA-FLEET-R037.5 (Evidence):** Emit the evidence.
- **ARBORA-FLEET-R037.6 (Hot-path proof):** Add the served hot-path test.
- **ARBORA-FLEET-R038:** One protocol stack with one implementation per role.
- [x] T007 ARBORA-FLEET-R027.8 — `onboard-fleet --allow-mass-withdrawal` passes the operator's decision to the pack import.

- **ARBORA-FLEET-R041:** Registering a connector imports its full pack with no manual step (rollup).
- **ARBORA-FLEET-R041.1:** Model.
- **ARBORA-FLEET-R041.2:** Behavior.
- **ARBORA-FLEET-R041.3:** Wiring.
- **ARBORA-FLEET-R041.4:** Refusal.
- **ARBORA-FLEET-R041.5:** Evidence.
- **ARBORA-FLEET-R041.6:** Hot-path proof.
- **ARBORA-FLEET-R041.7:** Handshake trigger: start one `onboard` pass when the multiplexer completes the handshake.
- **ARBORA-FLEET-R041.8:** Reconnect compare: re-capture only on a changed `serverInfo` version or manifest digest; renew otherwise.
- **ARBORA-FLEET-R041.9:** Serving never blocks: log, report in the surface report, retry with backoff. Evidence: `tests/fleet/test_handshake_onboard.py::test_failed_capture_serves_tools_reports_gap_and_retries_with_backoff`.
- **ARBORA-FLEET-R041.10:** Rules unchanged: keep mass-withdrawal approval; keep `onboard-fleet` as manual or bulk re-sync.
- **ARBORA-FLEET-R015.2:** Answer a connector's destructive confirmation from a consumed plan; decline everything else.
- **ARBORA-FLEET-R041.15:** Read captured pack bodies from the SDK 0.5 archive section.
- **ARBORA-FLEET-R041.16:** Serve connector pack status as a read (`fleet.packs.status`).
- **ARBORA-FLEET-R041.17:** Write the agent-library catalog nodes into EG on each onboarding pass.
- **ARBORA-FLEET-R041.18:** Run the off-loop onboarding passes under the temporary client scope.
- **ARBORA-FLEET-R042:** arbora registers connector data resources as virtual-graph sources by policy (rollup).
- **ARBORA-FLEET-R042.1:** Model.
- **ARBORA-FLEET-R042.2:** Behavior.
- **ARBORA-FLEET-R042.3:** Wiring.
- **ARBORA-FLEET-R042.4:** Refusal.
- **ARBORA-FLEET-R042.5:** Evidence.
- **ARBORA-FLEET-R042.6:** Hot-path proof.
- **ARBORA-FLEET-R043:** arbora serves virtual-graph queries over connector data through the one door (rollup).
- **ARBORA-FLEET-R043.1:** Model.
- **ARBORA-FLEET-R043.2:** Behavior.
- **ARBORA-FLEET-R043.3:** Wiring.
- **ARBORA-FLEET-R043.4:** Refusal.
- **ARBORA-FLEET-R043.5:** Evidence.
- **ARBORA-FLEET-R043.6:** Hot-path proof.
- **ARBORA-FLEET-R043.7:** Source executor. Evidence: `tests/fleet/test_source_executor.py`.
- **ARBORA-FLEET-R044:** dev4 content surface.
- **ARBORA-FLEET-R044.1:** dev4 content surface.
- **ARBORA-FLEET-R044.2:** dev4 content surface.
- **ARBORA-FLEET-R044.3:** dev4 content surface.
- **ARBORA-FLEET-R044.4:** dev4 content surface.
- **ARBORA-FLEET-R044.5:** dev4 content surface.

### R015/R029 WebUI bridge integration evidence

The WebUI bridge is configured by the host and discovered through the existing preloaded `find` tool. Browser API support and active per-session registrations are separate facts. No preload grants identity, attended consent, execution permission or a browser session. Boot, discovery, owner isolation and teardown regression checks are required; full web-agent execution acceptance remains open.

- Qualify R015/R029 live agent-to-attended-browser proposals end to end, including exact session, human acceptance, native run cancellation and existing mutation confirmation.

## Medulla companion rows (2026-10-28)

- [ ] T008 ARBORA-FLEET-R052 Deliver the requirement in requirements.md.
- [ ] T009 ARBORA-FLEET-R052.1 Deliver the requirement in requirements.md.
- [ ] T010 ARBORA-FLEET-R052.2 Deliver the requirement in requirements.md.
- [ ] T011 ARBORA-FLEET-R052.3 Deliver the requirement in requirements.md.
- [ ] T012 ARBORA-FLEET-R052.4 Deliver the requirement in requirements.md.
- [ ] T013 ARBORA-FLEET-R052.4.1 Deliver the requirement in requirements.md.
- [ ] T014 ARBORA-FLEET-R052.4.2 Deliver the requirement in requirements.md.
- [ ] T015 ARBORA-FLEET-R052.4.3 Deliver the requirement in requirements.md.
- [ ] T016 ARBORA-FLEET-R052.4.4 Deliver the requirement in requirements.md.
- [ ] T017 ARBORA-FLEET-R052.4.5 Deliver the requirement in requirements.md.
- [ ] T018 ARBORA-FLEET-R052.4.6 Deliver the requirement in requirements.md.
- [ ] T019 ARBORA-FLEET-R053 Deliver the requirement in requirements.md.
- [ ] T020 ARBORA-FLEET-R045 Deliver the requirement in requirements.md.
- [ ] T021 ARBORA-FLEET-R034.15 Deliver the requirement in requirements.md.
- [ ] T022 ARBORA-FLEET-R001 Typed pack publisher replaces placeholder ContentPack.
- [ ] T023 ARBORA-FLEET-R002 Tool schema fingerprints recomputed from served schemas (rollup).
- [ ] T024 ARBORA-FLEET-R002.1 Per-tool schema fingerprint computed from the served schema.
- [ ] T025 ARBORA-FLEET-R003 D18 write-back exposed only as a typed, previewed operation (rollup).
- [ ] T026 ARBORA-FLEET-R003.1 Typed write-back preview model and its refusal rule.
- [ ] T027 ARBORA-FLEET-R003.2 Write-back registered as a typed, gated registry operation, not a native child tool.
- [ ] T028 ARBORA-FLEET-R004 Pack annotations carry capability, digest, modality, cost, latency.
- [ ] T029 ARBORA-FLEET-R005 A2A task routing with typed work-item and lease operations (rollup).
- [ ] T030 ARBORA-FLEET-R005.1 Typed WorkItem view, the GetWorkItem/ListWorkItems producer.
- [ ] T031 ARBORA-FLEET-R006 Multiplexer skill and prompt harvest path removed.
- [ ] T032 ARBORA-FLEET-R007 Bridge runs on FastMCP 4 with native primitives (rollup).
- [ ] T033 ARBORA-FLEET-R008 RUM, security-audit and CI/CD feeds join the catalog (rollup).
- [ ] T034 ARBORA-FLEET-R008.1 Typed feed-event model per feed kind.
- [ ] T035 ARBORA-FLEET-R008.2 Live feed ingestion wired into the catalog.
- [ ] T036 ARBORA-FLEET-R009 Run admission evaluates, commits and publishes with capacity control.
- [ ] T037 ARBORA-FLEET-R009.1 Capacity acquisition is all-or-nothing with one re-decision.
- [ ] T038 ARBORA-FLEET-R009.2 Routing and composition publish only through acquired capacity.
- [ ] T039 ARBORA-FLEET-R009.3 Run admission publishes with all-or-nothing capacity control.
- [ ] T040 ARBORA-FLEET-R003.3 Write-back idempotency and preview gating survive a service restart.
- [ ] T041 ARBORA-FLEET-R006.1 Harvest call-site inventory, the removal baseline.
- [ ] T042 ARBORA-FLEET-R006.2 Skill/prompt body-harvest cutover for an already-admitted child.
- [ ] T043 ARBORA-FLEET-R006.3 Dead harvest helpers removed (rollup over `.3.1` to `.3.4`).
- [ ] T044 ARBORA-FLEET-R006.3.1 Deliver `_probe_protocol_families` harvest calls routed through the cutover helper.
- [ ] T045 ARBORA-FLEET-R006.3.2 Non-admitted fallback and `_harvest_resource_bodies` deleted.
- [ ] T046 ARBORA-FLEET-R006.3.3 Deadline and error-reason helpers deleted.
- [ ] T047 ARBORA-FLEET-R006.3.4 Harvest specs deleted.
- [ ] T048 ARBORA-FLEET-R006.4 Residual harvest-named symbol renamed off the removed path.
- [ ] T049 ARBORA-FLEET-R006.5 Closing audit: no harvest or hydration code remains.
- [ ] T050 ARBORA-FLEET-R007.1 Connector SDK server registers native prompts, resources and templates.
- [ ] T051 ARBORA-FLEET-R007.2 meristem's arbora bridge surface gains native prompt/resource/template registration (cross-repo, owner: meristem).
- [ ] T052 ARBORA-FLEET-R007.3 The served bridge exercises native prompts, resources and templates end to end.
- [ ] T053 ARBORA-FLEET-R011 Registry core computes a canonical digest and authorized find.
- [ ] T054 ARBORA-FLEET-R012 EG-direct binding generator with reviewed exclusions.
- [ ] T055 ARBORA-FLEET-R012.1 Engine operations come from the engine's served descriptors, not a contract file.
- [ ] T056 ARBORA-FLEET-R013 MCP projection exposes the six verbs with typed schemas.
- [ ] T057 ARBORA-FLEET-R014 Resolver ranks catalog items with partitioned feedback.
- [ ] T058 ARBORA-FLEET-R014.1 Deliver `ask` does not route a plain question to an op it cannot run.
- [ ] T059 ARBORA-FLEET-R014.2 A request verb is not evidence for an op.
- [ ] T060 ARBORA-FLEET-R014.3 A plain question goes to the engine planner unless an op names it.
- [ ] T061 ARBORA-FLEET-R015 Typed operations cover agents, browser and fleet calls.
- [ ] T062 ARBORA-FLEET-R015.1 Arbora classifies a failed `fleet.call` by its real cause.
- [ ] T063 ARBORA-FLEET-R015.2 A plan-confirmed destructive relay answers the connector's confirmation.
- [ ] T064 ARBORA-FLEET-R015.3 An unreadable policy ledger is denied with its cause, not as a spent budget.
- [ ] T065 ARBORA-FLEET-R016 Automated gates catch API surface and compatibility drift.
- [ ] T066 ARBORA-FLEET-R017 Authority-parity oracle validates principal, operation and surface.
- [ ] T067 ARBORA-FLEET-R018 Multiplexer exposes four resident fleet tools over one catalog.
- [ ] T068 ARBORA-FLEET-R019 Eunomia narrows authority at discover, load and call.
- [ ] T069 ARBORA-FLEET-R021 Fleet capability checks require exact scopes and deny unknown tools.
- [ ] T070 ARBORA-FLEET-R022 Catalog reconciliation re-ingests via durable replay.
- [ ] T071 ARBORA-FLEET-R051 Catalog reload rejects invalid candidates, proves replica convergence (rollup).
- [ ] T072 ARBORA-FLEET-R023 Connector count pins stay consistent across the fleet.
- [ ] T073 ARBORA-FLEET-R023.1 Pure connector count-pin comparison (producer).
- [ ] T074 ARBORA-FLEET-R024 Fleet reconciliation, scaling and deploy watch are served by arbora (rollup).
- [ ] T075 ARBORA-FLEET-R051.1 Catalog reload rejects invalid candidates.
- [ ] T076 ARBORA-FLEET-R051.2 Multi-replica convergence is observed by generation and digest.
- [ ] T077 ARBORA-FLEET-R024.1 Deploy-watch verdict is a typed Arbora operation.
- [ ] T078 ARBORA-FLEET-R024.2 Reconcile, scale and deploy-watch dispatch are served operations.
- [ ] T079 ARBORA-FLEET-R024.750 Fail-closed fleet health evidence.
- [ ] T080 ARBORA-FLEET-R025 Skills from every installed package, and an always-offered core pack, join the find/load path.
- [ ] T081 ARBORA-FLEET-R029 The intent tools are the only MCP surface.
- [ ] T082 ARBORA-FLEET-R029.1 Catalog search refuses an empty unscoped request.
- [ ] T083 ARBORA-FLEET-R026 The catalog admits each server on its own.
- [ ] T084 ARBORA-FLEET-R026.1 Catalog scale bound.
- [ ] T085 ARBORA-FLEET-R026.2 Fleet catalog reads run under the process authority.
- [ ] T086 ARBORA-FLEET-R027 Arbora onboards every configured fleet server.
- [ ] T087 ARBORA-FLEET-R028 Arbora renews every registration before its lease lapses.
- [ ] T088 ARBORA-FLEET-R030 Fleet onboarding registers connector access contracts unapproved.
- [ ] T089 ARBORA-FLEET-R031 Deliver `find` ranks catalog-admitted tools when a live probe cannot answer.
- [ ] T090 ARBORA-FLEET-R032 Gateway widget fetches run under a bound service actor.
- [ ] T091 ARBORA-FLEET-R033 Fleet-catalog `servers` read owned by arbora (rollup).
- [ ] T092 ARBORA-FLEET-R033.1 Define the typed fleet-catalog `servers` reader.
- [ ] T093 ARBORA-FLEET-R033.2 Refusals of the `servers` reader.
- [ ] T094 ARBORA-FLEET-R034 Skills and workflows are admitted before they run (rollup).
- [ ] T095 ARBORA-FLEET-R034.1 Model.
- [ ] T096 ARBORA-FLEET-R034.2 Behavior.
- [ ] T097 ARBORA-FLEET-R034.3 Wiring.
- [ ] T098 ARBORA-FLEET-R034.4 Refusal.
- [ ] T099 ARBORA-FLEET-R034.5 Evidence.
- [ ] T100 ARBORA-FLEET-R034.6 Hot-path proof.
- [ ] T101 ARBORA-FLEET-R034.7 Model.
- [ ] T102 ARBORA-FLEET-R034.8 Behavior.
- [ ] T103 ARBORA-FLEET-R034.9 Wiring.
- [ ] T104 ARBORA-FLEET-R034.10 Refusal.
- [ ] T105 ARBORA-FLEET-R034.11 Evidence.
- [ ] T106 ARBORA-FLEET-R034.12 Hot-path proof.
- [ ] T107 ARBORA-FLEET-R024.4 Model.
- [ ] T108 ARBORA-FLEET-R024.5 Behavior.
- [ ] T109 ARBORA-FLEET-R024.6 Wiring.
- [ ] T110 ARBORA-FLEET-R024.7 Refusal.
- [ ] T111 ARBORA-FLEET-R024.8 Evidence.
- [ ] T112 ARBORA-FLEET-R024.9 Hot-path proof.
- [ ] T113 ARBORA-FLEET-R002.3 Model.
- [ ] T114 ARBORA-FLEET-R002.4 Behavior.
- [ ] T115 ARBORA-FLEET-R002.5 Wiring.
- [ ] T116 ARBORA-FLEET-R002.6 Refusal.
- [ ] T117 ARBORA-FLEET-R002.7 Evidence.
- [ ] T118 ARBORA-FLEET-R002.8 Hot-path proof.
- [ ] T119 ARBORA-FLEET-R027.1 Model.
- [ ] T120 ARBORA-FLEET-R027.2 Behavior.
- [ ] T121 ARBORA-FLEET-R027.3 Wiring.
- [ ] T122 ARBORA-FLEET-R027.4 Refusal.
- [ ] T123 ARBORA-FLEET-R027.5 Evidence.
- [ ] T124 ARBORA-FLEET-R027.6 Hot-path proof.
- [ ] T125 ARBORA-FLEET-R019.1 Workflow admission applies the same narrowing boundary.
- [ ] T126 ARBORA-FLEET-R021.1 Every workflow tool requires its declaration and grant binding.
- [ ] T127 ARBORA-FLEET-R005.3 Integrated work-item distribution and provisioning use the R027 flow.
- [ ] T128 ARBORA-FLEET-R027.7.1 Production CLI report stream.
- [ ] T129 ARBORA-FLEET-R003.5 Complete repository request binding.
- [ ] T130 ARBORA-FLEET-R003.6 Trusted composition through shared invoke.
- [ ] T131 ARBORA-FLEET-R003.7 Preview and approval bind the commit operation.
- [ ] T132 ARBORA-FLEET-R003.8 Served effect and independent readback evidence.
- [ ] T133 ARBORA-FLEET-R003.9 Zero-effect refusal parity.
- [ ] T134 ARBORA-FLEET-R003.10 Durable replay across host and connector restart.
- [ ] T135 ARBORA-FLEET-R036 The connector contract is established on first connect (rollup).
- [ ] T136 ARBORA-FLEET-R036.1 Model.
- [ ] T137 ARBORA-FLEET-R036.2 Behavior.
- [ ] T138 ARBORA-FLEET-R036.3 Wiring.
- [ ] T139 ARBORA-FLEET-R036.4 Refusal.
- [ ] T140 ARBORA-FLEET-R036.5 Evidence.
- [ ] T141 ARBORA-FLEET-R036.6 Hot-path proof.
- [ ] T142 ARBORA-FLEET-R037 Tools load per job from the engine answer (rollup).
- [ ] T143 ARBORA-FLEET-R037.1 Model.
- [ ] T144 ARBORA-FLEET-R037.2 Behavior.
- [ ] T145 ARBORA-FLEET-R037.3 Wiring.
- [ ] T146 ARBORA-FLEET-R037.4 Refusal.
- [ ] T147 ARBORA-FLEET-R037.5 Evidence.
- [ ] T148 ARBORA-FLEET-R037.6 Hot-path proof.
- [ ] T149 ARBORA-FLEET-R038 One protocol stack with one implementation per role.
- [ ] T150 ARBORA-FLEET-R039 Domain pack reads compose engine methods (rollup).
- [ ] T151 ARBORA-FLEET-R039.1 Model.
- [ ] T152 ARBORA-FLEET-R039.2 Behavior.
- [ ] T153 ARBORA-FLEET-R039.3 Wiring.
- [ ] T154 ARBORA-FLEET-R039.4 Refusal.
- [ ] T155 ARBORA-FLEET-R039.5 Evidence.
- [ ] T156 ARBORA-FLEET-R039.6 Hot-path proof.
- [ ] T157 ARBORA-FLEET-R040 arbora serves and reports the prompts, skills and resources of each connector.
- [ ] T158 ARBORA-FLEET-R040.1 The served gateway forwards the relay ops.
- [ ] T159 ARBORA-FLEET-R040.2 A relay read survives a child restart.
- [ ] T160 ARBORA-FLEET-R041 Registering a connector imports its full pack with no manual step (rollup).
- [ ] T161 ARBORA-FLEET-R041.1 Model.
- [ ] T162 ARBORA-FLEET-R041.2 Behavior.
- [ ] T163 ARBORA-FLEET-R041.3 Wiring.
- [ ] T164 ARBORA-FLEET-R041.4 Refusal.
- [ ] T165 ARBORA-FLEET-R041.5 Evidence.
- [ ] T166 ARBORA-FLEET-R041.6 Hot-path proof.
- [ ] T167 ARBORA-FLEET-R041.7 Handshake trigger.
- [ ] T168 ARBORA-FLEET-R041.8 Reconnect compare.
- [ ] T169 ARBORA-FLEET-R041.9 Serving never blocks.
- [ ] T170 ARBORA-FLEET-R041.10 Rules unchanged.
- [ ] T171 ARBORA-FLEET-R041.11 A connector's declaration is captured as a pack resource.
- [ ] T172 ARBORA-FLEET-R041.12 The pack capture stays under the connector rate limit.
- [ ] T173 ARBORA-FLEET-R041.13 Mappings come from the captured declaration only.
- [ ] T174 ARBORA-FLEET-R041.14 Onboarding follows discovery and list changes.
- [ ] T175 ARBORA-FLEET-R041.15 Captured pack bodies are read from the archive section.
- [ ] T176 ARBORA-FLEET-R041.16 Connector pack status is a served read.
- [ ] T177 ARBORA-FLEET-R041.17 Onboarding writes the agent-library catalog into EG.
- [ ] T178 ARBORA-FLEET-R041.18 Off-loop passes own temporary EG transports.
- [ ] T179 ARBORA-FLEET-R041.19 A committed import with a failed projection is reported with its cause.
- [ ] T180 ARBORA-FLEET-R041.20 A long onboarding pass renews its graph authority between imports.
- [ ] T181 ARBORA-FLEET-R041.21 The catalog attest key covers the published component state.
- [ ] T182 ARBORA-FLEET-R042 arbora registers connector data resources as virtual-graph sources by policy (rollup).
- [ ] T183 ARBORA-FLEET-R042.1 Model.
- [ ] T184 ARBORA-FLEET-R042.2 Behavior.
- [ ] T185 ARBORA-FLEET-R042.3 Wiring.
- [ ] T186 ARBORA-FLEET-R042.4 Refusal.
- [ ] T187 ARBORA-FLEET-R042.5 Evidence.
- [ ] T188 ARBORA-FLEET-R042.6 Hot-path proof.
- [ ] T189 ARBORA-FLEET-R043 arbora serves virtual-graph queries over connector data through the one door (rollup).
- [ ] T190 ARBORA-FLEET-R043.1 Model.
- [ ] T191 ARBORA-FLEET-R043.2 Behavior.
- [ ] T192 ARBORA-FLEET-R043.3 Wiring.
- [ ] T193 ARBORA-FLEET-R043.4 Refusal.
- [ ] T194 ARBORA-FLEET-R043.5 Evidence.
- [ ] T195 ARBORA-FLEET-R043.6 Hot-path proof.
- [ ] T196 ARBORA-FLEET-R043.7 Source executor.
- [ ] T197 ARBORA-FLEET-R044 arbora serves all installed content as native MCP primitives, declared the same as every connector (rollup).
- [ ] T198 ARBORA-FLEET-R044.1 Skills as `skill://`.
- [ ] T199 ARBORA-FLEET-R044.2 Prompts as fastmcp prompts.
- [ ] T200 ARBORA-FLEET-R044.3 Engine core ontology.
- [ ] T201 ARBORA-FLEET-R044.4 arbora's own prompts.
- [ ] T202 ARBORA-FLEET-R044.5 Package content onboards like connector content.
- [ ] T203 ARBORA-FLEET-R041.17.1 Onboarding writes owner, version, term, lineage-hop and schema nodes with the asset set.
- [ ] T204 ARBORA-FLEET-R051.2.1 Convergence status is a served read.
- [ ] T205 ARBORA-FLEET-R052.5 Serve the cross-source questions through the intent surface.
- [ ] T206 ARBORA-FLEET-R054 Onboarding passes the seed body to the pack import.
- [ ] T207 ARBORA-FLEET-R055 Serve the composed execution answer through the intent surface.
- [ ] T208 ARBORA-FLEET-R056 Intent surface answers the job, downtime and plan questions.
- [ ] T209 ARBORA-FLEET-R057 Source executor relays read-time access checks.
- [ ] T210 ARBORA-FLEET-R058 Host bootstrap declares data-prep fields to the engine.

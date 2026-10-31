# Hosted API and intent operations — test contract

## Acceptance tests

Governing [spec](spec.md).

| Test | Requirements | Setup and observation |
|---|---|---|
| HO-T01 | HO-01, HO-07 | From a pinned public engine wheel enumerate `is_wire_callable` methods. Compare with curated/generated op bindings and explicit live exclusions. Fail on omissions, stale exclusions, duplicate ownership, or an unregistered Arbora route/tool. Regenerate artifacts twice and compare bytes/digest. |
| HO-T02 | HO-02 | Table-drive human, service, delegated agent, tenants, exact scopes, and surfaces through `invoke`. Assert same allow/deny code on MCP, HTTP and A2A; extra fields and identity/tenant spoofing fail before a handler call. |
| HO-T03 | HO-02, HO-08 | A service op with domain scope but no caller subject read fails. Subject-read plus service grant succeeds and creates a caller-owned record. Revoke either permission before deferred dispatch and assert zero effect. Cross-tenant subject always fails. |
| HO-T04 | HO-03 | Exact idempotent write replays one receipt; a missing key fails. Fuzzy mutation returns preview and creates no effect. Destructive plan is single-use and rejects expired, changed params, actor, tenant, policy revision or registry digest. Console-only admin rejects MCP/A2A confirmation and accepts same-human fresh-MFA console confirmation. |
| HO-T05 | HO-03, ARBORA-OPS-R036 | Make durable audit reservation fail: handler must not run. After effect, simulate audit outcome-link failure: return `INDETERMINATE`, retain reservation, and reconcile once. Assert no false rollback and no raw parameter value in durable audit. |
| HO-T06 | HO-04 | Discover each fleet item kind. Load a native tool and prove `tools/list`, direct native call and `act fleet.call` reach identical `invoke` policy/audit. Revoke scope/PDP rule, assert next call denied, tool removed and notification sent/queued. Cap 64 and hard 256. Explicit LRU. Idle TTL. Fallback usable when client ignores list-change. |
| HO-T07 | HO-05 | Generated HTTP route and MCP verb for one read and one effect reach the same service mock. Bearer and cookie+CSRF variants. Cursor paging. Idempotency header. Protocol routes inventoried. Retired route absent only after generated consumer exists. |
| HO-T08 | HO-06 | Every published engine code maps without rewording; unknown engine code is a contract failure. Fleet code/target preserved. Error envelope is stable and privacy scrubbed. Registry digest appears on success/error, registry ETag, and MCP instructions. Stable-operation breaking diff fails compatibility check. |
| HO-T09 | HO-07 | Exercise one representative typed op from each listed domain through the generated client. Missing upstream dependency returns `UNAVAILABLE` and is absent from usable discovery. No domain handler directly opens an engine, agent or child transport outside its public port. |
| HO-T10 | HO-08 | Per identity mode and PDP state: policy-off retains scope filter. Policy-on outage fails closed. Admin scope never implies delegate. Policy revision invalidates loaded items. Destructive/admin decisions are uncached. Discovery sets match across MCP, HTTP and A2A for one principal. |
| HO-T11 | HO-09 | On clean public checkout, install pinned dependencies and run focused tests without private endpoints/sibling trees. In CI provision ephemeral local services, call served MCP and HTTP on the same request, and verify one durable result and audit pair. |
| HO-T12 | HO-10 | Generate the former-tool/route inventory; every name is mapped or has reviewed drop reason. Use the generated Python and TypeScript clients in a real caller test. Search published source for retired names and verify no active consumer uses an old route. |
| HO-T13 | HO-11 | Boot one FastMCP loop and multiplexer. Enumerate tools, routes, and methods; compare them with the registry and protocol list. Remove one resident registration in a fixture; assert startup fails. A loaded forwarder is the only runtime addition. Its body calls invoke.
| HO-T14 | ARBORA-OPS-R039 | Build the dashboard router. All 15 old method and path pairs present |
| HO-T15 | ARBORA-OPS-R039 | Read-only caller on mutating routes. 403; API-key caller passes; health open |
| HO-T16 | ARBORA-OPS-R040 | Create with empty template or oversize data. Typed validation or 400 refusal |
| HO-T17 | ARBORA-OPS-R040 | Create then get. Id and rendered output returned; same artifact read back |
| HO-T18 | ARBORA-OPS-R040 | Get unknown id. 404 |
| HO-T19 | ARBORA-OPS-R040 | Refresh with inline data, with resolver, and with a failing resolver. New render; resolver render; prior render preserved with ok false |
| HO-T20 | ARBORA-OPS-R040 | Mount on the host app; read-only caller POSTs. Routes present; 403 on POST |
| HO-T21 | ARBORA-OPS-R041 | Discover registry widgets. genius_agent absent |
| HO-T22 | ARBORA-OPS-R021.4.1 | Run `tests/api/test_runs_ops.py::test_run_trace_reader_is_runtime_checkable_protocol`; it must pass and bind to this row. |
| HO-T23 | ARBORA-OPS-R021.4.2 | Run `tests/api/test_runs_ops.py::test_adapter_calls_engine_wire_method`; it must pass and bind to this row. |
| HO-T24 | ARBORA-OPS-R021.4.3 | Run `tests/api/test_runs_ops.py::test_run_metrics_models_reject_unknown_fields`; it must pass and bind to this row. |
| HO-T25 | ARBORA-OPS-R021.4.4 | Run `tests/api/test_runs_ops.py::test_runs_metrics_passes_engine_values_through`; it must pass and bind to this row. |
| HO-T26 | ARBORA-OPS-R021.4.5 | Run `tests/api/test_runs_ops.py::test_runs_get_returns_termination_context_and_applied_config`; it must pass and bind to this row. |
| HO-T27 | ARBORA-OPS-R021.4.6 | Run `tests/api/test_runs_ops.py::test_runs_ops_registered_with_existing_scopes`; it must pass and bind to this row. |
| HO-T28 | ARBORA-OPS-R061 | Planned test path: `tests/api/test_usage_indexed_served.py::test_indexed_usage_projection`. It remains unexecuted. See the R061 served-acceptance cases below. |
| HO-T29 | ARBORA-OPS-R063 | Planned test path: `tests/api/test_retrieval_evaluation_served.py::test_evaluation_and_original_utf8_context`. It remains unexecuted. See the R063 served-acceptance cases below. |

## Drift and scanner acceptance

Run `uv run pytest tests/api tests/fleet tests/mcp_server tests/gateway`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy arbora`, `uvx --from pre-commit==4.6.0 pre-commit run --all-files`, `uv build --wheel --out-dir dist`, and the repository's pinned scanner hooks. CCCC and KISS must stay within repository thresholds; jscpd adds zero clone pairs; Dupehound adds no new clones. Static checks reject transport calls outside the fleet executor, operation surfaces outside the registry, duplicate middleware authority, and frontend raw API bypass. Record exact command, tool versions, commit SHA, CI URL, and pass/fail in `evidence.md` before recording completed acceptance; `evidence.md` is created when evidence exists. Never invent a pass from this spec.

## Spec-audit rollup tests

- ARBORA-OPS-R050: the hot-path proof test passes and the typed refusal leaves no effect.
- ARBORA-OPS-R051: the hot-path proof test passes and the typed refusal leaves no effect.
- ARBORA-OPS-R052.4/.5/.6: `tests/integration/test_projection_scope.py` holds visible projections fixed while varying only hidden projections. Caller-visible response contents, counts, refusal details and trace fields must remain identical. A denied caller cannot read withheld counts through any audit view; its operator-audit request refuses without indirect count leakage. A separately authorized operator can read the withheld count. The typed refusal leaves no effect.
- ARBORA-OPS-R053: the hot-path proof test passes and the typed refusal leaves no effect.

R050/R051/R053 served fixtures cross shared invoke into public EG producer ports. They check engine verdict provenance and typed unavailability when a producer is absent. R051 uses MEDULLA-DECISION-ENGINE-R138 for scorer/verdict decisions and R133 for quotas/trace commits; AU only runs probes. R053 uses engine retrieval/context decisions and SDK source ingestion. Discovery fixtures use FLEET-R029 find/act/manage over R011/R013 registry machinery, with no retired fleet meta-tools or duplicate registry.

## Planned served acceptance for run reads

All paths below are planned tests, not execution receipts. Each test must bind its exact requirement ID.
The fixture must enter REST and MCP, traverse authorization and registration, and reach an engine-backed reader.

| Requirement | Planned test path | Positive served requirement | Negative served requirement |
|---|---|---|---|
| ARBORA-OPS-R021.4.1 | `tests/api/test_runs_served.py::test_reader_binding` | Both operations reach the bound reader with caller authority. | Missing binding returns `UNAVAILABLE` with no engine call. |
| ARBORA-OPS-R021.4.2 | `tests/api/test_runs_served.py::test_validated_trace_readback` | An ingested trace and decision edge survive engine readback. | Invalid ingestion stores nothing; another tenant cannot reveal the trace. |
| ARBORA-OPS-R021.4.3 | `tests/api/test_runs_served.py::test_metrics_grouping_and_abstention` | Mixed solved and failed traces preserve engine cost and grouping. | Zero solved traces abstain; malformed numeric values refuse. |
| ARBORA-OPS-R021.4.4 | `tests/api/test_runs_served.py::test_metrics_authority` | Authorized filters reach the engine unchanged. | Denied scope makes no engine call; no local aggregate replaces failure. |
| ARBORA-OPS-R021.4.5 | `tests/api/test_runs_served.py::test_summary_and_plan_projection` | Fields map exactly; an authorized fresh plan preserves order and revision. | Missing plan export stays unavailable; wrong-run, expired and regressing snapshots reveal no items. |
| ARBORA-OPS-R021.4.6 | `tests/api/test_runs_served.py::test_registered_rest_mcp_parity` | Generated clients invoke both registered transports with equal results. | Absent operation and malformed envelopes refuse without dispatch. |

## Planned versioned memory served acceptance

Each case must bind its exact child ID and enter real HTTP and MCP serving.
Use disposable durable engine storage and authenticated callers; do not mock transport, authorization, the host or persistence.
These paths are planned acceptance, not passing test receipts.

| Requirement | Planned test | Positive served requirement | Negative served requirement |
|---|---|---|---|
| ARBORA-OPS-R024.4.1 | `tests/api/test_memory_files_served.py::test_models_and_port_preserve_tagged_outcomes` | Generated outcomes survive the actual HTTP/MCP adapter and engine readback. | Missing methods refuse; a refused result has no shared head or stored receipt. |
| ARBORA-OPS-R024.4.2 | `tests/api/test_memory_files_served.py::test_read_list_preserve_scope_and_cursor` | Authorized listing retains stable ordering; exact revision reads return matching bytes. | Wrong-scope or expired cursors disclose no metadata; denied read reveals no existence. |
| ARBORA-OPS-R024.4.3 | `tests/api/test_memory_files_served.py::test_competing_saves_preserve_both_versions` | Two clients save one base; the stale save retains both versions across restart. | Capacity failure retains nothing; stale resolution leaves the accepted head unchanged. |
| ARBORA-OPS-R024.4.4 | `tests/api/test_memory_files_served.py::test_materialization_ack_checks_generation` | A fresh acknowledgment permits EG in_sync only after successful materialization. | Observe a new edit without changing the head, then deliver an older acknowledgment; it must refuse. |
| ARBORA-OPS-R024.4.5 | `tests/api/test_memory_files_served.py::test_authority_and_replay_are_non_disclosing` | Authorized same-body replay returns one durable receipt and performs no duplicate effects. | Denied existing, absent and creation requests match; changed-body replay and revoked replay disclose no head. |
| ARBORA-OPS-R024.4.6 | `tests/api/test_memory_files_served.py::test_complete_served_memory_file_journey` | Real host, Arbora and durable engine complete listing, conflict, resolution and acknowledgment. | Reject browser acknowledgment, symlink escape and unavailable producer; no mocked transport or persistence. |

## Owner-bound projection acceptance (ARBORA-OPS-R054–R058, R061, R063)

- `tests/api/test_agent_browser_ops.py::test_benchmark_activation_requires_technique_bound_evidence`: cover all five techniques, exact model/configuration, measured gain or non-regression, mismatched receipts, and no activation on refusal.
- `tests/api/test_ops_admin_ops.py::test_process_remediation_requires_run_bound_approval`: cover proposal-only behavior, explicit run/process-bound approval, stale or mismatched evidence, and zero process effects on refusal.
- `tests/api/test_decide_ops.py::test_changeset_review_binds_revision_classes_and_findings`: cover exact source revision, five flaw classes, evidence references, impact filtering, and refusal for missing or mismatched producer results.
- `tests/api/test_write_back_models.py::test_prompt_json_roundtrip_and_difference_oracle_is_stable`: cover stable serialization, semantic equivalence, true semantic changes, and source-effect refusals.
- `tests/gateway/test_schema_context_api.py::test_table_browser_projection_requires_owner_binding`: cover authorized data, denied scope, missing producer, and zero calls/effects on refusal. Add served WebUI consumer acceptance after owner contracts publish. These tests are proposed; none has run.
- ARBORA-OPS-R061: Planned paths: `tests/api/test_usage_indexed_ops.py::test_query_maps_filters_and_cursor_to_eg` and `tests/api/test_usage_indexed_served.py::test_indexed_usage_projection`. Serve authenticated HTTP and MCP, then enter ObservabilityView.tsx. Reconcile each model/project/agent group against an independent EG ledger. Check four disjoint counters and producer-derived totals. Check exact rational aggregation and half-even rounding once per final group/category. Verify PriceTableV1, minor units, accounting/currency revisions, coverage, index watermark, cursor, and pinned-FX separation. Check the R010.101 p95 limit: 500 ms from authenticated-handler entry through serialization. Use 1,000,000 indexed traces, 32 readers, and 1,000 fixed-window queries. Verify denied or foreign tenants, stale cursors/revisions, and absent, invalid, or exceeded profile evidence. Unknown attribution, price, and currency retain reasons and never become zero or a mixed-currency total. These cases are planned; none is a runtime receipt.
- ARBORA-OPS-R063: Planned operation test: `tests/api/test_retrieval_evaluation_ops.py::test_comparison_forwards_identical_inputs`. Planned served test: `tests/api/test_retrieval_evaluation_served.py::test_evaluation_and_original_utf8_context`. Serve two registered algorithms over one corpus, complete labeled query set, query-set ID/revision/digest, evaluator revision, and k. Use k from 1 through 1000. Keep corpus, labels, query set, evaluator, and query inputs identical across algorithms. Check each settings digest against that algorithm's pinned configuration. Check the hits_at_k denominator is the full identically labeled query set. Check the numerator counts queries with at least one relevant result in top k. Independently verify producer numerator, denominator, metric, and result digests through Arbora and WebUI reads. Exercise composed, decomposed, and multibyte text. Each half-open byte span must slice original stored bytes and match the stored-text digest. Escape HTML-hostile source before highlighting. Verify highlights preserve original byte spans. If normalization is used, check its separate NFC digest and validated map to original bytes. Refuse missing labels and zero denominators. Refuse mismatched corpus, labels, evaluator, or query inputs. Refuse missing or mismatched per-algorithm settings digests. Refuse incomplete results and denied or cross-tenant reads. Refuse stale cursors/revisions and invalid offsets/maps. Refuse 101 spans or any span over 8 KiB. These cases are planned, not runtime receipts.

## Additional owner requirements

- `ARBORA-OPS-R024.1.1`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-OPS-R014.1`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-OPS-R022.3`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-OPS-R022.4`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-OPS-R053.7`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-OPS-R021.2.1.1`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-OPS-R050.2.1`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-OPS-R009.1`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-OPS-R053.8`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-OPS-R059`: Planned `tests/api/test_evolution_metrics_served.py::test_proposal_metrics_preserve_owner_join_and_denial` proves the owner binding and served acceptance in plan.md. Cover HTTP, MCP, and the named browser consumer. No execution is claimed.
- `ARBORA-OPS-R060`: Planned `tests/api/test_sessions_served.py::test_full_manifest_sessions_and_costs_survive_restart` proves the owner binding and served acceptance in plan.md. Cover HTTP, MCP, and the named browser consumer. No execution is claimed.
- `ARBORA-OPS-R062`: Planned `tests/api/test_graph_changes_served.py::test_two_clients_recover_and_reauthorize_committed_feed` proves the owner binding and served acceptance in plan.md. Cover HTTP, MCP, and the named browser consumer. No execution is claimed.
- `ARBORA-OPS-R064`: Planned `tests/api/test_viewport_served.py::test_billion_point_viewport_keeps_bins_selection_and_byte_bounds` proves the owner binding and served acceptance in plan.md. Cover HTTP, MCP, and the named browser consumer. No execution is claimed.
- `ARBORA-OPS-R065`: Planned `tests/api/test_meeting_served.py::test_corrected_final_transcript_binds_selected_summary_provider` proves the owner binding and served acceptance in plan.md. Cover HTTP, MCP, and the named browser consumer. No execution is claimed.
- `ARBORA-OPS-R066`: Planned `tests/api/test_bookmarks_served.py::test_search_relationships_and_exports_share_authorized_snapshot` proves the owner binding and served acceptance in plan.md. Cover HTTP, MCP, and the named browser consumer. No execution is claimed.

## ARBORA-OPS-R025.1: Generated Python invocation

Run the client emitted by scripts/gen_api.py against an injected transport.
Unknown operations must make zero calls. Missing or changed response digests
must refuse both success and error envelopes. Matching envelopes retain their
result, error details and metadata. These tests do not qualify a producer wheel.

R025.2 retains full deterministic generation, drift and installed-wheel proof.
The emitted-client fixture is not this acceptance.

| HO-QUERY-01 | ARBORA-OPS-R018.2.1 | `tests/api/test_query_ops.py`, `tests/api/test_registry_factory.py`, and `tests/api/test_resolve.py` prove provider method, scope and inner schema compatibility. They prove authorized fallback, verified caller graph binding and provider error propagation. Tests record the final transport; they do not qualify a live NL planner or compose the production schema validator. |

## ARBORA-OPS-R025.2: Packaged generated artifacts

Run the canonical generator twice with the pinned packaged EG contract and
require `scripts/gen_api.py --check` to pass. Build the distribution from the
real generated source. `tests/deployment/test_generated_client_wheel.py` installs
that wheel without dependencies into an isolated target and imports its client
from outside the checkout. It verifies registry resources, operation membership
and matching response digests. Fixture-only generation cannot satisfy this test.

Reference acceptance resolves every generated provider snapshot and OpenAPI
reference. It compares resolved provider snapshots with the pinned source schema.
Negative cases reject missing schema files, invalid pointers, traversal and remote
references. Installed-wheel tests run outside the source tree without provider
packages and exercise both schema lookup and invocation.

## Parity composed operations acceptance (ARBORA-OPS-R801 to R842)

| Case | Row | Proving test and observation |
|---|---|---|
| Hot-path proof | `ARBORA-OPS-R801` | `tests/api/ops/test_graph_nodes_list.py::test_graph_nodes_list_served`: The test calls `graph.nodes.list` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R802` | `tests/api/ops/test_graph_view_read.py::test_graph_view_read_served`: The test calls `graph.view.read` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R803` | `tests/api/ops/test_graph_stats_read.py::test_graph_stats_read_served`: The test calls `graph.stats.read` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R804` | `tests/api/ops/test_graph_schema_read.py::test_graph_schema_read_served`: The test calls `graph.schema.read` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R805` | `tests/api/ops/test_graph_edges_write.py::test_graph_edges_write_served`: The test calls `graph.edges.write` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R806` | `tests/api/ops/test_graph_impact_read.py::test_graph_impact_read_served`: The test calls `graph.impact.read` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R807` | `tests/api/ops/test_code_context_read.py::test_code_context_read_served`: The test calls `code.context.read` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R808` | `tests/api/ops/test_kb_articles_read.py::test_kb_articles_read_served`: The test calls `kb.articles.read`, `kb.articles.write` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R809` | `tests/api/ops/test_sdd_documents_read.py::test_sdd_documents_read_served`: The test calls `sdd.documents.read`, `sdd.documents.write` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R810` | `tests/api/ops/test_agents_library_list.py::test_agents_library_list_served`: The test calls `agents.library.list`, `agents.library.write` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R811` | `tests/api/ops/test_agents_reload.py::test_agents_reload_served`: The test calls `agents.reload` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R812` | `tests/api/ops/test_agents_goals_run.py::test_agents_goals_run_served`: The test calls `agents.goals.run` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R813` | `tests/api/ops/test_ops_maintenance_run.py::test_ops_maintenance_run_served`: The test calls `ops.maintenance.run` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R814` | `tests/api/ops/test_ingest_pipeline_run.py::test_ingest_pipeline_run_served`: The test calls `ingest.pipeline.run` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R815` | `tests/api/ops/test_prompts_graph_read.py::test_prompts_graph_read_served`: The test calls `prompts.graph.read`, `prompts.graph.write` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R816` | `tests/api/ops/test_prompts_library_read.py::test_prompts_library_read_served`: The test calls `prompts.library.read` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R817` | `tests/api/ops/test_fleet_servers_manage.py::test_fleet_servers_manage_served`: The test calls `fleet.servers.manage` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R818` | `tests/api/ops/test_schedule_tasks_read.py::test_schedule_tasks_read_served`: The test calls `schedule.tasks.read` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R819` | `tests/api/ops/test_workflows_run.py::test_workflows_run_served`: The test calls `workflows.run`, `workflows.write` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R820` | `tests/api/ops/test_ontology_objects_read.py::test_ontology_objects_read_served`: The test calls `ontology.objects.read`, `ontology.objects.write` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R821` | `tests/api/ops/test_markets_signals_read.py::test_markets_signals_read_served`: The test calls `markets.signals.read` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R822` | `tests/api/ops/test_identity_verify.py::test_identity_verify_served`: The test calls `identity.verify` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R823` | `tests/api/ops/test_decide_claims_reconcile.py::test_decide_claims_reconcile_served`: The test calls `decide.claims.reconcile`, `decide.claims.explain` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R824` | `tests/api/ops/test_decide_promotion_submit.py::test_decide_promotion_submit_served`: The test calls `decide.promotion.submit` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R825` | `tests/api/ops/test_feedback_corrections_record.py::test_feedback_corrections_record_served`: The test calls `feedback.corrections.record` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R826` | `tests/api/ops/test_remediation_playbooks_route.py::test_remediation_playbooks_route_served`: The test calls `remediation.playbooks.route` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R827` | `tests/api/ops/test_comparative-analysis.py::test_comparative-analysis_served`: The test calls `skill:comparative-analysis` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R828` | `tests/api/ops/test_usage_read.py::test_usage_read_served`: The test calls `usage.read`, `usage.price_catalog.write` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R829` | `tests/api/ops/test_incidents_record.py::test_incidents_record_served`: The test calls `incidents.record`, `incidents.read` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R830` | `tests/api/ops/test_access_graph_ownership_infer.py::test_access_graph_ownership_infer_served`: The test calls `access.graph_ownership.infer`, `access.graph_ownership.apply` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R831` | `tests/api/ops/test_retrieval_search.py::test_retrieval_search_served`: The test calls `retrieval.search` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R832` | `tests/api/ops/test_query_uql.py::test_query_uql_served`: The test calls `query.uql` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R833` | `tests/api/ops/test_ingest_extraction_ground.py::test_ingest_extraction_ground_served`: The test calls `ingest.extraction.ground` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R834` | `tests/api/ops/test_traces_read.py::test_traces_read_served`: The test calls `traces.read` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R835` | `tests/api/ops/test_orchestration_questions_ask.py::test_orchestration_questions_ask_served`: The test calls `orchestration.questions.ask` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R836` | `tests/api/ops/test_placement_plan_read.py::test_placement_plan_read_served`: The test calls `placement.plan.read`, `placement.plan.apply` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R837` | `tests/api/ops/test_ingest_repositories_link_owners.py::test_ingest_repositories_link_owners_served`: The test calls `ingest.repositories.link_owners` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R838` | `tests/api/ops/test_ingest_foreign_sources_register.py::test_ingest_foreign_sources_register_served`: The test calls `ingest.foreign_sources.register`, `ingest.foreign_sources.sync` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R839` | `tests/api/ops/test_code_health_sweep.py::test_code_health_sweep_served`: The test calls `code.health.sweep` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R840` | `tests/api/ops/test_ingest_sessions_sync.py::test_ingest_sessions_sync_served`: The test calls `ingest.sessions.sync` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R841` | `tests/api/ops/test_ingest_packs_validate.py::test_ingest_packs_validate_served`: The test calls `ingest.packs.validate` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |
| Hot-path proof | `ARBORA-OPS-R842` | `tests/api/ops/test_actions_run.py::test_actions_run_served`: The test calls `actions.run` through the served route. It asserts the listed engine calls and a cross-tenant refusal. It asserts typed unavailable for a missing method. |

## ARBORA-OPS-R843 acceptance

| Case | Row | Proving test and observation |
|---|---|---|
| Evidence | `ARBORA-OPS-R843.1` | `tests/api/test_au_tool_parity.py::test_replaced_wrapper_methods_are_served`: each needed method is a generated operation. Every read tool maps only to a read-effect operation. |

## ARBORA-OPS-R067 acceptance

| Case | Row | Proving test and observation |
|---|---|---|
| Model | `ARBORA-OPS-R067.1` | `tests/api/test_research_web.py::test_params_and_result_round_trip`: A round trip keeps every field. |
| Behavior | `ARBORA-OPS-R067.2` | `tests/api/test_research_web.py::test_composes_four_steps_in_order`: The engine receives the query, the question and the work item in order. |
| Backend | `ARBORA-OPS-R067.3` | `tests/api/test_research_web_backends.py::test_explicit_archivebox_run`: A `backend=archivebox` run stores equal Documents. |
| Fallback | `ARBORA-OPS-R067.3` | `tests/api/test_research_web_backends.py::test_forced_fallback_run`: A failed primary page reaches `archivebox` once and records it. |
| Wiring | `ARBORA-OPS-R067.4` | `tests/api/test_research_web.py::test_served_invoke_reaches_handler`: A served call reaches the handler. |
| Refusal | `ARBORA-OPS-R067.5` | `tests/api/test_research_web.py::test_research_web_refusals`: Each case returns its code and enqueues nothing. |
| Evidence | `ARBORA-OPS-R067.6` | `tests/api/test_research_web.py::test_trace_names_decision_and_backends`: The trace holds the fields and no page text. |
| Hot-path proof | `ARBORA-OPS-R067.7` | `tests/gateway/test_research_web_served.py::test_research_web_default_archivebox_and_fallback`: Three served calls store equal Documents and cite the backend. |
| Admission | `ARBORA-OPS-R067.10` | Planned `tests/gateway/test_web_conformance_admission.py`: the served path accepts an exact current EG record. Missing, stale, revoked, mismatched and cross-tenant records cause zero backend calls. |
| Bound | `ARBORA-OPS-R067.9` | `tests/api/test_research_web_backends.py::test_hanging_archivebox_times_out_and_falls_back`: A backend that never answers fails with `timeout` and the other backend serves the page. |

Hot-path proof for `ARBORA-OPS-R067`: `tests/gateway/test_research_web_served.py::test_research_web_default_archivebox_and_fallback`.

## Connector sources (ARBORA-OPS-R020.6)

| Requirement | Case | Required assertion |
|---|---|---|
| `ARBORA-OPS-R020.6` | Connector sources served through the composed SDK sync runner | The rollup closes when `ARBORA-OPS-R020.6.1` through `ARBORA-OPS-R020.6.7` pass their proving tests. |
| `ARBORA-OPS-R020.6.1` | Model | `tests/ingest/test_connector_sources.py::test_source_record_shape` validates the record and refuses a malformed source ID. |
| `ARBORA-OPS-R020.6.2` | Behavior | `tests/ingest/test_connector_sources.py::test_list_sync_status_compose_runner` drives the three operations over a fake gateway and engine. |
| `ARBORA-OPS-R020.6.3` | Wiring | `tests/ingest/test_connector_sources.py::test_served_invocation_binds_ingest_runner` finds the composed runner in the operation runtime bindings. |
| `ARBORA-OPS-R020.6.4` | Refusal | `tests/ingest/test_connector_sources.py::test_refusals_fail_closed` covers each refusal and the failed WorkItem commit. |
| `ARBORA-OPS-R020.6.5` | Evidence | `tests/ingest/test_connector_sources.py::test_receipt_binds_work_item` compares the receipt job ID with the committed WorkItem. |
| `ARBORA-OPS-R020.6.6` | Hot-path proof | The test observes the declared source, a succeeded WorkItem and the ingested nodes with provenance. |
| `ARBORA-OPS-R020.6.7` | Pack inventory | The test lists one pack per declaring connector, reports `error` after a failed sync and refuses an unbound runner. |
| `ARBORA-OPS-R020.6.8` | Failure cause | `tests/ingest/test_connector_sources.py::test_a_failed_sync_carries_its_cause_to_the_receipt_and_job`, `::test_a_completed_sync_has_no_reason`; `tests/fleet/test_child_large_results.py::test_a_two_mib_child_result_reaches_arbora`. |

## Refusal causes and replay owners (ARBORA-OPS-R009.2, R009.3)

| Requirement | Case | Required assertion |
|---|---|---|
| `ARBORA-OPS-R009.2` | Refusal causes are logged | `tests/api/test_refusal_causes.py::test_refusal_cause_is_logged_with_request_id` observes the logged cause and the redacted envelope. |
| `ARBORA-OPS-R009.3` | Declared replay owners | `tests/api/test_refusal_causes.py::test_ops_modules_declare_replay_owners` finds `agents.run` and a test module's declaration. |
| `ARBORA-OPS-R009.4` | Typed refusal details | `tests/api/test_typed_refusal_details.py`; `tests/fleet/test_relay_session_recovery.py::test_a_prompt_called_without_its_arguments_is_a_typed_refusal`, `::test_declared_required_prompt_arguments_are_checked_before_the_child`. |

### ARBORA-OPS-R001 installed-wheel fixture acceptance

Copy all boot helpers into the isolated wheel test directory. Start the copied model helper, wait for readiness, and check a real HTTP completion. A missing helper must fail with its subprocess diagnostic before Arbora dispatch. Keep incompatible or absent published TUI dependencies visible as a qualification blocker, not a passing end-to-end result.

### ARBORA-OPS-R001 published TUI acceptance

Install the TUI from the locked test extra. Run `test_the_tui_headless_prompt_completes` with the installed console entry point. Require the expected model reply from the authenticated local stack. The installed-wheel job must report zero skips.


Test the actual serving-root binding and contextual read path. Cover concurrent callers, owner/session/tenant/graph mismatch, and stale credentials. Test missing kg:read, native auth refusal, absent generated methods, and null or malformed snapshots. Preserve future and expiry rejection. Source doubles establish mapping and admission only; no unpublished package substitutes for installed acceptance.

## ARBORA-OPS-R068 acceptance

| Case | Row | Proving test and observation |
|---|---|---|
| Component reads | `ARBORA-OPS-R068.1` | `tests/api/test_harness_components.py::test_components_list_and_get_served`: A served round trip returns versions with their digests and valid intervals. |
| Lesson query | `ARBORA-OPS-R068.2` | `tests/api/test_harness_lessons.py::test_lessons_retrieve_ranks_by_confidence`: The answer order follows the engine confidence ranking. |
| Evolution report | `ARBORA-OPS-R068.3` | `tests/api/test_harness_evolution.py::test_evolution_report_denial_hides_both`: The denied caller sees the refusal and nothing else. |
| Adoption | `ARBORA-OPS-R068.4` | `tests/api/test_harness_evolution.py::test_adopt_refuses_human_token`: A human-scoped caller cannot invoke the adoption. |

## ARBORA-OPS-R848 to R851 acceptance

| Case | Row | Proving test and observation |
|---|---|---|
| Source reads | `ARBORA-OPS-R848.1` | `tests/api/test_source_ops.py::test_source_reads_served`: tree, blob, history and blame return the engine content under the caller's path ACL. `tests/mcp_server/test_source_reader.py` proves the admitted engine reads, the per-method scope pre-checks and the projection limits. |
| Source writes | `ARBORA-OPS-R848.2` | `tests/api/test_source_ops.py::test_commit_land_revert_admitted`: commit, land and revert admit per principal. A destructive call without an approved plan refuses. |
| Source diff reads | `ARBORA-OPS-R848.3` | `tests/api/test_source_ops.py::test_source_diff_served`: `source.diff` returns the engine diff between two refs under the caller's path ACL. |
| Landing policy | `ARBORA-OPS-R849` | `tests/api/test_source_landing.py::test_fast_gate_refuses_before_land`: a failing fast gate refuses before landing with zero effects. A post-landing failure records the engine bisect and revert decision. |
| Virtual checkout | `ARBORA-OPS-R850` | `tests/api/test_source_checkout.py::test_checkout_stream_resumable`: directories stream before blobs. A resumed fetch continues from the last acknowledged entry. |
| Git bridge | `ARBORA-OPS-R851` | `tests/api/test_source_git_bridge.py::test_smart_http_clone_and_fetch`: clone and fetch succeed over the smart-HTTP endpoint. Mirror sync is served by the repository-manager connector. |

## Graph-driven development, context, governance and row-engine operations

| Row | Proving test |
|---|---|
| `ARBORA-OPS-R848.4` | `tests/api/test_source_ops.py::test_edit_symbol_admitted_and_refusals_pass_through` |
| `ARBORA-OPS-R852` | See each child. |
| `ARBORA-OPS-R852.1` | `tests/api/test_harness_promotion_ops.py::test_registry_lists_three_operations` |
| `ARBORA-OPS-R852.2` | `tests/api/test_harness_promotion_ops.py::test_promote_rollback_round_trip` |
| `ARBORA-OPS-R853` | See each child. |
| `ARBORA-OPS-R853.1` | `tests/api/test_lifecycle_ops.py::test_registry_lists_lifecycle_operations` |
| `ARBORA-OPS-R853.2` | `tests/api/test_lifecycle_ops.py::test_defect_to_deployment_trace` |
| `ARBORA-OPS-R854` | See each child. |
| `ARBORA-OPS-R854.1` | `tests/api/test_retrieval_ops.py::test_feedback_and_scope_operations_listed` |
| `ARBORA-OPS-R854.2` | `tests/api/test_retrieval_ops.py::test_scope_and_feedback_served` |
| `ARBORA-OPS-R855` | See each child. |
| `ARBORA-OPS-R855.1` | `tests/api/test_resolve.py::test_catalog_class_ask_routes_to_the_engine_planner_ahead_of_ranking` |
| `ARBORA-OPS-R855.2` | `tests/api/test_governance_ops.py::test_term_review_and_search` |
| `ARBORA-OPS-R856` | See each child. |
| `ARBORA-OPS-R856.1` | `tests/api/test_governance_ops.py::test_registry_lists_quality_operations` |
| `ARBORA-OPS-R856.2` | `tests/api/test_governance_ops.py::test_quality_incident_served` |
| `ARBORA-OPS-R857` | See each child. |
| `ARBORA-OPS-R857.1` | `tests/api/test_sparql_protocol.py::test_routes_registered` |
| `ARBORA-OPS-R857.2` | `tests/api/test_sparql_protocol.py::test_protocol_query_and_update` |
| `ARBORA-OPS-R858` | See each child. |
| `ARBORA-OPS-R858.1` | `tests/api/test_wire_access_ops.py::test_registry_lists_wire_access_operations` |
| `ARBORA-OPS-R858.2` | `tests/api/test_wire_access_ops.py::test_grant_read_revoke` |

## Trust flags, curation review and query workspace operations

| Row | Proving test |
|---|---|
| `ARBORA-OPS-R855.3` | `tests/api/test_governance_ops.py::test_trust_flag_and_proposal_review` |
| `ARBORA-OPS-R855.4` | `tests/api/test_resolve.py::test_catalog_class_ask_routes_to_the_engine_planner_ahead_of_ranking` |
| `ARBORA-OPS-R855.5` | `tests/api/test_resolve.py::test_find_for_catalog_assets_points_at_the_graph_query_ops_first` |
| `ARBORA-OPS-R855.6` | `tests/api/test_resolve.py::test_catalog_question_is_not_routed_without_a_visible_nl_planner` |

## ARBORA-OPS-R860 acceptance

| Case | Row | Proving test and observation |
|---|---|---|
| Model | `ARBORA-OPS-R860.1` | A unit test round-trips each params model. |
| Behavior | `ARBORA-OPS-R860.2` | Tests assert the engine calls and no local verdict. |
| Wiring | `ARBORA-OPS-R860.3` | A served test reaches each handler through `invoke`. |
| Refusal | `ARBORA-OPS-R860.4` | A served test asserts each code and no engine commit. |
| Evidence | `ARBORA-OPS-R860.5` | A test reads the audit event. |
| Hot-path proof | `ARBORA-OPS-R860.6` | `tests/api/test_program_ops_served.py::test_optimize_promote_rollback_served` passes. |

Hot-path proof for `ARBORA-OPS-R860`: `tests/api/test_program_ops_served.py::test_optimize_promote_rollback_served`.
## Durable public run replay (ARBORA-OPS-R021.4.7)

`tests/api/test_run_replay.py` verifies the shared HTTP/MCP invocation path, owner-first reads, scope and permission refusals. It also checks child task identity, public-only payloads, exclusive cursors, duplicate IDs, terminal continuation and status mapping.
Native EG qualification must exercise the exact property-based `ORDER BY` and
parameter-bound `LIMIT` query against out-of-order durable RunEvent writes.

Cursor regression cases include an earlier terminal, gap and duplicate identity
before the requested anchor. The census bound is explicit, and served HTTP/MCP
checks preserve ACCESS_DENIED from owner lookup, metadata census and payload reads.

## ARBORA-OPS-R861 acceptance

| Check | Row | Proof |
|---|---|---|
| Model | `ARBORA-OPS-R861.1` | A unit test round-trips the document and rejects an unknown field. |
| Save | `ARBORA-OPS-R861.2` | A served test saves, updates, then replays a stale revision and sees the conflict. |
| Refusal | `ARBORA-OPS-R861.3` | Tests assert each refusal, no engine write, and zero query executions. |
| Read | `ARBORA-OPS-R861.4` | A served test lists owned and shared entries and gets one by id. |
| Preview | `ARBORA-OPS-R861.5` | A served test asserts the preview, `APPROVAL_REQUIRED` without a plan, and the effect after approval. |
| Share | `ARBORA-OPS-R861.6` | A served test grants, revokes and refuses a reader caller. |
| Evidence | `ARBORA-OPS-R861.7` | Tests assert cross-tenant isolation, non-disclosure and the audit event. |
| Wiring | `ARBORA-OPS-R861.8` | A served test reaches each handler through `invoke` over HTTP and MCP. |
| Hot-path proof | `ARBORA-OPS-R861.9` | `tests/api/test_dashboard_ops_served.py::test_save_share_list_conflict_delete_served` passes. |

Hot-path proof for `ARBORA-OPS-R861`: `tests/api/test_dashboard_ops_served.py::test_save_share_list_conflict_delete_served`.

## Shared execution explorer acceptance (pending)

- `ARBORA-OPS-R021.4.8`: preserve replay v1 fixtures and decoder refusals unchanged. Unsupported projection versions refuse explicitly. Use a root with two independent steps and a join, then a declared child/grandchild run. Assert stable keys despite repeated local names and distinct spawn/dependency edges. Compare cursor-aligned snapshot/replay; test duplicates, gaps, pagination, missing lineage, bounds and denied owners before hydration. The flat fixture does not prove recursive production.
- `ARBORA-OPS-R021.4.9`: read a permitted public summary, redacted tool event and separately authorized transcript. Assert missing transcript is unavailable, never empty success. Forge each owner/node/context binding; no payload leaks. Verify all interaction flags remain false for trace-only nodes and equivalent MCP/HTTP refusals.

### Flat execution snapshot source qualification

`tests/api/test_execution_graph.py` targets R021.4.8.1. It exercises the registered
read through HTTP and MCP. It checks canonical owner/run admission before protected reads
and plan binding rechecks. It also covers schema negotiation, edge direction,
run-qualified identities, private-field omission and bounded event evidence.
It preserves the existing replay v1 cases in `tests/api/test_run_replay.py`.
These tests use injected native responses and do not qualify a native service,
spawned children, transcript access, interaction or the full parent requirement.

### Trusted scaffold configuration (ARBORA-OPS-R021.4.5.1.2)

The existing operator-owned task-plan configuration accepts only
`plan_scaffold: "on"` or `plan_scaffold: "off"`; omission means off.
Client metadata and model proposals cannot enable scaffolds.
Tests run the actual AU callback and update_plan tool for trusted on.
Trusted off must leave the publisher unused. Other harness settings remain unchanged.
Real native acceptance must observe the initial scaffold and tool-driven revisions.
It must retain the executable TaskPlan and exact owner/fence/expiry binding.
Record disabled-mode results separately; revision zero alone does not prove scaffold publication.

R021.4.8.2 retains the installed root-child-grandchild and snapshot/update
reconciliation matrix above. No test in the flat slice claims this acceptance.

R026.2 tests in `tests/api/test_gen_api.py` cover boolean input narrowing and
result widening, empty-schema equivalence and nested properties. They preserve
the dictionary comparison policy and provider-digest review. An installed
provider regression compares its unconstrained PlanMatViewGet result with itself.

The R021.4.8.1 transition matrix covers all six step states against five task events.
It checks each allowed transition and exact refusal through the public projection.
Unknown and cyclic predecessor tests retain their fail-closed expectations.

### Installed native read boundary (2026-10-25 qualification extension)

Extend the existing Linux installed-wheel real-boot case, not a second host.
After a native WorkItem claim and checklist publication, call the actual HTTP
and MCP `runs.current_plan`, `runs.get`, `runs.events` and
`runs.execution_graph` registrations. Use controlled durable public-event
fixtures, explicitly not installed AU producer evidence. Compare transport
projections, reconnect with a fresh MCP client, and resume the exclusive event
cursor. A separately signed foreign-tenant credential must expose neither the
checklist nor replay. Only explicit unavailable results, known authority refusal
codes, or HTTP/MCP authentication 401/403 qualify. Backend errors and transport
failures never count as tenant-isolation evidence. Source tests exercise these
guards, not the native service. The fixture admits only one actor, so this case
does not claim same-tenant different-owner isolation.
No graph override may hide default serving-root routing.

Without an admitted executable TaskPlan, the flat graph must remain unavailable.
This case does not qualify positive flat topology or native task-plan production.
It excludes process restart, nested lineage, UI stale-state clearing and central continuation.
ARBORA-OPS-R021.4.8.2 remains open. Runtime evidence requires a hosted run of the installed candidate
with the exact locked published dependencies.

### Native current-plan owner routing (ARBORA-OPS-R021.4.5.1.1)

The served reader verifies the caller before resolving native admission.
It calls owner-bound GetRunWorkItem on the host's WorkItem graph with unchanged caller claims.
It validates the immutable run, tenant, subject and graph before reading plan payloads.
Missing or malformed admission yields unavailable. A foreign owner or tenant cannot expose a snapshot.
Native authorization refusals retain their codes. Neither request fields nor mutable metadata may select another graph.
Tests reject forged graph fields, identity drift and added scopes.
The failing real-boot assertion remains unchanged until the hosted serving path passes.

## Medulla companion rows (2026-10-28)

- `ARBORA-OPS-R069`: Future tests cover the acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-OPS-R070`: Future tests cover the acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-OPS-R071`: Future tests cover the acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-OPS-R072`: Future tests cover the acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-OPS-R073`: Future tests cover the acceptance and refusals in requirements.md. Tests have not run.

## Participant memory, operator hooks and assistant profiles

- `ARBORA-OPS-R009.6`: Test: `tests/api/test_operator_hooks.py::test_bootstrap_refuses_absent_write_or_recursive_hook` (module test). Test: `tests/api/test_operator_hooks.py::test_hooks_load_in_declared_order` (module test). Hot-path proof: `ARBORA-OPS-R009.9`.
- `ARBORA-OPS-R009.7`: Test: `tests/api/test_operator_hooks.py::test_input_hook_allow_redact_and_reject` (served: HTTP and MCP through the registry `invoke`) asserts the three decisions and the order of two hooks.
- `ARBORA-OPS-R009.8`: Test: `tests/api/test_operator_hooks.py::test_output_hook_redacts_and_rejects_and_replay_matches` (served: HTTP and MCP) asserts the replayed response equals the hooked response.
- `ARBORA-OPS-R009.9`: Test: `tests/api/test_operator_hooks.py::test_timeout_error_and_unknown_decision_reject_with_hook_failed` (served) and `::test_decisions_are_recorded_without_matched_text` (served). Hot-path proof: `tests/api/test_operator_hooks.py::test_two_hooks_through_http_and_mcp` enters through the HTTP route and the MCP tool.
- `ARBORA-OPS-R024.6`: Test: `tests/api/test_participant_ops_served.py::test_derivation_status_reaches_the_engine` (served: HTTP and MCP, loopback engine).
- `ARBORA-OPS-R897`: Satisfied only when every child is satisfied. Root journey: `tests/api/test_participant_ops_served.py::test_participant_memory_journey` (served: HTTP and MCP).
- `ARBORA-OPS-R897.1`: Test: `tests/api/test_participant_models.py::test_models_round_trip_engine_vectors` (module test). Test: `tests/api/test_memory_ops.py::test_memory_ops_still_round_trip_after_send_extraction` (served).
- `ARBORA-OPS-R897.2`: Test: `tests/api/test_participant_ops_served.py::test_participant_put_and_member_add_round_trip` (served: HTTP and MCP) asserts replay by idempotency key and both refusal codes.
- `ARBORA-OPS-R897.3`: Test: `tests/api/test_participant_ops_served.py::test_scope_and_observation_round_trip_with_refusals` (served).
- `ARBORA-OPS-R897.4`: Test: `tests/api/test_participant_ops_served.py::test_turn_append_commits_and_enqueues` (served, loopback engine) and `::test_backlog_refusal_keeps_the_turn`.
- `ARBORA-OPS-R897.5`: Test: `tests/api/test_participant_ops_served.py::test_ask_and_card_return_citations_or_typed_refusals` (served).
- `ARBORA-OPS-R897.6`: Test: `tests/api/test_assistant_ops_served.py::test_profile_and_document_set_round_trip_and_run_reference` (served).
- `ARBORA-OPS-R897.7`: Test: `tests/api/test_participant_registration.py::test_registry_lists_every_operation_and_scope_is_registered` (module test). Test: `tests/api/test_participant_registration.py::test_engine_codes_map_to_status` (module test).
- `ARBORA-OPS-R897.8`: Test: `tests/api/test_participant_ops_served.py::test_participant_memory_journey` (served: HTTP and MCP).


## Row proofs

Each entry below names a row whose proof is the verification text of that row in [requirements.md](requirements.md).

| Row | Proof |
|---|---|
| `ARBORA-OPS-R005` | A contract test fails closed when the packaged engine error contract is absent or mismatched against the generated bindings. |
| `ARBORA-OPS-R017.2` | Test: `tests/api/test_finance_served.py::test_paper_action_requires_account_ownership` (L3) runs HTTP and MCP through `invoke`, with a loopback medulla engine. It asserts the owner succeeds once. It asserts another tenant gets `SUBJECT_ACCESS_DENIED` and a controlled provider records zero calls. |
| `ARBORA-OPS-R018.1` | Test: `tests/api/test_query_ops.py::test_ontology_schema_graph_and_list_bind_engine_methods` (L1). Test: `tests/api/test_query_served.py::test_sparql_and_schema_routes_reach_engine` (L3) runs HTTP and MCP through `invoke`, with a loopback medulla engine. |
| `ARBORA-OPS-R018.2.2` | Test: `tests/api/test_query_served.py::test_over_ceiling_budget_reaches_engine_unchanged` (L3) asserts the engine received the caller budget and the response shows the applied one. Test: `tests/api/test_query_served.py::test_unavailable_planner_refuses_without_fallback` (L3). |
| `ARBORA-OPS-R019.1` | Test: `tests/api/test_federation_ops.py::test_adapter_calls_engine_and_redacts` (L1). Test: `tests/api/test_federation_served.py::test_register_list_get_probe_redact_credentials` (L3) runs HTTP and MCP through `invoke`, with a loopback medulla engine. |
| `ARBORA-OPS-R019.2` | Test: `tests/api/test_federation_served.py::test_only_owner_shares_and_revocation_blocks_probe` (L3) runs HTTP and MCP through `invoke`, with a loopback medulla engine. It asserts the stranger never sees the source. |
| `ARBORA-OPS-R020.4` | Test: `tests/api/test_ingest_drift_embedding_ops.py::test_drift_ops_call_runner_with_tenant` (L1). Test: `tests/api/test_ingest_served.py::test_drift_repair_previews_then_applies_with_approval` (L3) runs HTTP and MCP through `invoke`, with a loopback medulla engine. |
| `ARBORA-OPS-R020.5` | Test: `tests/api/test_ingest_drift_embedding_ops.py::test_embedding_ops_call_runner_and_return_job_state` (L1). Test: `tests/api/test_ingest_served.py::test_reembed_creates_one_durable_job_per_key` (L3) runs HTTP and MCP through `invoke`, with a loopback medulla engine. |
| `ARBORA-OPS-R021.1` | Test: `tests/api/test_decide_ops.py::test_commit_calls_engine_client_not_runner` (L1). Test: `tests/api/test_decide_served.py::test_human_cannot_commit_and_result_has_provenance` (L3) runs HTTP and MCP through `invoke`, with a loopback medulla engine. |
| `ARBORA-OPS-R022.1` | A test confirms a work-item write requires the declared scope and that offers are visible only to their bound principal. |
| `ARBORA-OPS-R022.2.1` | Test: `tests/api/test_evolution_ops.py::test_status_reaches_runner_with_tenant_and_declares_loops_read` (L1). It is verified by the root slice test `tests/api/test_evolution_served.py::test_scopes_idempotent_run_and_pause`. |
| `ARBORA-OPS-R022.2.2` | Test: `tests/api/test_evolution_ops.py::test_schedules_list_reaches_runner_with_tenant_and_fails_closed` (L1). It is verified by the root slice test `tests/api/test_evolution_served.py::test_scopes_idempotent_run_and_pause`. |
| `ARBORA-OPS-R022.2.3` | Test: `tests/api/test_evolution_ops.py::test_proposals_list_reaches_runner_with_tenant_and_fails_closed` (L1). It is verified by the root slice test `tests/api/test_evolution_served.py::test_scopes_idempotent_run_and_pause`. |
| `ARBORA-OPS-R022.2.4` | Test: `tests/api/test_evolution_ops.py::test_run_requires_loops_control_and_key` (L1). Test: `tests/api/test_evolution_served.py::test_scopes_idempotent_run_and_pause` (L3) asserts one job for two calls with one key. |
| `ARBORA-OPS-R022.2.5` | Test: `tests/api/test_evolution_ops.py::test_pause_requires_loops_control_and_is_idempotent` (L1). It is verified by the root slice test `tests/api/test_evolution_served.py::test_scopes_idempotent_run_and_pause`. |
| `ARBORA-OPS-R024.3.1` | Test: `tests/api/test_usage_served.py::test_session_usage_forwards_caller_and_keeps_partial_coverage` (L3). It asserts forwarding, scope refusal, foreign-context refusal and preserved partial coverage. |
| `ARBORA-OPS-R024.3.2` | Test: `tests/api/test_usage_served.py::test_timing_is_validated_and_correlated_to_turn_receipts` (L3). It asserts correlation, the range bounds, a missing receipt reference and absence of derived rate fields. |
| `ARBORA-OPS-R024.5` | Test: `tests/api/test_ops_admin_ops.py::test_backup_restore_require_step_up_and_audit` (L3). It asserts the plain-confirmation refusal, the step-up success and the audit fields. |
| `ARBORA-OPS-R032.2.2` | Test: `tests/mcp_server/test_browser_composition.py::test_mcp_server_refuses_eg_browser_without_factory` (L3). It asserts the startup refusal and that a factory-bearing start succeeds. |
| `ARBORA-OPS-R032.2.3.2.2` | Test: `tests/mcp_server/test_browser_composition.py::test_start_call_receives_the_root_boundary` (L3). It asserts the object passed to the supervisor is the root's boundary. |
| `ARBORA-OPS-R032.2.4` | Test: `tests/mcp_server/test_browser_composition.py::test_one_runtime_reaches_mcp_server_and_webui` (L3). |
| `ARBORA-OPS-R035.2` | Test: `tests/api/test_resident_tool_surface.py::test_running_server_lists_exactly_the_resident_tools` (L3). It boots the server and lists tools over MCP, then asserts the middleware denies nothing. |
| `ARBORA-OPS-R039.1` | Test: `tests/gateway/test_dashboard_route_census.py::test_dashboard_router_exposes_every_route` (L2). It fails when a pair is removed or added without editing the pinned set. |
| `ARBORA-OPS-R039.2` | Test: `tests/gateway/test_dashboard_api.py::test_write_routes_refuse_read_only_callers` (L3). It asserts 403 on four routes for a read caller and success for an API-key caller. |
| `ARBORA-OPS-R039.4` | Test: `tests/gateway/test_dashboard_api.py::test_malformed_source_is_422_and_unknown_service_is_404` (L3). It asserts both codes and zero engine calls on the 422. |
| `ARBORA-OPS-R039.5` | Test: `tests/gateway/test_dashboard_api.py::test_daemon_status_key_sets_are_pinned` (L2). It fails when any of the five keys is dropped. |
| `ARBORA-OPS-R039.6` | Test: `tests/gateway/test_dashboard_api.py::test_daemon_shards_reads_engine_snapshot_and_refuses_when_absent` (L3). It asserts the snapshot body, both refusals and no import of `knowledge_graph` modules. |
| `ARBORA-OPS-R039.7` | Test: `tests/gateway/test_dashboard_api.py::test_hydration_routes_call_served_ingest_and_keep_refusal_codes` (L3). It asserts the job identifier, the three refusal codes and the status shape. |
| `ARBORA-OPS-R039.9` | tests/gateway/test_dashboard_api.py: 403 cases unchanged; an import census shows no Meristem-internal import. |
| `ARBORA-OPS-R040.1` | An meristem test imports each name from the public module. |
| `ARBORA-OPS-R040.2` | Test: `tests/gateway/test_artifacts_api.py::test_create_request_rejects_empty_template_and_oversize_data` (L1). Verified by the route test `test_create_and_refusal_cases`. |
| `ARBORA-OPS-R040.3` | Test: `tests/gateway/test_ports.py::test_artifact_ports_are_runtime_protocols` (L1). |
| `ARBORA-OPS-R040.4` | Test: `tests/gateway/test_artifacts_api.py::test_create_returns_id_and_rendered_and_refuses_bounded_json_error` (L3). It asserts 201, 400 and 422. |
| `ARBORA-OPS-R040.5` | Test: `tests/gateway/test_artifacts_api.py::test_get_returns_artifact_and_404_when_absent` (L3). |
| `ARBORA-OPS-R040.6` | Test: `tests/gateway/test_artifacts_api.py::test_refresh_inline_resolver_404_and_failure_preserves_render` (L3). |
| `ARBORA-OPS-R040.7` | Test: `tests/gateway/test_artifacts_route_mount.py::test_routes_mount_with_capability_dependencies` (L3). It asserts the three routes and the 403 cases. |
| `ARBORA-OPS-R040.8` | Test: `tests/gateway/test_artifacts_api.py::test_refresh_uses_the_exported_resolver_and_defaults_to_preserve` (L3). It uses a fake resolver and the default. |
| `ARBORA-OPS-R040.9` | The meristem row exists and ARBORA-OPS-R040.1 cites its ID. |
| `ARBORA-OPS-R040.10` | Test: `tests/gateway/test_artifacts_route_mount.py::test_capability_matrix_on_artifact_routes` (L3). It asserts the 403 cases and the API-key pass. |
| `ARBORA-OPS-R042.1` | Test: `tests/api/test_content_screening.py::test_closed_value_sets_and_opspec_origin` (L1). Verified by the root slice test. |
| `ARBORA-OPS-R042.2` | Test: `tests/api/test_content_screening.py::test_each_attack_class_and_masked_secret` (L1). It plants one case per class and one secret per pattern. |
| `ARBORA-OPS-R042.3` | Test: `tests/api/test_content_screening.py::test_invoke_calls_screen_then_redactor_in_order` (L3). It records call order through the served route. |
| `ARBORA-OPS-R042.4` | Test: `tests/api/test_content_screening.py::test_quarantine_and_unscreened_memory_write_refuse_with_zero_effects` (L3). It asserts both codes and no memory write. |
| `ARBORA-OPS-R042.5` | Test: `tests/api/test_content_screening.py::test_audit_holds_fields_and_no_secret_text` (L3). It asserts the four fields and the absence of the planted secret. |
| `ARBORA-OPS-R042.6` | Test: `tests/gateway/test_content_screening_hot_path.py::test_served_route_quarantines_hidden_instructions_and_masks_secrets` (L3). |
| `ARBORA-OPS-R050.1` | Test: `tests/api/test_policy_models.py::test_models_validate_fields_and_closed_change_set` (L1). Verified by the root slice test `tests/api/test_policy_plan_guard_served.py::test_policy_create_then_violating_plan_is_refused`. |
| `ARBORA-OPS-R050.3` | Test: `tests/api/test_policy_wiring.py::test_served_policy_request_reaches_engine_port_and_absent_producer_refuses` (L3). It asserts the engine call and zero effects without a producer. |
| `ARBORA-OPS-R050.4` | Test: `tests/api/test_policy_refusal.py::test_three_refusals_have_zero_effects` (L3). It asserts each code and no plan run or policy write. |
| `ARBORA-OPS-R050.5` | Test: `tests/api/test_policy_evidence.py::test_audit_record_fields_for_change_and_refused_plan` (L3). |
| `ARBORA-OPS-R050.6` | Test: `tests/api/test_policy_plan_guard_served.py::test_policy_create_then_violating_plan_is_refused` (L3). |
| `ARBORA-OPS-R051.1` | Test: `tests/api/test_security_eval_models.py::test_models_validate_and_close_verdict_set` (L1). Verified by the root slice test. |
| `ARBORA-OPS-R051.2` | Test: `tests/api/test_security_eval_select.py::test_only_declared_catalog_entries_are_selected_and_cap_applies` (L2). It asserts selection comes from the engine answer and the cap holds. |
| `ARBORA-OPS-R051.3` | Test: `tests/api/test_security_eval_wiring.py::test_registry_lists_operation_and_binder_composes_ports` (L3). |
| `ARBORA-OPS-R051.4` | Test: `tests/api/test_security_eval_refusal.py::test_refusals_start_zero_probes` (L3). It asserts zero runner calls for each refusal. |
| `ARBORA-OPS-R051.5` | Test: `tests/api/test_security_eval_evidence.py::test_audit_record_fields` (L3). |
| `ARBORA-OPS-R051.6` | Test: `tests/api/test_security_evaluate_served.py::test_in_scope_target_yields_findings_and_out_of_scope_refuses` (L3). |
| `ARBORA-OPS-R052.1` | Test: `tests/api/test_projection_models.py::test_fields_are_validated_and_frozen` (L1). Verified by the root slice test. |
| `ARBORA-OPS-R052.2` | Test: `tests/api/test_projection_scope.py::test_exact_cover_and_partial_cover` (L2). |
| `ARBORA-OPS-R052.3` | Test: `tests/api/test_projection_wiring.py::test_registry_lists_operation_and_binder_composes_reader` (L3). |
| `ARBORA-OPS-R052.5` | Test: `tests/api/test_projection_evidence.py::test_operator_reads_withheld_count_and_denied_caller_cannot` (L3). |
| `ARBORA-OPS-R052.6` | Test: `tests/api/test_projection_scope_served.py::test_two_scopes_see_different_projection_sets` (L3). |
| `ARBORA-OPS-R053.1` | Test: `tests/api/test_interaction_space_models.py::test_models_validate_fields` (L1). Verified by the root slice test. |
| `ARBORA-OPS-R053.2` | Test: `tests/api/test_interaction_space_build.py::test_union_caps_and_directory_names` (L2). |
| `ARBORA-OPS-R053.3` | Test: `tests/api/test_interaction_space_wiring.py::test_served_request_reaches_composed_retriever_and_missing_producer_refuses` (L3). |
| `ARBORA-OPS-R053.4` | Test: `tests/api/test_interaction_space_refusal.py::test_three_refusals_run_zero_queries` (L3). |
| `ARBORA-OPS-R053.5` | Test: `tests/api/test_interaction_space_evidence.py::test_audit_record_fields` (L3). |
| `ARBORA-OPS-R053.6` | Test: `tests/api/test_retrieval_space_served.py::test_two_queries_yield_boundary_and_two_directories` (L3). |
| `ARBORA-OPS-R009.5` | Test: `tests/api/test_connector_approval_forward.py::test_destructive_call_carries_approval_ref` (L3) through the registry `invoke` over HTTP and MCP. |
| `ARBORA-OPS-R843.2` | Each module's disposition names an existing row or the harness; closes when the meristem importers are re-pointed. |
| `ARBORA-OPS-R054.1` | Test: `tests/api/test_serving_technique_served.py::test_missing_mismatched_or_regressing_evidence_refuses` (L3). It asserts each code and no commit. |
| `ARBORA-OPS-R054.2` | Test: `tests/api/test_serving_technique_served.py::test_activation_requires_technique_bound_evidence` (L3). It activates all five techniques and refuses one regressed receipt. |
| `ARBORA-OPS-R055.1` | Test: `tests/api/test_ops_admin_ops.py::test_process_proposal_is_proposal_only` (L3). It asserts the verdicts and zero process effects. |
| `ARBORA-OPS-R055.2` | Test: `tests/api/test_ops_admin_ops.py::test_process_remediation_requires_run_bound_approval` (L3). It asserts one application after approval and zero effects on mismatch. |
| `ARBORA-OPS-R059.1` | Test: `tests/api/test_evolution_metrics_served.py::test_models_round_trip_rust_vectors` (L1). |
| `ARBORA-OPS-R059.2` | Test: `tests/api/test_evolution_metrics_served.py::test_projection_forwards_caller_scope_and_keeps_partial_state` (L3). |
| `ARBORA-OPS-R059.3` | Test: `tests/api/test_evolution_metrics_served.py::test_denied_or_incomplete_requests_read_nothing` (L3). It asserts zero engine calls on the first two refusals. |
| `ARBORA-OPS-R059.4` | Test: `tests/api/test_evolution_metrics_served.py::test_dashboard_joins_proposal_to_trace_metrics` (L3). |
| `ARBORA-OPS-R060.1` | Test: `tests/api/test_sessions_served.py::test_models_round_trip_rust_vectors_and_bound_page_size` (L1). |
| `ARBORA-OPS-R060.2` | Test: `tests/api/test_sessions_served.py::test_manifest_and_discovery_status_name_every_declared_harness` (L3). |
| `ARBORA-OPS-R060.3` | Test: `tests/api/test_sessions_served.py::test_denied_sources_are_partial_without_metadata` (L3). |
| `ARBORA-OPS-R060.4` | Test: `tests/api/test_sessions_served.py::test_refusals_expose_no_cross_tenant_sessions` (L3). |
| `ARBORA-OPS-R060.5` | Test: `tests/api/test_sessions_served.py::test_browser_discovers_browses_and_searches_every_harness` (L3). |
| `ARBORA-OPS-R061.1` | Test: `tests/api/test_usage_index_served.py::test_models_round_trip_rust_vectors_and_unknown_stays_unknown` (L1). |
| `ARBORA-OPS-R061.2` | Test: `tests/api/test_usage_index_served.py::test_projection_is_engine_aggregate_without_local_arithmetic` (L3). |
| `ARBORA-OPS-R061.3` | Test: `tests/api/test_usage_index_served.py::test_registry_lists_operation_and_binder_composes_reader` (L3). |
| `ARBORA-OPS-R061.4` | Test: `tests/api/test_usage_index_served.py::test_denied_stale_and_unproven_latency_expose_no_rows` (L3). |
| `ARBORA-OPS-R061.5` | Test: `tests/api/test_usage_index_served.py::test_browser_query_reconciles_with_independent_ledger` (L3). It checks the four counters, rounding, minor units, watermark, cursor and unknown reasons. |
| `ARBORA-OPS-R062.1` | Test: `tests/api/test_graph_changes_served.py::test_models_round_trip_rust_vectors` (L1). |
| `ARBORA-OPS-R062.2` | Test: `tests/api/test_graph_changes_served.py::test_stream_orders_events_and_reauthorizes_on_reconnect` (L3). |
| `ARBORA-OPS-R062.3` | Test: `tests/api/test_graph_changes_served.py::test_expired_cursor_returns_typed_recovery` (L3). |
| `ARBORA-OPS-R062.4` | Test: `tests/api/test_graph_changes_served.py::test_revocation_stops_delivery_and_limit_is_measured` (L3). |
| `ARBORA-OPS-R062.5` | Test: `tests/api/test_graph_changes_served.py::test_two_browsers_converge_under_gaps_reconnect_and_revocation` (L3). |
| `ARBORA-OPS-R063.1` | Test: `tests/api/test_retrieval_evaluation_served.py::test_models_round_trip_rust_vectors` (L1). |
| `ARBORA-OPS-R063.2` | Test: `tests/api/test_retrieval_evaluation_served.py::test_two_algorithms_match_independent_hits_at_k_ledger` (L3). |
| `ARBORA-OPS-R063.3` | Test: `tests/api/test_retrieval_evaluation_served.py::test_context_spans_slice_original_bytes_with_unicode` (L3). It uses composed and decomposed Unicode and multibyte text. |
| `ARBORA-OPS-R063.4` | Test: `tests/api/test_retrieval_evaluation_served.py::test_every_refusal_returns_before_any_result` (L3). |
| `ARBORA-OPS-R063.5` | Test: `tests/api/test_retrieval_evaluation_served.py::test_two_algorithms_match_independent_hits_at_k_ledger` (L3). |
| `ARBORA-OPS-R064.1` | Test: `tests/api/test_viewport_served.py::test_models_round_trip_rust_vectors` (L1). |
| `ARBORA-OPS-R064.2` | Test: `tests/api/test_viewport_served.py::test_limits_are_enforced_before_dispatch` (L3). |
| `ARBORA-OPS-R064.3` | Test: `tests/api/test_viewport_served.py::test_stale_tokens_and_bad_limits_return_no_usable_result` (L3). |
| `ARBORA-OPS-R064.4` | Test: `tests/api/test_viewport_served.py::test_scatter_and_line_interaction_over_large_source` (L3). |
| `ARBORA-OPS-R065.1` | Test: `tests/api/test_meetings_served.py::test_models_round_trip_vectors` (L1). |
| `ARBORA-OPS-R065.2` | Test: `tests/api/test_meetings_served.py::test_corrections_gaps_and_finalization_are_ordered` (L3). |
| `ARBORA-OPS-R065.3` | Test: `tests/api/test_meetings_served.py::test_each_local_and_hosted_route_records_provenance` (L3). |
| `ARBORA-OPS-R065.4` | Test: `tests/api/test_meetings_served.py::test_denied_egress_and_stale_revision_make_zero_provider_calls` (L3). |
| `ARBORA-OPS-R065.5` | Test: `tests/api/test_meetings_served.py::test_ordered_corrections_finalization_and_provider_routes` (L3). |
| `ARBORA-OPS-R066.1` | Test: `tests/api/test_bookmarks_served.py::test_models_round_trip_and_schema_digests_are_stable` (L1). |
| `ARBORA-OPS-R066.2` | Test: `tests/api/test_bookmarks_served.py::test_search_and_relationships_share_one_snapshot_scope` (L3). |
| `ARBORA-OPS-R066.3` | Test: `tests/api/test_bookmarks_served.py::test_csv_escapes_formula_cells_and_json_preserves_unknown_metadata` (L2). |
| `ARBORA-OPS-R066.4` | Test: `tests/api/test_bookmarks_served.py::test_refusals_return_before_any_data` (L3). |
| `ARBORA-OPS-R066.5` | Test: `tests/api/test_bookmarks_served.py::test_ingest_to_browser_search_relationships_and_export` (L3). |
| `ARBORA-OPS-R855.3.1` | `tests/api/test_trust_flag_notice.py::test_deprecation_event_notifies_reading_principals` (L3, loopback engine). |
| `ARBORA-OPS-R862` | Test: `tests/api/test_recommendation_gate_served.py::test_pass_surfaces_withheld_and_absent_block` (served, HTTP and MCP). It asserts one surfaced record and two blocked records. |
| `ARBORA-OPS-R863` | Test: `tests/api/test_decide_ops.py::test_approval_commits_once_and_scope_refusal_precedes_engine` (served). A reviewer outside scope makes zero engine calls. |
| `ARBORA-OPS-R864` | Test: `tests/api/test_retrieval_ops.py::test_bound_provider_only_and_zero_alternate_calls_on_refusal` (served). It varies provider and reasoning model independently. |
| `ARBORA-OPS-R865` | Test: `tests/api/test_write_back_served.py::test_approve_one_proposal_and_refuse_without_scope` (served). |
| `ARBORA-OPS-R866` | Test: `tests/fleet/test_tool_search_served.py::test_order_equals_sealed_record` (served over MCP). It asserts search order equals the record order and `UNAVAILABLE` on engine failure. Disclosure gate: `tests/fleet/test_tool_disclosure_gate_served.py::test_disclosed_tool_runs_and_undisclosed_tool_refuses_before_call` (served). The positive case runs a disclosed tool. The negative case asserts a refusal before the tool call. |
| `ARBORA-OPS-R867` | Test: `tests/test_autonomy_confirmation.py::test_irreversible_action_waits_for_approval` (served). The call is refused before approval and runs after it. |
| `ARBORA-OPS-R868` | Test: `tests/api/test_retrieval_ops.py::test_listing_toc_digest_equals_read_span_digest` (served). It asserts digest equality and the pass-through refusal. |
| `ARBORA-OPS-R869` | Test: `tests/api/test_generated_code_admission_gate.py::test_write_refuses_without_matching_admission` (served). It asserts both refusals and one admitted commit. |
| `ARBORA-OPS-R870` | Test: `tests/api/test_changeset_review_op.py::test_op_calls_decide_and_returns_the_record_digest` (served). |
| `ARBORA-OPS-R871` | Test: `tests/ops/test_playbook_step_requires_decision.py::test_action_refuses_without_a_committed_record` (served). It also runs one admitted step. |
| `ARBORA-OPS-R872` | Test: `tests/api/test_certification_gate_served.py::test_each_attestation_case_refuses_without_effect` (served). The exact current attestation admits the run. |
| `ARBORA-OPS-R873` | Test: `tests/control_plane/test_workflow_host_lazy_start.py::test_start_counter_zero_then_one` (served). Counter is 0 before first use, 1 after repeats, 0 after an unknown id. |
| `ARBORA-OPS-R874` | Test: `tests/control_plane/test_workflow_match_identity.py::test_entry_resolves_registered_record_and_unknown_refuses` (served). |
| `ARBORA-OPS-R875` | Test: `tests/api/test_skill_upgrade_gate_served.py::test_breaking_stops_and_compatible_and_migrated_dispatch` (served). |
| `ARBORA-OPS-R876` | Test: `tests/api/test_loops_continuation_served.py::test_same_continuation_after_host_restart` (served). The host restarts between two reads. |
| `ARBORA-OPS-R877` | Test: `tests/a2a/test_team_coordination_served.py::test_unavailable_starts_team_available_keeps_primary_and_status_survives_restart` (served). |
| `ARBORA-OPS-R878` | Test: `tests/api/ops/test_models_list.py::test_lists_only_visible_profiles_over_http_and_mcp` (L3) runs through the registry `invoke` and asserts a cross-tenant profile is absent. |
| `ARBORA-OPS-R879` | `tests/api/test_ops_hooks_served.py::test_hooks_install_uninstall_over_http_and_mcp` (L3). |
| `ARBORA-OPS-R880` | Test: `tests/api/test_query_federation.py::test_cross_source_report_served` (served HTTP and MCP, recorded engine). |
| `ARBORA-OPS-R881` | Test: `tests/api/test_artifact_ops_served.py::test_artifact_operations_serve_over_http_and_mcp` (L3). It creates, gets and refreshes an artifact and asserts each refusal. |
| `ARBORA-OPS-R882` | Test: `tests/api/test_agent_bus_served.py::test_send_receive_dispatch_over_medulla` runs `invoke` over HTTP and MCP with a loopback medulla engine. |
| `ARBORA-OPS-R883` | Test: `tests/api/test_retrieval_ops.py::test_agent_ref_is_taken_from_identity` calls the operations over HTTP and MCP against a recorded engine (served). |
| `ARBORA-OPS-R884` | Test: `tests/api/test_meetings_ops.py::test_summary_create_admits_context_and_commit` (L3) runs through the registry `invoke` with a loopback medulla and a recorded harness receipt. It asserts the denied-provider refusal makes zero medulla calls. |
| `ARBORA-OPS-R885` | A served test invokes the operation through the registry over HTTP and MCP with a loopback engine. It asserts the receipt digests. A self-approval refuses. |
| `ARBORA-OPS-R886` | Test: a served test through the operation registry's `invoke` over HTTP and MCP with a recorded medulla engine (L3). |
| `ARBORA-OPS-R887` | A served test calls the registry `invoke` over HTTP and MCP against a recorded medulla engine. It approves one proposal. It asserts the refusal passthrough for a missing approver. |
| `ARBORA-OPS-R888` | Test: `tests/api/test_governance_ops.py::test_access_request_approval_needs_steward` (L3) runs through the registry `invoke` over HTTP and MCP against a loopback medulla engine. It asserts the grant after a steward approval and zero effects after a non-steward approval. |
| `ARBORA-OPS-R889` | Test: `tests/api/test_lineage_route.py::test_run_event_reaches_engine_under_caller_tenant` posts one event and reads the edge through `ask`. |
| `ARBORA-OPS-R890` | Test: `tests/agent_host/test_engine_decision_port_served.py::test_port_calls_registered_ops_under_the_run_principal` (L3). A loopback engine records the principal, tenant and operation id for each of the four calls. |
| `ARBORA-OPS-R891` | Test: `tests/test_usage_forwarding.py::test_step_usage_events_append_once_per_run` (L3). A loopback engine records the principal, tenant and batches. It asserts the field set and the batch split. It also asserts that a refusal leaves the run result unchanged. |
| `ARBORA-OPS-R893` | Test: `tests/gateway/test_credential_authority.py::test_exact_grant_only_and_refusals_write_receipts` (L3) over MCP through the multiplexer. |
| `ARBORA-OPS-R894` | Test: `tests/agent_host/test_changeset_submitter_served.py::test_materialize_commit_land_and_conflict_through_invoke` (L3). A loopback engine returns one conflict then accepts the retry. |
| `ARBORA-OPS-R895` | Test: `tests/api/test_meeting_summarize_served.py::test_summary_needs_final_receipt_and_admitted_provider` (L3). It asserts zero provider calls before finalization and for each admission refusal. |
| `ARBORA-OPS-R896.1` | Test: `tests/api/test_development_surface_served.py::test_artifact_questions_reach_registered_queries` (L3: HTTP and MCP, loopback engine). It asserts one engine call per question and no new operation ID. |
| `ARBORA-OPS-R896.2` | Test: `tests/api/test_development_surface_served.py::test_evolving_mode_needs_operator_approval` (L3: HTTP and MCP). It asserts that no approval leaves the mode unchanged. |
| `ARBORA-OPS-R896.3` | Test: `tests/api/test_development_surface_served.py::test_run_start_returns_run_id_and_refuses_reference_mode` (L3: HTTP and MCP). |
| `ARBORA-OPS-R896.4` | Test: `tests/api/test_development_surface_served.py::test_run_status_reports_evidence_and_hides_other_tenants` (L3). |
| `ARBORA-OPS-R896.5` | Test: `tests/api/test_development_surface_served.py::test_accept_opens_a_pull_request_and_never_writes_specs` (L3: HTTP and MCP). |
| `ARBORA-OPS-R896.6` | Test: `tests/api/test_development_surface_served.py::test_artifact_graph_drives_a_run_over_mcp` (L4). |

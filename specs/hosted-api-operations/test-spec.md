# Hosted API and intent operations — test contract

## Acceptance tests

Governing [spec](spec.md).

| Test | Requirements | Setup and observation |
|---|---|---|
| HO-T01 | HO-01, HO-07 | From a pinned public engine wheel enumerate `is_wire_callable` methods. Compare with curated/generated op bindings and explicit live exclusions; fail on omissions, stale exclusions, duplicate ownership, or an unregistered Arbora route/tool. Regenerate artifacts twice and compare bytes/digest. |
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

R050/R051/R053 served fixtures cross shared invoke into public EG producer ports. They check engine verdict provenance and typed unavailability when a producer is absent. R051 uses EG-DECISION-ENGINE-R138 for scorer/verdict decisions and R133 for quotas/trace commits; AU only runs probes. R053 uses engine retrieval/context decisions and SDK source ingestion. Discovery fixtures use FLEET-R029 find/act/manage over R011/R013 registry machinery, with no retired fleet meta-tools or duplicate registry.

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

## Refusal causes and replay owners (ARBORA-OPS-R009.2, R009.3)

| Requirement | Case | Required assertion |
|---|---|---|
| `ARBORA-OPS-R009.2` | Refusal causes are logged | `tests/api/test_refusal_causes.py::test_refusal_cause_is_logged_with_request_id` observes the logged cause and the redacted envelope. |
| `ARBORA-OPS-R009.3` | Declared replay owners | `tests/api/test_refusal_causes.py::test_ops_modules_declare_replay_owners` finds `agents.run` and a test module's declaration. |

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
| `ARBORA-OPS-R855.1` | `tests/api/test_governance_ops.py::test_registry_lists_governance_operations` |
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

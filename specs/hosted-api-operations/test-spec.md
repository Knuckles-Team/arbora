# Hosted API and intent operations — test contract

Status: **READY FOR IMPLEMENTATION**. Delivery: **NOT ACCEPTED**. Governing [spec](spec.md).

| Test | Requirements | Setup and observation |
|---|---|---|
| HO-T01 | HO-01, HO-07 | From a pinned public engine wheel enumerate `is_wire_callable` methods. Compare with curated/generated op bindings and explicit live exclusions; fail on omissions, stale exclusions, duplicate ownership, or an unregistered GraphOS route/tool. Regenerate artifacts twice and compare bytes/digest. |
| HO-T02 | HO-02 | Table-drive human, service, delegated agent, tenants, exact scopes, and surfaces through `invoke`. Assert same allow/deny code on MCP, HTTP and A2A; extra fields and identity/tenant spoofing fail before a handler call. |
| HO-T03 | HO-02, HO-08 | A service op with domain scope but no caller subject read fails. Subject-read plus service grant succeeds and creates a caller-owned record. Revoke either permission before deferred dispatch and assert zero effect. Cross-tenant subject always fails. |
| HO-T04 | HO-03 | Exact idempotent write replays one receipt; a missing key fails. Fuzzy mutation returns preview and creates no effect. Destructive plan is single-use and rejects expired, changed params, actor, tenant, policy revision or registry digest. Console-only admin rejects MCP/A2A confirmation and accepts same-human fresh-MFA console confirmation. |
| HO-T05 | HO-03, GRAPHOS-OPS-R036 | Make durable audit reservation fail: handler must not run. After effect, simulate audit outcome-link failure: return `INDETERMINATE`, retain reservation, and reconcile once. Assert no false rollback and no raw parameter value in durable audit. |
| HO-T06 | HO-04 | Discover each fleet item kind. Load a native tool and prove `tools/list`, direct native call and `act fleet.call` reach identical `invoke` policy/audit. Revoke scope/PDP rule, assert next call denied, tool removed and notification sent/queued. Cap 64 and hard 256. Explicit LRU. Idle TTL. Fallback usable when client ignores list-change. |
| HO-T07 | HO-05 | Generated HTTP route and MCP verb for one read and one effect reach the same service mock. Bearer and cookie+CSRF variants. Cursor paging. Idempotency header. Protocol routes inventoried. Retired route absent only after generated consumer exists. |
| HO-T08 | HO-06 | Every published engine code maps without rewording; unknown engine code is a contract failure. Fleet code/target preserved. Error envelope is stable and privacy scrubbed. Registry digest appears on success/error, registry ETag, and MCP instructions. Stable-operation breaking diff fails compatibility check. |
| HO-T09 | HO-07 | Exercise one representative typed op from each listed domain through the generated client. Missing upstream dependency returns `UNAVAILABLE` and is absent from usable discovery. No domain handler directly opens an engine, agent or child transport outside its public port. |
| HO-T10 | HO-08 | Per identity mode and PDP state: policy-off retains scope filter. Policy-on outage fails closed. Admin scope never implies delegate. Policy revision invalidates loaded items. Destructive/admin decisions are uncached. Discovery sets match across MCP, HTTP and A2A for one principal. |
| HO-T11 | HO-09 | On clean public checkout, install pinned dependencies and run focused tests without private endpoints/sibling trees. In CI provision ephemeral local services, call served MCP and HTTP on the same request, and verify one durable result and audit pair. |
| HO-T12 | HO-10 | Generate the former-tool/route inventory; every name is mapped or has reviewed drop reason. Use the generated Python and TypeScript clients in a real caller test. Search published source for retired names and verify no active consumer uses an old route. |
| HO-T13 | HO-11 | Boot with one FastMCP loop and multiplexer. Enumerate tools/routes/methods and compare to registry plus explicit protocol list; remove any one resident registration in a fixture and assert startup fails. A loaded forwarder is the only runtime addition and its body calls `invoke`. |
| HO-T14 | GRAPHOS-OPS-R039 | Build the dashboard router. All 15 old method and path pairs present |
| HO-T15 | GRAPHOS-OPS-R039 | Read-only caller on mutating routes. 403; API-key caller passes; health open |
| HO-T16 | GRAPHOS-OPS-R040 | Create with empty template or oversize data. Typed validation or 400 refusal |
| HO-T17 | GRAPHOS-OPS-R040 | Create then get. Id and rendered output returned; same artifact read back |
| HO-T18 | GRAPHOS-OPS-R040 | Get unknown id. 404 |
| HO-T19 | GRAPHOS-OPS-R040 | Refresh with inline data, with resolver, and with a failing resolver. New render; resolver render; prior render preserved with ok false |
| HO-T20 | GRAPHOS-OPS-R040 | Mount on the host app; read-only caller POSTs. Routes present; 403 on POST |
| HO-T21 | GRAPHOS-OPS-R041 | Discover registry widgets. genius_agent absent |
| HO-T22 | GRAPHOS-OPS-R021.4.1 | Run `tests/api/test_runs_ops.py::test_run_trace_reader_is_runtime_checkable_protocol`; it must pass and bind to this row. |
| HO-T23 | GRAPHOS-OPS-R021.4.2 | Run `tests/api/test_runs_ops.py::test_adapter_calls_engine_wire_method`; it must pass and bind to this row. |
| HO-T24 | GRAPHOS-OPS-R021.4.3 | Run `tests/api/test_runs_ops.py::test_run_metrics_models_reject_unknown_fields`; it must pass and bind to this row. |
| HO-T25 | GRAPHOS-OPS-R021.4.4 | Run `tests/api/test_runs_ops.py::test_runs_metrics_passes_engine_values_through`; it must pass and bind to this row. |
| HO-T26 | GRAPHOS-OPS-R021.4.5 | Run `tests/api/test_runs_ops.py::test_runs_get_returns_termination_context_and_applied_config`; it must pass and bind to this row. |
| HO-T27 | GRAPHOS-OPS-R021.4.6 | Run `tests/api/test_runs_ops.py::test_runs_ops_registered_with_existing_scopes`; it must pass and bind to this row. |

## Drift and scanner acceptance

Run `uv run pytest tests/api tests/fleet tests/mcp_server tests/gateway`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy graph_os`, `uvx --from pre-commit==4.6.0 pre-commit run --all-files`, `uv build --wheel --out-dir dist`, and the repository's pinned scanner hooks. CCCC and KISS must stay within repository thresholds; jscpd adds zero clone pairs; Dupehound adds no new clones. Static checks reject transport calls outside the fleet executor, operation surfaces outside the registry, duplicate middleware authority, and frontend raw API bypass. Record exact command, tool versions, commit SHA, CI URL, and pass/fail in `evidence.md` before setting delivery to `ACCEPTED`; `evidence.md` is created when evidence exists. Never invent a pass from this spec.

## Spec-audit rollup tests

- GRAPHOS-OPS-R050: the hot-path proof test passes and the typed refusal leaves no effect.
- GRAPHOS-OPS-R051: the hot-path proof test passes and the typed refusal leaves no effect.
- GRAPHOS-OPS-R052.4/.5/.6: `tests/integration/test_projection_scope.py` holds visible projections fixed while varying only hidden projections. Caller-visible response contents, counts, refusal details and trace fields must remain identical. A denied caller cannot read withheld counts through any audit view; its operator-audit request refuses without indirect count leakage. A separately authorized operator can read the withheld count. The typed refusal leaves no effect.
- GRAPHOS-OPS-R053: the hot-path proof test passes and the typed refusal leaves no effect.

R050/R051/R053 served fixtures cross shared invoke into public EG producer ports. They check engine verdict provenance and typed unavailability when a producer is absent. R051 uses EG-DECISION-ENGINE-R138 for scorer/verdict decisions and R133 for quotas/trace commits; AU only runs probes. R053 uses engine retrieval/context decisions and SDK source ingestion. Discovery fixtures use FLEET-R029 find/act/manage over R011/R013 registry machinery, with no retired fleet meta-tools or duplicate registry.

## Planned served acceptance for run reads

All paths below are planned tests, not execution receipts. Each test must bind its exact requirement ID.
The fixture must enter REST and MCP, traverse authorization and registration, and reach an engine-backed reader.

| Requirement | Planned test path | Positive served requirement | Negative served requirement |
|---|---|---|---|
| GRAPHOS-OPS-R021.4.1 | `tests/api/test_runs_served.py::test_reader_binding` | Both operations reach the bound reader with caller authority. | Missing binding returns `UNAVAILABLE` with no engine call. |
| GRAPHOS-OPS-R021.4.2 | `tests/api/test_runs_served.py::test_validated_trace_readback` | An ingested trace and decision edge survive engine readback. | Invalid ingestion stores nothing; another tenant cannot reveal the trace. |
| GRAPHOS-OPS-R021.4.3 | `tests/api/test_runs_served.py::test_metrics_grouping_and_abstention` | Mixed solved and failed traces preserve engine cost and grouping. | Zero solved traces abstain; malformed numeric values refuse. |
| GRAPHOS-OPS-R021.4.4 | `tests/api/test_runs_served.py::test_metrics_authority` | Authorized filters reach the engine unchanged. | Denied scope makes no engine call; no local aggregate replaces failure. |
| GRAPHOS-OPS-R021.4.5 | `tests/api/test_runs_served.py::test_summary_and_plan_projection` | Fields map exactly; an authorized fresh plan preserves order and revision. | Missing plan export stays unavailable; wrong-run, expired and regressing snapshots reveal no items. |
| GRAPHOS-OPS-R021.4.6 | `tests/api/test_runs_served.py::test_registered_rest_mcp_parity` | Generated clients invoke both registered transports with equal results. | Absent operation and malformed envelopes refuse without dispatch. |

## Planned versioned memory served acceptance

Each case must bind its exact child ID and enter real HTTP and MCP serving.
Use disposable durable engine storage and authenticated callers; do not mock transport, authorization, the host or persistence.
These paths are planned acceptance, not passing test receipts.

| Requirement | Planned test | Positive served requirement | Negative served requirement |
|---|---|---|---|
| GRAPHOS-OPS-R024.4.1 | `tests/api/test_memory_files_served.py::test_models_and_port_preserve_tagged_outcomes` | Generated outcomes survive the actual HTTP/MCP adapter and engine readback. | Missing methods refuse; a refused result has no shared head or stored receipt. |
| GRAPHOS-OPS-R024.4.2 | `tests/api/test_memory_files_served.py::test_read_list_preserve_scope_and_cursor` | Authorized listing retains stable ordering; exact revision reads return matching bytes. | Wrong-scope or expired cursors disclose no metadata; denied read reveals no existence. |
| GRAPHOS-OPS-R024.4.3 | `tests/api/test_memory_files_served.py::test_competing_saves_preserve_both_versions` | Two clients save one base; the stale save retains both versions across restart. | Capacity failure retains nothing; stale resolution leaves the accepted head unchanged. |
| GRAPHOS-OPS-R024.4.4 | `tests/api/test_memory_files_served.py::test_materialization_ack_checks_generation` | A fresh acknowledgment permits EG in_sync only after successful materialization. | Observe a new edit without changing the head, then deliver an older acknowledgment; it must refuse. |
| GRAPHOS-OPS-R024.4.5 | `tests/api/test_memory_files_served.py::test_authority_and_replay_are_non_disclosing` | Authorized same-body replay returns one durable receipt and performs no duplicate effects. | Denied existing, absent and creation requests match; changed-body replay and revoked replay disclose no head. |
| GRAPHOS-OPS-R024.4.6 | `tests/api/test_memory_files_served.py::test_complete_served_memory_file_journey` | Real host, GraphOS and durable engine complete listing, conflict, resolution and acknowledgment. | Reject browser acknowledgment, symlink escape and unavailable producer; no mocked transport or persistence. |

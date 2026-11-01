# Verification contract

Every assertion is run against an exact commit and reports a pass/fail result. Fixture tests are required for a clean checkout and cloud PR; served release probes require separate evidence before completed acceptance. Do not turn an unavailable external environment into a blocking source hook.

## Positive and negative cases

| Area | Positive case | Negative case and required outcome |
|---|---|---|
| Registry | Every packaged wire-callable engine method has one op or a reviewed exclusion. Digest repeatable across two builds | Missing method, stale exclusion, schema or generated artifact drift fails a local gate |
| Resident MCP | Exactly six verbs and four fleet tools appear at boot with correct schemas. FastMCP 4 protocol clients call them | Missing registration fails startup. Retired granular/harvest tool cannot reappear |
| Discovery | Authorized tool, prompt, resource/template, skill, and connector item appear with correct body/schema and same result set across MCP/HTTP/A2A | Unauthorized, stale, duplicate, malformed, or digest-mismatched item is absent or rejects refresh; no partial catalog |
| Resolver | First example for each op ranks it top three; tenant and policy partition feedback | Cross-tenant reward/cache leak rejected; fuzzy match cannot execute a write |
| Load and lifecycle | Native schema matches child, session isolation, list-change and fallback work. One-hour idle expiration and cap behavior | 65th load without explicit eviction returns `LOAD_CAP_EXCEEDED`. Unknown native name denies. Evicted or expired name cannot call |
| Scopes and policy | Exact `mcp:discover`/`mcp:delegate` plus item scopes allow matching caller. Local principal has explicit scopes | `admin`/`kg:admin` alone, unscoped stdio, missing child scope, PDP denial or outage all deny. Policy off stays scope filtered |
| Revocation | A loaded call passes, then scope/policy revision changes. Next call rechecks and notification is queued | Revoked native forwarder, `fleet.call`, HTTP, and A2A must all deny with the same category. No stale-cache grace |
| Effects | READ executes. WRITE requires appropriate plan/idempotency. Destructive requires confirmation. Admin requires console step-up | Caller-supplied actor/tenant rejected. Service child without domain scope/subject read rejected. Replay key conflict rejected |
| Child identity | Distinct package, transport, and runtime MCP names resolve to verified runtime name | Duplicate runtime names and ambiguous aliases reject candidate; static service token cannot be used as caller token |
| Connector pins | Real `tools/list` schema fingerprint matches certified manifest and pack annotations | Empty placeholder schema, changed schema, or missing effect annotation cannot be certified or silently treated as READ |
| Reload | Candidate validates, publishes one generation atomically, sends ordered deltas, re-ingests changed bodies once | Malformed/duplicate/stale/deleted/digest-mismatch candidate retains last-known-good; in-flight drain timeout bounded; no mixed replica generation |
| Assembly/admission | Real generated model's complete `agents` list survives parsing. Capacity acquired all-or-nothing and released on stop | Dict fakes, partial capacity lease, second re-decision, dropped agent, or leaked lease fail |
| Catalog admission | The reader skips a bare registration. Joined servers serve. Status lists the skipped name | Receipt, revision, pin, content and page faults still abort the read |
| Fleet onboarding | The pass registers, attests and imports each configured server. The CLI reports each server | The pass reports a failing server, a registry outage or a missing credential. Other servers and boot continue |
| Lease renewal | A lease that lapses within six hours renews under a new window key. Self-served leases renew | The pass leaves a fresh lease alone. A refused catalog refresh keeps the prior catalog |
| Feeds | Tenant-bound telemetry/security/CI metadata enters catalog and audit through typed ops | Raw secret values, unsanitized identity/IP, cross-tenant events and live trading without write-back authorization reject |

## Authority parity oracle

Table-drive principal kind (human, delegated human, service), tenant, exact scopes, Eunomia mode and decision, effect class, item kind, and surface. Invoke an op through an MCP verb, resident fleet tool, loaded native forwarder, HTTP, and A2A. For equivalent operations assert the same allow/deny and stable error code, including unknown op, missing scope, principal mismatch, PDP down, confirmation required, step-up required, and child failure. Discovery views for each principal must produce the same authorized identities. Engine error codes are preserved verbatim in the envelope; fleet errors preserve the child code and mark the source.

The oracle must include a loaded tool followed by scope removal, policy revision, item deletion, and server identity change. The next call, even by native name without a new `tools/list`, must refuse. Verify audit fields are the same for native forwarder and `act op=fleet.call`; the audit stores only parameter digest, not secret values.

## Reload and served probe

Provision two local arbora replicas, one synthetic child MCP server, a fixture policy, and a disposable durable event store. Capture the callable reload op ID, prior/candidate/active generation IDs and digests, one shared trace ID, and timestamps. Change one tool schema, prompt body, resource/template body, and skill body. Refresh and prove the two replicas converge on the same complete generation. Each affected item has one ordered ingestion receipt, and sessions receive the matching notifications. Hold one call in flight during swap to prove drain behavior. Repeat with a malformed body, duplicate server identity, stale digest, deleted asset, and policy outage. Record last-known-good, error, and absence of partial publication. Restart one replica and replay receipts to prove durability and idempotency.

CI may run this probe with local containers/processes and synthetic credentials. A deployment-specific probe may additionally validate a real cluster, but its absence does not make unrelated cloud PR hooks fail. Completed acceptance requires the served probe to pass against landed code.

## Quality and contribution gates

- Run repository format/lint/type/unit suites, packaged contract regeneration in `--check` mode, and targeted MCP protocol tests from a clean checkout. Pin tools and fixtures in repository manifests; avoid host-only paths and ambient credentials.
- CCCC: new or changed functions meet the repository's cyclomatic/ABC limits. The target is at most 10; the absolute limit is 15. Split orchestration into small typed steps. KISS limits: at most 35 statements, 8 parameters, 20 locals, depth 5, 8 returns and 1 boolean parameter. A file has at most 900 lines and 40 functions, unless generated declarations need a documented different check. No skip, xfail, noqa, or type-ignore is used to hide a failure.
- jscpd: zero new duplicate pairs over merge base. Dupehound: zero new clones. Reuse the existing catalog reader, multiplexer, notification, and served composition paths rather than copying them.
- Static gates check no unregistered surface, no child transport call outside the fleet gateway, all native forwarders call `invoke`, no default-open unknown tool path, exact scope and service-grant registry, and generated artifact parity. Runtime gate checks policy mode matrix and authority parity.
- Fail a PR only for reproducible source, contract, or fixture failures. Record optional remote dependency or live-environment observations as release evidence, with a provisionable substitute for contributor CI.

## Evidence record template

| Commit | Case/gate | Command or fixture | Result | Environment | Timestamp | Receipt or trace |
|---|---|---|---|---|---|---|

No execution result is recorded in this template.

## ARBORA-FLEET-R030

`tests/fleet/test_fleet_access_contracts.py` uses a fake pack with one `ontology://` entry and a fake parser.
- Onboarding registers one source connection and one mapping per contract.
- Each registered mapping is unapproved, and the catalog serves no mapping for its class.
- A second registration adds no duplicate mapping.
- A missing SDK module skips contracts and raises nothing.
- A domain ontology with triple-quoted literals never reaches the parser. A separate contracts body still registers.
- A contracts body that the parser rejects logs a warning with its URI. Onboarding continues.

## Certified fleet execution acceptance

These integration tests have not been executed. Use the actual served intent operations and REST invoke route.
The plan defines fixtures, authority boundaries, matrix coverage and measured capacity thresholds.

| Requirement | Served proof |
|---|---|
| `ARBORA-FLEET-R034.7` | Typed fixtures reject missing bindings and dependency cycles. |
| `ARBORA-FLEET-R034.8` | A stale dependency or one denied tool prevents all workflow calls. |
| `ARBORA-FLEET-R034.9` | Served agents.run reaches the admission gate before AU execution. |
| `ARBORA-FLEET-R034.10` | Unknown tools, cyclic dependencies and revoked grants produce no new effects. |
| `ARBORA-FLEET-R034.11` | Independent trace reads prove atomic-before-composed ordering and exact tool binding. |
| `ARBORA-FLEET-R034.12` | Observe the configured model request and zero denied backend calls. |
| `ARBORA-FLEET-R024.4` | Typed nested request/result round trips preserve every allocation boundary. |
| `ARBORA-FLEET-R024.5` | Observe at least 200 concurrent instances and independent durable results under the declared budget. |
| `ARBORA-FLEET-R024.6` | The served operation reaches the approved SDK provisioner and returned-result consumer. |
| `ARBORA-FLEET-R024.7` | No denied allocation occurs; partial failures retain actual resource identities for cleanup. |
| `ARBORA-FLEET-R024.8` | Counters reconcile with independent runtime and durable result observations. |
| `ARBORA-FLEET-R024.9` | Mock allocations and smaller extrapolated runs cannot establish capacity. |
| `ARBORA-FLEET-R002.3` | Fixtures parse nested definitions and reject missing versions or unknown required fields. |
| `ARBORA-FLEET-R002.4` | Compatible updates publish atomically; incompatible evolution leaves the prior generation active. |
| `ARBORA-FLEET-R002.5` | Real served catalog probes supply both schema versions and verified producer receipts. |
| `ARBORA-FLEET-R002.6` | Missing or contradictory producer evidence never permits a generation swap. |
| `ARBORA-FLEET-R002.7` | Receipt reads identify exactly which definition each admitted workflow consumed. |
| `ARBORA-FLEET-R002.8` | MCP and REST expose the same complete admitted generation. |
| `ARBORA-FLEET-R027.1` | All phases retain one tenant, run identity and exact skill digest. |
| `ARBORA-FLEET-R027.2` | Independent graph and agent observations agree before a successful completion claim. |
| `ARBORA-FLEET-R027.3` | One served request traverses discovery, distribution, provisioning and ingested skill consumption. |
| `ARBORA-FLEET-R027.4` | Denied discovery returns no protected identity; no unauthorized agent or tool effect occurs. |
| `ARBORA-FLEET-R027.5` | The evidence proves graph identity consumption rather than matching copied skill text. |
| `ARBORA-FLEET-R027.6` | Separate helper tests cannot satisfy integrated acceptance. |
| `ARBORA-FLEET-R019.1` | The served integrated tests cover this consumer dependency. |
| `ARBORA-FLEET-R021.1` | The served integrated tests cover this consumer dependency. |
| `ARBORA-FLEET-R005.3` | The served integrated tests cover this consumer dependency. |

## Original parent acceptance coverage

These cases remain required in addition to the fleet execution extension.
Existing tests are reuse targets, not new evidence for these child IDs. The residual integration tests have not been executed for this change.

| Parent obligation | Child coverage | Existing test seam and required proof |
|---|---|---|
| R019 discover/load/call ordering and disabled filtering. | ARBORA-FLEET-R019.2 | Extend tests/api/test_policy_eunomia.py with served phase coverage and engine effect counters. |
| R019 three deployment defaults and off warning. | ARBORA-FLEET-R019.2 | Reuse test_profile_defaults; assert the deployment doctor warning for disabled policy. |
| R019 enabled denial and unreachable service. | ARBORA-FLEET-R019.2 | Reuse test_enabled_denial_and_unavailable_are_distinct and test_missing_pdp_fails_closed; exercise each served phase. |
| R021 exact scopes with no broad-admin substitute, including stdio. | ARBORA-FLEET-R021.2 | Serve both stdio and network clients; allow exact scopes and refuse each broader substitute. |
| R021 unknown native tool denial and active MCP policy middleware. | ARBORA-FLEET-R021.2 | Extend tests/mcp_server/test_graph_tool_surface.py; assert denied visibility and dispatch with middleware active. |
| R027 configured-server selection and fresh-store register/capture/attest/import. | ARBORA-FLEET-R027.7 | Reuse tests/fleet/test_fleet_onboarding.py config filtering and fresh-store multi-server cases. |
| R027 idempotency, admitted servers, per-server failure, registry outage and missing credential. | ARBORA-FLEET-R027.7 | Reuse the same onboarding test suite and assert healthy-server progress and no duplicate imports. |
| R027 complete CLI pass and non-zero exit on any failure. | ARBORA-FLEET-R027.7 | Reuse test_cli_runs_one_full_pass_and_fails_on_any_server through the actual command. |
| R027 background boot with non-blocking, non-fatal failure. | ARBORA-FLEET-R027.7 | Extend test_a_failing_pass_never_escapes_the_boot_thread with live serving and concurrent MCP requests. |

Record exact revisions and bound test IDs separately for these residual children.
A successful certification, workflow or provisioning test cannot substitute for original policy, scope or onboarding coverage.


## Repository source-effect served acceptance

These proposed tests live in tests/fleet/test_repository_source_effect_served.py. This definition change has not run them.
Each row includes positive and negative assertions; all original R003 and R003.1 through R003.3 acceptance remains required.

| Requirement | Proposed function | Required proof |
|---|---|---|
| ARBORA-FLEET-R003.4 | `test_original_fleet_restart` | Restart and fault each admitted writable profile. Observe original receipts without duplicate effects and no partial protocol state. Missing previews and mismatched keys must prevent writes. |
| ARBORA-FLEET-R003.5 | `test_request_binding` | Round-trip seeded byte fixtures through HTTP and MCP. Compare canonical projections and refuse each independently altered field. |
| ARBORA-FLEET-R003.6 | `test_trusted_composition` | A served request reaches the real admitted connector once. Unbound configuration and direct native bypass attempts reach it zero times. |
| ARBORA-FLEET-R003.7 | `test_commit_binding` | One exact approved preview commits. Each binding change, invalid lease and missing required step-up produces zero provider mutations. |
| ARBORA-FLEET-R003.8 | `test_actual_effect_readback` | Serve preview, approval and commit against a disposable provider. Independently read its revision and durable graph attempt. An echoed flag or unbound receipt must refuse success. |
| ARBORA-FLEET-R003.9 | `test_refusal_parity` | Observe zero denied mutations on both surfaces. Inject readback failure after an effect and observe uncertainty without another write. |
| ARBORA-FLEET-R003.10 | `test_restart_replay` | Race identical calls and crash at each effect boundary. Restart both processes and prove one effect or a truthful unresolved result. |


### Original parent coverage

| Original obligation | Retained coverage |
|---|---|
| Typed, previewed registry operation with no unguarded native child tool | R003.1, R003.2 and R003.4; verify actual served exposure and refused bypass |
| Preview/key matching and identical replayed receipt | R003.2 and R003.4; include generic writable profiles, not only repository files |
| Writable-fleet correctness across service restart and fault | R003.3 and R003.4; restart each admitted profile and observe durable results |

The original restart/fault acceptance includes absence of partial protocol state. Record uncertainty completely when an external effect is possible.
An uncertain result is not evidence that a provider write rolled back. Preserve original assertions; repository-only coverage is insufficient.

### Real served fixture and sequence

Start the actual Arbora serving composition, a durable graph service and an admitted SDK connector in separate processes.
Use a contributor-owned disposable provider repository and trusted fixture configuration; credentials stay outside recorded evidence.
Record exact commits, registered operations, schema/configuration digests, authority fixtures and invocation surfaces.
Imported classes, direct helper calls and in-memory receipt fakes cannot satisfy this served acceptance.
Missing producer exports, connector composition or qualified replay binding must produce unavailable outcomes, not synthetic success.

1. Form P from the canonical generated change set, full SDK intent and trusted connector-binding digest.
2. Call fleet.write_back.preview through HTTP and MCP. Observe SDK dry_run and exact digest-bound preview with zero provider mutations.
3. Prepare fleet.write_back.commit using identical P and the SDK key. Observe its existing commit-operation confirmation binding without mutation.
4. Record explicit approval of the canonical change set through existing authority and complete required confirmation/step-up.
   If approval changes the canonical request, repeat preview and preparation; no changed P can reuse the old binding.
5. Call commit with identical P, the commit plan and original SDK key. Observe the admitted SDK call and one conditional provider effect.
6. Independently read the returned immutable provider revision and path. Compare exact bytes, size and executable mode with approved intent.
7. Read the durable graph attempt through a separate client. Match its request, revision and observation digest with the SDK receipt.
8. Repeat and race identical requests. Restart both host and connector; recover the identical proven receipt with no additional effect.

Use SDK's exact byte vectors, including empty bytes, non-ASCII UTF-8, distinct CRLF/LF and embedded zero bytes.
Generate the 4096-byte vector with `payload[index] = (73*index + 19) mod 256`, index=0..4095.
Across processes and both surfaces, identical P must yield identical params_digest and SDK change-set/patch digests.
An identical durable replay must preserve the SDK receipt digest. Different actual effect revisions must not be hidden by digest equality.

### Negative and recovery matrix

Independently change connector, source instance, repository, target, path, immutable base, payload bytes/digest/size, executable mode and adapter digest.
Also change actor, tenant, approval, effect key, trusted binding, policy and registry revision.
Test unknown fields, sole-field patch/scope/provenance violations, missing approval/audit, expired requests and outer/inner key mismatches.
Use traversal, symlink and encoded path aliases, denied scopes, policy outage, wrong principal and failed subject-read checks.
Test missing step-up, expired plans and a preview-operation lease submitted to commit. Each pre-effect refusal causes zero provider mutations.
Race a provider base change after preflight. The SDK must reject at effect time without overwriting the competing revision.
A denied direct native child call must not reach the write-back producer. Read-only hydration never supplies approval.

Inject a failed or mismatched independent readback after a possible effect. Assert an indeterminate outcome and zero additional mutations.
Reject a response containing only an echoed key, applied flag, mutable revision or mismatched receipt identity/digest.
Preserve engine/fleet error provenance through the existing error envelope; HTTP and MCP must agree on refusal category and effect count.

Crash before plan consumption, after consumption before SDK dispatch, after provider effect, after readback and after durable receipt before response.
Restart both processes against the same authorities. Require a proven original effect, authoritative no-effect, or a truthful unresolved result.
After authoritative no-effect, only a still-authorized conditional retry may proceed; a changed base requires new approval.
A consumed lease, a missing receipt, or another writer's identical bytes must never trigger blind reapply.
Audit and receipt failures after mutation preserve uncertainty and known references rather than claiming rollback.
Record request, plan, audit and SDK receipt associations with actual provider-call counters and fresh durable reads.


## OpenAPI operation schema preflight (`ARBORA-FLEET-R035`)

Run `uv run --no-sync pytest tests/fleet/test_multiplexer_schema_disclosure.py` against the frozen Arbora lock and merged public SDK source. The fixture cases prove ordered reconstruction and compare both selected schemas before invocation. Stale manifests, missing fragments, absent output schema and mismatched action must refuse before the operation call. Changed catalog, denied session, OAuth-session mismatch and uncertain-write replay must also refuse before that call. These tests use fake child sessions and do not prove live provider publication, production credentials, deployment, or provider effects. Report focused consumer evidence separately from any later live/release evidence.

Cover non-prefixed OAuth aliases with stale disclosures and ordinary tools without schema metadata. Release preflight before cancelling its caller to prove the final-send cancellation fence. Cancel and refuse a half-open preflight, then prove a later healthy probe succeeds without replay. Compare CI's EG source revision with the lock and inspect the pinned SDK's public disclosure exports.

## Additional owner requirements

- `ARBORA-FLEET-R024.10`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-FLEET-R024.11`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-FLEET-R002.9`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-FLEET-R034.13`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-FLEET-R034.14`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.

## ARBORA-FLEET-R036 acceptance

| Case | Row | Proving test and observation |
|---|---|---|

| Model | `ARBORA-FLEET-R036.1` | `tests/fleet/test_connect_contract.py::test_contract_record_round_trips`: A round trip keeps every field and refuses a blank connector. |

| Behavior | `ARBORA-FLEET-R036.2` | `tests/fleet/test_connect_contract.py::test_only_changed_resources_import`: A second connect with one changed resource sends one import. |

| Wiring | `ARBORA-FLEET-R036.3` | `tests/fleet/test_connect_contract.py::test_list_changed_reaches_onboard`: A served connect and a `list_changed` event both reach `onboard`. |

| Refusal | `ARBORA-FLEET-R036.4` | `tests/fleet/test_connect_contract.py::test_contract_refusals`: Each case records its reason, and a rejected connector serves no tools. |

| Evidence | `ARBORA-FLEET-R036.5` | `tests/fleet/test_connect_contract.py::test_status_reports_contract`: The status read shows the digests and the engine receipts. |

| Hot-path proof | `ARBORA-FLEET-R036.6` | `tests/fleet/test_connect_contract_served.py::test_first_connect_and_list_changed_reimport_only_changed`: The first connect imports all resources, and the change imports only the seed graph. |


Hot-path proof for `ARBORA-FLEET-R036`: `tests/fleet/test_connect_contract_served.py::test_first_connect_and_list_changed_reimport_only_changed`.

## ARBORA-FLEET-R037 acceptance

| Case | Row | Proving test and observation |
|---|---|---|

| Model | `ARBORA-FLEET-R037.1` | `tests/fleet/test_job_loads.py::test_job_load_round_trips`: A round trip keeps every field and refuses an empty request. |

| Behavior | `ARBORA-FLEET-R037.2` | `tests/fleet/test_job_loads.py::test_loads_only_answered_tools`: A 300-tool fleet loads only the answered tools. |

| Wiring | `ARBORA-FLEET-R037.3` | `tests/fleet/test_job_loads.py::test_find_job_reaches_load_tools`: A served `find` with a job reaches `load_tools`. |

| Refusal | `ARBORA-FLEET-R037.4` | `tests/fleet/test_job_loads.py::test_job_load_refusals`: Each case loads no tool and returns its reason. |

| Evidence | `ARBORA-FLEET-R037.5` | `tests/fleet/test_job_loads.py::test_result_cites_decision`: Each loaded tool names its decision premise. |

| Hot-path proof | `ARBORA-FLEET-R037.6` | `tests/fleet/test_job_loads_served.py::test_find_job_lazily_loads_answered_tools`: The session lists only the answered search tools, and `tools/list_changed` fires once. |


Hot-path proof for `ARBORA-FLEET-R037`: `tests/fleet/test_job_loads_served.py::test_find_job_lazily_loads_answered_tools`.

## ARBORA-FLEET-R038 acceptance

| Case | Row | Proving test and observation |
|---|---|---|
| Evidence | `ARBORA-FLEET-R038` | `tests/fleet/test_protocol_roles.py::test_one_implementation_per_role`: An import-graph test fails when a arbora module implements a connector transport or connector server scaffolding. |

## ARBORA-FLEET-R029.1 acceptance

| Case | Row | Proving test and observation |
|---|---|---|
| Refusal | `ARBORA-FLEET-R029.1` | `tests/api/test_fleet_effect.py::test_unscoped_search_without_query_is_invalid_argument`: an empty unscoped search raises `INVALID_ARGUMENT` with `field: query`, mapped to HTTP 400, and the gateway never runs. |
| Admission | `ARBORA-FLEET-R029.1` | `tests/api/test_fleet_effect.py::test_query_browse_or_server_scope_reaches_the_gateway`: a query, `browse: true` or a `servers` filter reaches the gateway. |

## ARBORA-FLEET-R015.1 acceptance

| Case | Row | Proving test and observation |
|---|---|---|
| Child refusal | `ARBORA-FLEET-R015.1` | `tests/fleet/test_child_failure_classification.py::test_child_refusal_inside_task_group_keeps_child_message`: a child JSON-RPC error inside a task group answers `CHILD_REFUSED` with the child's message. The log names the server and cause. |
| Transport | `ARBORA-FLEET-R015.1` | `tests/fleet/test_child_failure_classification.py::test_transport_leaf_is_unavailable_not_refused` and `::test_dead_session_signature_is_unavailable`: a transport leaf or a stopped session answers `UNAVAILABLE`. |
| Redaction | `ARBORA-FLEET-R015.1` | `tests/fleet/test_child_failure_classification.py::test_credential_text_is_redacted_from_log_and_result`: a credential in the child's text reaches neither the log nor the result. |
| Unmountable child | `ARBORA-FLEET-R015.1` | `tests/fleet/test_child_failure_classification.py::test_unmountable_child_answers_unavailable_without_dispatch`: a child absent from the catalog answers `UNAVAILABLE`. |
| Pipeline unwrap | `ARBORA-FLEET-R015.1` | `tests/fleet/test_child_failure_classification.py::test_pipeline_unwraps_a_single_grouped_refusal` and `::test_pipeline_keeps_unsettled_for_an_unknown_grouped_cause`. |
| Nested object arguments | `ARBORA-FLEET-R015.1` | `tests/fleet/test_child_failure_classification.py::test_governed_call_hands_children_plain_json_for_nested_objects`: a frozen nested object and array argument reaches the child as plain `dict` and `list`; `::test_delegated_bound_check_rejects_frozen_mapping` pins why the thaw is needed. |
| Unsettled outcome | `ARBORA-FLEET-R015.1` | `tests/fleet/test_child_failure_classification.py::test_indeterminate_message_states_unknown_outcome` and `::test_unacquired_reservation_and_timeout_log_their_cause`: `INDETERMINATE` says the outcome is unknown, and the pipeline logs the unsettled cause. |
| Remote tracing child | `ARBORA-FLEET-R015.1` | `tests/fleet/test_multiplexer_dynamic_gateway.py::test_load_catalog_admits_remote_langfuse_entry_without_projection`: the catalog keeps a remote tracing-service child unchanged, with or without keys. |

- `ARBORA-FLEET-R041`: See each child.
- `ARBORA-FLEET-R041.1`: `tests/fleet/test_auto_onboard.py::test_trigger_and_counts_round_trip` stores and reads a record.
- `ARBORA-FLEET-R041.2`: `tests/fleet/test_auto_onboard.py::test_registration_and_release_start_one_pass_each` registers a fixture, then bumps its manifest digest.
- `ARBORA-FLEET-R041.3`: `tests/fleet/test_auto_onboard.py::test_every_trigger_reaches_the_one_onboard_function` patches `onboard` and drives each trigger.
- `ARBORA-FLEET-R041.4`: `tests/fleet/test_auto_onboard.py::test_orphan_refusal_marks_contract_rejected_and_admits_no_tools`.
- `ARBORA-FLEET-R041.5`: `tests/fleet/test_auto_onboard.py::test_status_reports_counts_digests_receipt_and_reason`.
- `ARBORA-FLEET-R041.6`: `tests/fleet/test_auto_onboard_served.py::test_register_then_release_serves_all_surfaces_without_manual_step`.
- `ARBORA-FLEET-R041.7`: `tests/fleet/test_handshake_onboard.py::test_completed_handshake_starts_one_pass_and_imports_full_pack` connects a fixture connector and calls nothing else.
- `ARBORA-FLEET-R041.8`: `tests/fleet/test_handshake_onboard.py::test_reconnect_recaptures_only_on_version_or_digest_change` reconnects unchanged, then with a new digest.
- `ARBORA-FLEET-R041.9`: `tests/fleet/test_handshake_onboard.py::test_failed_capture_serves_tools_reports_gap_and_retries_with_backoff`.
- `ARBORA-FLEET-R041.10`: `tests/fleet/test_handshake_onboard.py::test_large_shrink_on_handshake_waits_for_mass_withdrawal_approval`.
- `ARBORA-FLEET-R015.2`: `tests/fleet/test_plan_confirmation.py` (real SDK-guarded child: confirmed delete runs; unconfirmed and other-tool plans are cancelled; expired, mismatched and unknown plans dispatch nothing).
- `ARBORA-FLEET-R015.3`: `tests/test_data_migrations.py::test_spent_budget_and_unreadable_ledger_are_distinct_denials` (doubles) and `::test_native_unreadable_ledger_denial_names_the_cause` (released native engine).
- `ARBORA-FLEET-R041.15`: `tests/fleet/test_declarations.py::test_sdk_pack_declaration_is_read_from_its_archive_section`, `::test_a_section_that_does_not_match_its_digest_is_refused`, `::test_sdk_pack_ontology_bodies_are_read_from_the_archive`.
- `ARBORA-FLEET-R041.16`: `tests/api/test_served_replay_owners.py::test_served_pack_status_is_a_read_and_reaches_dispatch_without_a_key`, `::test_pack_status_tenant_comes_from_the_caller_not_the_request`.
- `ARBORA-FLEET-R041.17`: `tests/fleet/test_catalog_graph.py::test_pack_becomes_typed_assets_candidates_and_lineage`, `tests/fleet/test_catalog_graph.py::test_ntriples_escape_literals_and_dedupe`, `tests/fleet/test_catalog_graph.py::test_onboarding_writes_the_catalog_and_never_fails_on_a_refusal`.
- `ARBORA-FLEET-R041.18`: `tests/fleet/test_handshake_onboard.py::test_handshake_pass_scopes_its_loop_transports`.
- `ARBORA-FLEET-R042`: See each child.
- `ARBORA-FLEET-R042.1`: `tests/fleet/test_source_approval.py::test_decision_round_trips`.
- `ARBORA-FLEET-R042.2`: `tests/fleet/test_source_approval.py::test_read_only_admitted_contract_is_approved_and_write_contract_is_held`.
- `ARBORA-FLEET-R042.3`: `tests/fleet/test_source_approval.py::test_onboard_calls_policy_and_engine_registry_once`.
- `ARBORA-FLEET-R042.4`: `tests/fleet/test_source_approval.py::test_secret_unknown_kind_write_and_outage_hold_the_mapping`.
- `ARBORA-FLEET-R042.5`: `tests/fleet/test_source_approval.py::test_catalog_reports_approved_and_held_counts_with_reason`.
- `ARBORA-FLEET-R042.6`: `tests/fleet/test_source_approval_served.py::test_onboarded_read_contract_serves_rows_and_held_contract_serves_none`.
- `ARBORA-FLEET-R043`: See each child.
- `ARBORA-FLEET-R043.1`: `tests/api/test_query_federation.py::test_view_names_connector_contract_and_mapping`.
- `ARBORA-FLEET-R043.2`: `tests/api/test_query_federation.py::test_query_reports_virtual_sources_and_sources_op_lists_granted_ones`.
- `ARBORA-FLEET-R043.3`: `tests/api/test_query_federation.py::test_rest_and_mcp_project_the_same_sources_operation`.
- `ARBORA-FLEET-R043.4`: `tests/api/test_query_federation.py::test_ungranted_held_and_over_budget_sources_refuse_with_typed_codes`.
- `ARBORA-FLEET-R043.5`: `tests/api/test_query_federation.py::test_audit_line_names_sources_and_holds_no_rows`.
- `ARBORA-FLEET-R043.6`: `tests/api/test_query_federation_served.py::test_nl_question_spans_native_facts_and_live_connector_rows`.
- `ARBORA-FLEET-R043.7`: `tests/fleet/test_source_executor.py::test_reads_page_through_fleet_call_with_caller_delegation_and_per_source_errors`.
- `ARBORA-FLEET-R044.1`: `tests/test_installed_content.py`.
- `ARBORA-FLEET-R044.2`: `tests/test_installed_content.py`.
- `ARBORA-FLEET-R044.3`: `tests/test_installed_content.py`.
- `ARBORA-FLEET-R044.4`: `tests/test_installed_content.py`.
- `ARBORA-FLEET-R044.5`: `tests/test_installed_content.py`.

### R015/R029 WebUI bridge integration evidence

The WebUI bridge is configured by the host and discovered through the existing preloaded `find` tool. Browser API support and active per-session registrations are separate facts. No preload grants identity, attended consent, execution permission or a browser session. Boot, discovery, owner isolation and teardown regression checks are required; full web-agent execution acceptance remains open.

Discovery retirement regression: controlled verification interleavings cover disconnect and same-reference channel replacement. The final locked snapshot excludes retired or replaced candidates without changing caller authority. Full composed frontend/backend and delegated web-agent execution acceptance remain open.

Served intent integration regression: native harness and WebUI inventories include the served intent registry as well as backing tools. Real FastMCP calls preserve two synthetic owner/tenant sessions and refuse unregistered host tools. This proves the preload inventory path, not attended agent execution.

Live proposal tests cover missing origins, exact acceptance, mutation confirmation, replay refusal and disconnect. Controlled cancellations cover lease issuance, cleanup and same-process origin closure. Payload and provenance validation reject invalid data. Full acceptance remains open for authenticated session continuity and distributed cancellation. Cancellation before origin registration or in another worker does not yet revoke the local origin. The shared browser consumer and served agent path require separate composed acceptance.

Served effect ownership: `tests/fleet/test_attended_proposal_effect_owner.py` exercises the production `EgEffectJournal` over synthetic WorkItem and audit ports. Concurrent requests and same-run completed replay emit no duplicate proposal or execution. Cancellation leaves retry indeterminate without redispatch. Missing, foreign-run, foreign-session and retired origins cannot recover cached browser results. Checks include retirement during journal or audit reads. `tests/test_a2a_extension_lifespan.py` covers SDK scope ownership under real FastMCP. It tests startup, background and teardown failures, plus joined cancellation cleanup. The composed default AgentRunExecutor and multiplexer with synthetic identities reach read acceptance and separate mutation confirmation. Authenticated rendered-browser acceptance remains open. Synthetic ports and browser responders do not prove login, attended receipt or WebSocket session continuity.

Reproduce the composed journey with `uv run --no-sync pytest tests/fleet/test_attended_proposal_agent_journey.py`. This test owns real startup and shutdown, with synthetic model, engine and browser ports. It checks consent, separate mutation confirmation, completed replay, cancellation and cross-run refusal.

`tests/webui_host/attended_admission.md` documents the independent admission probe and required auth-owner interfaces. The probe records middleware behavior; it does not prove login or rendered-browser execution.

## Native served node-label readback (R015)

`tests/test_served_engine_native_labels.py` uses the actual ServedEngine pool and
an explicitly selected matching released server in an isolated anonymous store.
It covers create/add read-by-label and canonical ActionPolicy ledger readback.
Legacy type-only records stay unchanged and do not become labeled receipts.
An absent native binary skips explicitly; these tests do not qualify browser admission.

## Caller-bound served browser state (R015)

`tests/browser_control/test_served_browser_authority.py` exercises the actual
ServedEngine and WebUI factory contract. Native cases use the explicitly selected
released server and isolated anonymous store from the node-label fixture.
They cover node create/read/CAS, typed atomic trace edges, caller scope and graph mismatch.
They also cover absent ambient authority and revocation across the client-pool await.
Factory installation grants no channel, attended proof or operation authority.

### R015 terminal channel-retirement authority loss

`tests/browser_control/test_browser_control_service.py::test_disconnect_reports_terminal_retirement_authority_loss`
verifies expired, missing, and scope-lost authority stop cleanup with an error.
The local channel is absent; durable registration remains unresolved.
The existing transient-retirement regression still requires a successful retry.

## Unqualified historical policy accounting (R015)

`tests/access/test_legacy_policy_history.py` reproduces type-only audit history
against the released native server. Such candidates make requested-kind accounting
unavailable. Existing rate and blast checks then refuse unused-budget assumptions.
The test verifies that the old record remains unchanged and unrelated kinds still work.
This is safe refusal, not historical migration or receipt qualification.

## Production CLI diagnostic streams

ARBORA-FLEET-R027.7.1: test_cli_retains_diagnostics_on_stderr_and_one_json_report covers successful, partial and exceptional results. It checks retained stderr diagnostics, exact JSON decoding and stdout restoration. The ordered test imports the served-browser factory before provisioning and onboarding. It retains their original JSON assertions against released WebUI2.8.2. This is process-composition evidence, not a package release or live fleet acceptance.

ARBORA-FLEET-R027.7.2 remains planned: qualify the complete original onboarding matrix through installed providers. The stream tests do not discharge registration, capture, attestation, import or boot acceptance.

## Medulla companion rows (2026-10-28)

- `ARBORA-FLEET-R052`: Future tests cover the acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-FLEET-R052.1`: Future tests cover the acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-FLEET-R052.2`: Future tests cover the acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-FLEET-R052.3`: Future tests cover the acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-FLEET-R052.4`: Future tests cover the acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-FLEET-R052.4.1`: Future tests cover the acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-FLEET-R052.4.2`: Future tests cover the acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-FLEET-R052.4.3`: Future tests cover the acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-FLEET-R052.4.4`: Future tests cover the acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-FLEET-R052.4.5`: Future tests cover the acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-FLEET-R052.4.6`: Future tests cover the acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-FLEET-R053`: Future tests cover the acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-FLEET-R045`: Future tests cover the acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-FLEET-R034.15`: Future tests cover the acceptance and refusals in requirements.md. Tests have not run.

## Entries added for rows without a prior entry

| Test ID | Requirement | Expected observation |
|---|---|---|
| T-ARBORA-FLEET-R041.19 | ARBORA-FLEET-R041.19 | Test: `tests/fleet/test_auto_onboard.py::test_commit_then_projection_failure_names_code_and_graph_and_reprojects_once`. |
| T-ARBORA-FLEET-R041.20 | ARBORA-FLEET-R041.20 | Test: `tests/fleet/test_auto_onboard.py::test_pass_longer_than_authority_lifetime_renews_between_imports`. |
| T-ARBORA-FLEET-R041.21 | ARBORA-FLEET-R041.21 | Test: `tests/fleet/test_catalog_attest.py::test_attest_key_changes_when_the_published_component_moves`. |

## Entries added for rows without a prior entry

| Test ID | Requirement | Expected observation |
|---|---|---|
| T-ARBORA-FLEET-R003.2 | ARBORA-FLEET-R003.2 | Test: `tests/api/test_write_back_ops.py::test_preview_commit_replay_and_refusals` (L2) covers preview-then-commit, replayed commit returning the original receipt, no preview and a mismatched key. |
| T-ARBORA-FLEET-R008.2 | ARBORA-FLEET-R008.2 | Test: `tests/fleet/test_feed_catalog_served.py::test_feed_events_sanitized_and_tenant_scoped` (L3) feeds one event per kind and a rejected one through `find` over HTTP and MCP. |
| T-ARBORA-FLEET-R009.2 | ARBORA-FLEET-R009.2 | Test: `tests/a2a/test_run_admission_capacity_served.py::test_evaluate_commit_publish_with_capacity` (L3) drives grant, one re-decision, publish and release on stop. |
| T-ARBORA-FLEET-R009.3 | ARBORA-FLEET-R009.3 | Test: `tests/a2a/test_run_admission_capacity_served.py::test_all_or_nothing_and_release` (L3) asserts no partial hold on denial and release on stop. |
| T-ARBORA-FLEET-R003.3 | ARBORA-FLEET-R003.3 | Test: `tests/fleet/test_write_back_restart.py::test_write_back_survives_restart` (L3) rebuilds the registry mid-sequence and asserts one effect and an unchanged receipt. |
| T-ARBORA-FLEET-R006.3.2 | ARBORA-FLEET-R006.3.2 | The inventory test confirms `_harvest_resource_bodies` no longer exists and the skill/prompt discovery tests still pass. |
| T-ARBORA-FLEET-R006.3.3 | ARBORA-FLEET-R006.3.3 | Test: `tests/fleet/test_harvest_inventory.py::test_deadline_helpers_absent` (L1) asserts the symbols are gone; the probe deadline tests still pass. |
| T-ARBORA-FLEET-R006.3.4 | ARBORA-FLEET-R006.3.4 | Test: `tests/fleet/test_harvest_inventory.py::test_harvest_specs_absent` (L1) asserts the spec symbols are gone; the `_probe_skills` and `_probe_prompts` tests pass. |
| T-ARBORA-FLEET-R006.4 | ARBORA-FLEET-R006.4 | Test: `tests/fleet/test_harvest_inventory.py::test_no_harvest_named_symbol` (L1) greps `multiplexer.py` for `_harvest` names outside comments; `probe_catalog` tests pass. |
| T-ARBORA-FLEET-R006.5 | ARBORA-FLEET-R006.5 | Test: `tests/fleet/test_no_harvest_remains.py::test_baseline_is_zero` (L1) passes. |
| T-ARBORA-FLEET-R007.1 | ARBORA-FLEET-R007.1 | Test: `tests/test_mcp_native_primitives.py` in scion-sdk (L3, conformance kit) lists the prompt. It reads the resource and lists the template through the served instance. |
| T-ARBORA-FLEET-R007.2 | ARBORA-FLEET-R007.2 | A protocol test in meristem confirms `register_arbora_surface()`'s backing instance exposes native prompts/resources/templates alongside `arbora_registrars()`'s tools. |
| T-ARBORA-FLEET-R007.3 | ARBORA-FLEET-R007.3 | Test: `tests/mcp/test_fastmcp4_native.py::test_served_bridge_native_primitives` (L3) runs against the started MCP surface. |
| T-ARBORA-FLEET-R012.1 | ARBORA-FLEET-R012.1 | A test builds the registry from a fixture served descriptor set and gets the same operations as the file-based build for the same methods. A test proves a missing descriptor set refuses the build. A source scan finds no read of the packaged contract directory. |
| T-ARBORA-FLEET-R022 | ARBORA-FLEET-R022 | Test: `tests/fleet/test_catalog_replay.py::test_replay_twice_same_state` (L3) replays a delta sequence twice through the booted catalog. It asserts identical state, the gap refusal and the digest refusal. |
| T-ARBORA-FLEET-R024.2 | ARBORA-FLEET-R024.2 | Test: `tests/fleet/test_fleet_ops_served.py::test_reconcile_scale_and_watch_served` (L3) reproduces the recorded scale-up, scale-down and watch decisions and refuses an unauthorized caller. |
| T-ARBORA-FLEET-R033.1 | ARBORA-FLEET-R033.1 | Test: `tests/gateway/test_servers_reader.py::test_servers_scoped_by_tenant_and_principal` (L3) lists only servers visible to the calling tenant and principal through the served route. |
| T-ARBORA-FLEET-R033.2 | ARBORA-FLEET-R033.2 | Test: `tests/gateway/test_servers_reader.py::test_refusals_leak_no_records` (L3) asserts each code and an empty body. |
| T-ARBORA-FLEET-R034.1 | ARBORA-FLEET-R034.1 | Test: `tests/fleet/test_admission_models.py::test_required_fields` (L1) refuses each missing field. |
| T-ARBORA-FLEET-R034.2 | ARBORA-FLEET-R034.2 | Test: `tests/fleet/test_admission_models.py::test_default_policies_pass_and_fail` (L2) covers pass, over-limit and each missing workflow field. |
| T-ARBORA-FLEET-R034.3 | ARBORA-FLEET-R034.3 | Test: `tests/fleet/test_admission_wiring.py::test_policies_run_before_effect` (L3) asserts the order through the served route. |
| T-ARBORA-FLEET-R034.4 | ARBORA-FLEET-R034.4 | Test: `tests/fleet/test_admission_wiring.py::test_refusal_names_field_or_limit` (L3) asserts both codes and their detail. |
| T-ARBORA-FLEET-R034.5 | ARBORA-FLEET-R034.5 | Test: `tests/fleet/test_admission_wiring.py::test_trace_records_digest_policy_fields` (L3) reads the trace back. |
| T-ARBORA-FLEET-R034.6 | ARBORA-FLEET-R034.6 | Test: `tests/fleet/test_admission_hot_path.py::test_unsafe_ownerless_and_complete` (L3) passes over HTTP and MCP. |
| T-ARBORA-FLEET-R039.1 | ARBORA-FLEET-R039.1 | Test: `tests/api/test_domain_pack_contract.py::test_request_and_result_fields` (L1). It lists both models and fails on an extra field. |
| T-ARBORA-FLEET-R039.2 | ARBORA-FLEET-R039.2 | Test: `tests/api/test_domain_pack_contract.py::test_result_equals_three_engine_responses` (L2). A recorded-engine fixture compares the result with the three responses. |
| T-ARBORA-FLEET-R039.3 | ARBORA-FLEET-R039.3 | Test: `tests/api/test_domain_pack_contract.py::test_served_tool_reaches_three_engine_methods` (L3). It asserts the three recorded engine calls under the caller tenant. |
| T-ARBORA-FLEET-R039.4 | ARBORA-FLEET-R039.4 | Test: `tests/api/test_domain_pack_contract.py::test_refusals_carry_stable_codes` (L3). One case per refusal asserts the stable code. |
| T-ARBORA-FLEET-R039.5 | ARBORA-FLEET-R039.5 | Test: `tests/api/test_domain_pack_contract.py::test_result_carries_digest_and_request_ids` (L3). |
| T-ARBORA-FLEET-R039.6 | ARBORA-FLEET-R039.6 | Test: `tests/fleet/test_domain_pack_served.py::test_domain_pack_read_composes_engine_methods` (L4). |
| T-ARBORA-FLEET-R041.17.1 | ARBORA-FLEET-R041.17.1 | `tests/fleet/test_catalog_graph.py::test_onboarding_writes_owner_version_hops_and_schema_nodes` (L3, served onboarding against a loopback engine). |
| T-ARBORA-FLEET-R051.2.1 | ARBORA-FLEET-R051.2.1 | Test: `tests/fleet/test_catalog_convergence_served.py::test_convergence_read_over_mcp` (L4) reads the state over MCP. |
| T-ARBORA-FLEET-R052.5 | ARBORA-FLEET-R052.5 | Test: `tests/fleet/test_cq_xsrc_served.py::test_three_questions_over_http_and_mcp` (L3) runs each question over HTTP and MCP against a loopback engine. |
| T-ARBORA-FLEET-R054 | ARBORA-FLEET-R054 | Test: `tests/fleet/test_connect_contract.py::test_seed_body_reaches_pack_import` (L3) asserts the recorded engine request carries the seed body and a seed refusal propagates. |
| T-ARBORA-FLEET-R055 | ARBORA-FLEET-R055 | Test: `tests/fleet/test_orchestration_probes.py::test_execution_answer_through_intent_surface` (L5) asserts seven parts with provenance over a loopback medulla engine. |
| T-ARBORA-FLEET-R056 | ARBORA-FLEET-R056 | Test: `tests/fleet/test_job_downtime_plan_intent.py::test_five_questions_through_intent_surface` (L5) connects two fixture connectors and asks all five questions through `find`. |
| T-ARBORA-FLEET-R057 | ARBORA-FLEET-R057 | Test: `tests/fleet/test_source_executor_access_check.py::test_timeout_denies_every_record` runs the executor against a loopback connector (served). |
| T-ARBORA-FLEET-R058 | ARBORA-FLEET-R058 | Test: `tests/fleet/test_data_prep_bootstrap.py::test_bootstrap_declares_each_capability_once` (L3) uses a recorded engine and asserts one call per capability. |

## Planned tests

Each test below is named by its requirement row, has no running implementation yet, and is written with the row. No pass is inferred.

| Test | Requirements | Level and fixture | Positive result | Negative / boundary result |
|---|---|---|---|---|
| PT-001 | ARBORA-FLEET-R002 | `tests/fleet/test_versioned_definitions_served.py::test_catalog_reload_admits_compatible_update` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-002 | ARBORA-FLEET-R002.3 | `tests/fleet/test_versioned_definitions.py::test_nested_definition_round_trip_and_refusals` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-003 | ARBORA-FLEET-R002.4 | `tests/fleet/test_versioned_definitions.py::test_admit_definition_rules` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-004 | ARBORA-FLEET-R002.5 | `tests/fleet/test_versioned_definitions_served.py::test_onboard_calls_admission_before_import` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-005 | ARBORA-FLEET-R002.6 | `tests/fleet/test_versioned_definitions.py::test_each_refusal_code_leaves_generation_unchanged` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-006 | ARBORA-FLEET-R002.7 | `tests/fleet/test_versioned_definitions_served.py::test_receipt_names_consumed_definition` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-007 | ARBORA-FLEET-R003 | `tests/fleet/test_write_back_restart.py::test_write_back_survives_restart` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-008 | ARBORA-FLEET-R003.10 | `tests/fleet/test_repository_source_effect_served.py::test_restart_replay` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-009 | ARBORA-FLEET-R003.2 | `tests/api/test_write_back_ops.py::test_preview_commit_replay_and_refusals` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-010 | ARBORA-FLEET-R003.5 | `tests/fleet/test_repository_source_effect_served.py::test_request_binding` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-011 | ARBORA-FLEET-R003.6 | `tests/fleet/test_repository_source_effect_served.py::test_trusted_composition` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-012 | ARBORA-FLEET-R003.7 | `tests/fleet/test_repository_source_effect_served.py::test_commit_binding` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-013 | ARBORA-FLEET-R003.8 | `tests/fleet/test_repository_source_effect_served.py::test_actual_effect_readback` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-014 | ARBORA-FLEET-R003.9 | `tests/fleet/test_repository_source_effect_served.py::test_refusal_parity` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-015 | ARBORA-FLEET-R005 | `tests/fleet/test_discovery_distribution_provisioning.py::test_a2a_task_routes_through_work_item` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-016 | ARBORA-FLEET-R006 | `tests/fleet/test_no_harvest_remains.py::test_baseline_is_zero` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-017 | ARBORA-FLEET-R006.3 | `tests/fleet/test_harvest_inventory.py::test_removed_symbols_absent` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-018 | ARBORA-FLEET-R006.3.2 | `tests/fleet/test_harvest_inventory.py::test_harvest_resource_bodies_absent` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-019 | ARBORA-FLEET-R006.3.3 | `tests/fleet/test_harvest_inventory.py::test_deadline_helpers_absent` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-020 | ARBORA-FLEET-R006.3.4 | `tests/fleet/test_harvest_inventory.py::test_harvest_specs_absent` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-021 | ARBORA-FLEET-R006.4 | `tests/fleet/test_harvest_inventory.py::test_no_harvest_named_symbol` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-022 | ARBORA-FLEET-R007 | `tests/mcp/test_fastmcp4_native.py::test_served_bridge_native_primitives` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-023 | ARBORA-FLEET-R007.2 | `tests/mcp_server/test_arbora_surface_native_primitives.py::test_backing_surface_exposes_prompts_resources_and_templates` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-024 | ARBORA-FLEET-R008 | `tests/fleet/test_feed_catalog_served.py::test_feed_events_sanitized_and_tenant_scoped` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-025 | ARBORA-FLEET-R009 | `tests/a2a/test_run_admission_capacity_served.py::test_evaluate_commit_publish_with_capacity` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-026 | ARBORA-FLEET-R009.3 | `tests/a2a/test_run_admission_capacity_served.py::test_all_or_nothing_and_release` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-027 | ARBORA-FLEET-R012.1 | `tests/api/test_registry_factory.py::test_engine_operations_come_from_served_descriptors` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-028 | ARBORA-FLEET-R015.2 | `tests/fleet/test_plan_confirmation_relay.py::test_destructive_relay_answers_from_confirmed_plan` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-029 | ARBORA-FLEET-R019 | `tests/api/test_policy_eunomia.py::test_discover_load_call_modes` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-030 | ARBORA-FLEET-R019.1 | `tests/fleet/test_certified_workflow_served.py::test_workflow_admission_uses_policy_gate` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-031 | ARBORA-FLEET-R021 | `tests/fleet/test_multiplexer_runtime_policy.py::test_exact_scope_and_unknown_tool_denied` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-032 | ARBORA-FLEET-R021.1 | `tests/fleet/test_certified_workflow_served.py::test_revoked_grant_stops_later_calls` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-033 | ARBORA-FLEET-R022 | `tests/fleet/test_catalog_replay.py::test_replay_twice_same_state` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-034 | ARBORA-FLEET-R024 | `tests/fleet/test_fleet_ops_served.py::test_reconcile_scale_and_watch_served` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-035 | ARBORA-FLEET-R024.10 | `tests/fleet/test_provisioning_producer_binding.py::test_agents_run_reaches_the_allocation_producer` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-036 | ARBORA-FLEET-R024.11 | `tests/fleet/test_provisioning_aggregation.py::test_aggregation_reads_real_fleet_receipts` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-037 | ARBORA-FLEET-R024.4 | `tests/fleet/test_provisioned_capacity.py::test_request_result_round_trip` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-038 | ARBORA-FLEET-R024.5 | `tests/fleet/test_provisioned_capacity_served.py::test_two_hundred_concurrent_instances` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-039 | ARBORA-FLEET-R024.6 | `tests/fleet/test_provisioned_capacity_served.py::test_agents_run_reaches_provisioner_and_consumer` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-040 | ARBORA-FLEET-R024.7 | `tests/fleet/test_provisioned_capacity_served.py::test_refusals_allocate_nothing` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-041 | ARBORA-FLEET-R024.8 | `tests/fleet/test_provisioned_capacity_served.py::test_counters_reconcile_with_runtime` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-042 | ARBORA-FLEET-R024.9 | `tests/fleet/test_provisioned_capacity_served.py::test_matrix_two_hundred_refusal_replay_interrupt` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-043 | ARBORA-FLEET-R027 | `tests/fleet/test_fleet_onboarding.py::test_full_onboarding_matrix` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-044 | ARBORA-FLEET-R027.1 | `tests/fleet/test_discovery_distribution_provisioning.py::test_receipt_binds_all_phases` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-045 | ARBORA-FLEET-R027.2 | `tests/fleet/test_discovery_distribution_provisioning.py::test_graph_and_agent_agree_before_success` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-046 | ARBORA-FLEET-R027.3 | `tests/fleet/test_discovery_distribution_provisioning.py::test_one_request_traverses_discovery_to_skill` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-047 | ARBORA-FLEET-R027.4 | `tests/fleet/test_discovery_distribution_provisioning.py::test_denials_have_no_effect` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-048 | ARBORA-FLEET-R027.5 | `tests/fleet/test_discovery_distribution_provisioning.py::test_evidence_proves_identity_consumption` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-049 | ARBORA-FLEET-R027.6 | `tests/fleet/test_discovery_distribution_provisioning.py::test_full_sequence_with_denial_restart_duplicate` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-050 | ARBORA-FLEET-R033 | `tests/gateway/test_servers_reader.py::test_servers_scoped_by_tenant_and_principal` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-051 | ARBORA-FLEET-R033.2 | `tests/gateway/test_servers_reader.py::test_refusals_leak_no_records` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-052 | ARBORA-FLEET-R034 | `tests/fleet/test_admission_hot_path.py::test_unsafe_ownerless_and_complete` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-053 | ARBORA-FLEET-R034.1 | `tests/fleet/test_admission_models.py::test_required_fields` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-054 | ARBORA-FLEET-R034.10 | `tests/fleet/test_certified_workflow_served.py::test_refusal_codes_and_no_effect` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-055 | ARBORA-FLEET-R034.11 | `tests/fleet/test_certified_workflow_served.py::test_trace_orders_atomic_before_composed` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-056 | ARBORA-FLEET-R034.12 | `tests/fleet/test_certified_workflow_served.py::test_allowed_denied_stale` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-057 | ARBORA-FLEET-R034.13 | `tests/fleet/test_skill_upgrade_served.py::test_compatible_breaking_and_migrated_upgrades` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-058 | ARBORA-FLEET-R034.14 | `tests/fleet/test_certified_workflow_served.py::test_attestation_consumed_by_digest` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-059 | ARBORA-FLEET-R034.15 | `tests/fleet/test_certified_workflow_served.py::test_attestation_matrix` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-060 | ARBORA-FLEET-R034.2 | `tests/fleet/test_admission_models.py::test_default_policies_pass_and_fail` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-061 | ARBORA-FLEET-R034.3 | `tests/fleet/test_admission_wiring.py::test_policies_run_before_effect` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-062 | ARBORA-FLEET-R034.4 | `tests/fleet/test_admission_wiring.py::test_refusal_names_field_or_limit` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-063 | ARBORA-FLEET-R034.5 | `tests/fleet/test_admission_wiring.py::test_trace_records_digest_policy_fields` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-064 | ARBORA-FLEET-R034.7 | `tests/fleet/test_admission_models.py::test_bindings_and_cycle_refusal` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-065 | ARBORA-FLEET-R034.8 | `tests/fleet/test_certified_workflow_served.py::test_stale_dependency_or_denied_tool_blocks_all` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-066 | ARBORA-FLEET-R034.9 | `tests/fleet/test_certified_workflow_served.py::test_agents_run_reaches_gate_before_execution` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-067 | ARBORA-FLEET-R036 | `tests/fleet/test_connect_contract_served.py::test_first_connect_and_list_changed_reimport_only_changed` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-068 | ARBORA-FLEET-R036.1 | `tests/fleet/test_connect_contract.py::test_contract_record_round_trips` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-069 | ARBORA-FLEET-R036.2 | `tests/fleet/test_connect_contract.py::test_only_changed_resources_import` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-070 | ARBORA-FLEET-R036.3 | `tests/fleet/test_connect_contract.py::test_list_changed_reaches_onboard` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-071 | ARBORA-FLEET-R036.4 | `tests/fleet/test_connect_contract.py::test_contract_refusals` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-072 | ARBORA-FLEET-R036.5 | `tests/fleet/test_connect_contract.py::test_status_reports_contract` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-073 | ARBORA-FLEET-R037 | `tests/fleet/test_job_loads_served.py::test_find_job_lazily_loads_answered_tools` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-074 | ARBORA-FLEET-R037.1 | `tests/fleet/test_job_loads.py::test_job_load_round_trips` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-075 | ARBORA-FLEET-R037.2 | `tests/fleet/test_job_loads.py::test_loads_only_answered_tools` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-076 | ARBORA-FLEET-R037.3 | `tests/fleet/test_job_loads.py::test_find_job_reaches_load_tools` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-077 | ARBORA-FLEET-R037.4 | `tests/fleet/test_job_loads.py::test_job_load_refusals` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-078 | ARBORA-FLEET-R037.5 | `tests/fleet/test_job_loads.py::test_result_cites_decision` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-079 | ARBORA-FLEET-R038 | `tests/architecture/test_protocol_roles.py::test_no_second_role_implementation` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-080 | ARBORA-FLEET-R039 | `tests/fleet/test_domain_pack_served.py::test_domain_pack_read_composes_engine_methods` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-081 | ARBORA-FLEET-R039.1 | `tests/api/test_domain_pack_contract.py::test_request_and_result_fields` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-082 | ARBORA-FLEET-R039.2 | `tests/api/test_domain_pack_contract.py::test_result_equals_three_engine_responses` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-083 | ARBORA-FLEET-R039.3 | `tests/api/test_domain_pack_contract.py::test_served_tool_reaches_three_engine_methods` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-084 | ARBORA-FLEET-R039.4 | `tests/api/test_domain_pack_contract.py::test_refusals_carry_stable_codes` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-085 | ARBORA-FLEET-R039.5 | `tests/api/test_domain_pack_contract.py::test_result_carries_digest_and_request_ids` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-086 | ARBORA-FLEET-R040.2 | `tests/fleet/test_relay_read_restart.py::test_relay_read_survives_child_restart` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-087 | ARBORA-FLEET-R041 | `tests/fleet/test_auto_onboard_served.py::test_register_then_release_serves_all_surfaces_without_manual_step` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-088 | ARBORA-FLEET-R041.1 | `tests/fleet/test_auto_onboard.py::test_trigger_and_counts_round_trip` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-089 | ARBORA-FLEET-R041.17.1 | `tests/fleet/test_catalog_graph.py::test_onboarding_writes_owner_version_hops_and_schema_nodes` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-090 | ARBORA-FLEET-R041.19 | `tests/fleet/test_auto_onboard.py::test_commit_then_projection_failure_names_code_and_graph_and_reprojects_once` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-091 | ARBORA-FLEET-R041.2 | `tests/fleet/test_auto_onboard.py::test_registration_and_release_start_one_pass_each` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-092 | ARBORA-FLEET-R041.20 | `tests/fleet/test_auto_onboard.py::test_pass_longer_than_authority_lifetime_renews_between_imports` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-093 | ARBORA-FLEET-R041.21 | `tests/fleet/test_catalog_attest.py::test_attest_key_changes_when_the_published_component_moves` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-094 | ARBORA-FLEET-R041.3 | `tests/fleet/test_auto_onboard.py::test_every_trigger_reaches_the_one_onboard_function` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-095 | ARBORA-FLEET-R041.4 | `tests/fleet/test_auto_onboard.py::test_orphan_refusal_marks_contract_rejected_and_admits_no_tools` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-096 | ARBORA-FLEET-R041.5 | `tests/fleet/test_auto_onboard.py::test_status_reports_counts_digests_receipt_and_reason` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-097 | ARBORA-FLEET-R042 | `tests/fleet/test_source_approval_served.py::test_onboarded_read_contract_serves_rows_and_held_contract_serves_none` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-098 | ARBORA-FLEET-R042.1 | `tests/fleet/test_source_approval.py::test_decision_round_trips` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-099 | ARBORA-FLEET-R042.2 | `tests/fleet/test_source_approval.py::test_read_only_admitted_contract_is_approved_and_write_contract_is_held` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-100 | ARBORA-FLEET-R042.3 | `tests/fleet/test_source_approval.py::test_onboard_calls_policy_and_engine_registry_once` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-101 | ARBORA-FLEET-R042.4 | `tests/fleet/test_source_approval.py::test_secret_unknown_kind_write_and_outage_hold_the_mapping` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-102 | ARBORA-FLEET-R042.5 | `tests/fleet/test_source_approval.py::test_catalog_reports_approved_and_held_counts_with_reason` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-103 | ARBORA-FLEET-R043.1 | `tests/api/test_query_federation.py::test_view_names_connector_contract_and_mapping` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-104 | ARBORA-FLEET-R043.2 | `tests/api/test_query_federation.py::test_query_reports_virtual_sources_and_sources_op_lists_granted_ones` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-105 | ARBORA-FLEET-R043.3 | `tests/api/test_query_federation.py::test_rest_and_mcp_project_the_same_sources_operation` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-106 | ARBORA-FLEET-R043.4 | `tests/api/test_query_federation.py::test_ungranted_held_and_over_budget_sources_refuse_with_typed_codes` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-107 | ARBORA-FLEET-R043.5 | `tests/api/test_query_federation.py::test_audit_line_names_sources_and_holds_no_rows` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-108 | ARBORA-FLEET-R045 | `tests/fleet/test_ingest_worker_health.py::test_stopped_worker_is_unhealthy_and_running_worker_exposes_metrics` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-109 | ARBORA-FLEET-R051.2 | `tests/fleet/test_catalog_convergence.py::test_lagging_replica_reports_degraded_and_never_mixes` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-110 | ARBORA-FLEET-R051.2.1 | `tests/fleet/test_catalog_convergence_served.py::test_convergence_read_over_mcp` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-111 | ARBORA-FLEET-R052.1 | `tests/api/test_supply_chain_impact_served.py::test_report_shape_and_method_absent_refusal` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-112 | ARBORA-FLEET-R052.2 | `tests/api/test_supply_chain_impact_served.py::test_removing_a_connector_turns_its_source_into_an_abstention` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-113 | ARBORA-FLEET-R052.3 | `tests/api/test_sources_block.py::test_block_present_over_mcp_and_rest_and_missing_block_refused` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-114 | ARBORA-FLEET-R052.4.1 | `tests/fleet/test_competency_probes.py::test_graph_label_count_probe` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-115 | ARBORA-FLEET-R052.4.1 | `tests/fleet/test_orchestration_probes.py::test_probe_node_counts_by_label` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-116 | ARBORA-FLEET-R052.4.2 | `tests/fleet/test_orchestration_probes.py::test_probe_deploy_container_plan` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-117 | ARBORA-FLEET-R052.4.3 | `tests/fleet/test_orchestration_probes.py::test_probe_agent_library_listing` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-118 | ARBORA-FLEET-R052.4.4 | `tests/fleet/test_orchestration_probes.py::test_probe_why_intent_surface` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-119 | ARBORA-FLEET-R052.4.5 | `tests/fleet/test_orchestration_probes.py::test_probe_find_list_containers_tool` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-120 | ARBORA-FLEET-R052.4.6 | `tests/fleet/test_orchestration_probes.py::test_probe_delegate_list_containers` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-121 | ARBORA-FLEET-R052.5 | `tests/fleet/test_cq_xsrc_served.py::test_three_questions_over_http_and_mcp` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-122 | ARBORA-FLEET-R053 | `tests/fleet/test_smoke_sweep.py::test_sweep_records_rows_and_alerts_once_on_new_failure` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-123 | ARBORA-FLEET-R054 | `tests/fleet/test_connect_contract.py::test_seed_body_reaches_pack_import` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-124 | ARBORA-FLEET-R055 | `tests/fleet/test_orchestration_probes.py::test_execution_answer_through_intent_surface` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-125 | ARBORA-FLEET-R056 | `tests/fleet/test_job_downtime_plan_intent.py::test_five_questions_through_intent_surface` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-126 | ARBORA-FLEET-R057 | `tests/fleet/test_source_executor_access_check.py::test_timeout_denies_every_record` (planned) | The result the row states holds. | Each refusal the row states is returned. |
| PT-127 | ARBORA-FLEET-R058 | `tests/fleet/test_data_prep_bootstrap.py::test_bootstrap_declares_each_capability_once` (planned) | The result the row states holds. | Each refusal the row states is returned. |

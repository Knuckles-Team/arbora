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
- `ARBORA-FLEET-R041.15`: `tests/fleet/test_declarations.py::test_sdk_pack_declaration_is_read_from_its_archive_section`, `::test_a_section_that_does_not_match_its_digest_is_refused`, `::test_sdk_pack_ontology_bodies_are_read_from_the_archive`.
- `ARBORA-FLEET-R041.16`: `tests/api/test_served_replay_owners.py::test_served_pack_status_is_a_read_and_reaches_dispatch_without_a_key`, `::test_pack_status_tenant_comes_from_the_caller_not_the_request`.
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

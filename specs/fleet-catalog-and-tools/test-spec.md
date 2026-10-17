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

Provision two local graph-os replicas, one synthetic child MCP server, a fixture policy, and a disposable durable event store. Capture the callable reload op ID, prior/candidate/active generation IDs and digests, one shared trace ID, and timestamps. Change one tool schema, prompt body, resource/template body, and skill body. Refresh and prove the two replicas converge on the same complete generation. Each affected item has one ordered ingestion receipt, and sessions receive the matching notifications. Hold one call in flight during swap to prove drain behavior. Repeat with a malformed body, duplicate server identity, stale digest, deleted asset, and policy outage. Record last-known-good, error, and absence of partial publication. Restart one replica and replay receipts to prove durability and idempotency.

CI may run this probe with local containers/processes and synthetic credentials. A deployment-specific probe may additionally validate a real cluster, but its absence does not make unrelated cloud PR hooks fail. Completed acceptance requires the served probe to pass against landed code.

## Quality and contribution gates

- Run repository format/lint/type/unit suites, packaged contract regeneration in `--check` mode, and targeted MCP protocol tests from a clean checkout. Pin tools and fixtures in repository manifests; avoid host-only paths and ambient credentials.
- CCCC: new or changed functions meet the repository's cyclomatic/ABC limits. The target is at most 10; the absolute limit is 15. Split orchestration into small typed steps. KISS limits: at most 35 statements, 8 parameters, 20 locals, depth 5, 8 returns, 1 boolean parameter, 900 lines/file, 40 functions/file unless generated declarations require a documented different check. No skip, xfail, noqa, or type-ignore is used to hide a failure.
- jscpd: zero new duplicate pairs over merge base. Dupehound: zero new clones. Reuse the existing catalog reader, multiplexer, notification, and served composition paths rather than copying them.
- Static gates check no unregistered surface, no child transport call outside the fleet gateway, all native forwarders call `invoke`, no default-open unknown tool path, exact scope and service-grant registry, and generated artifact parity. Runtime gate checks policy mode matrix and authority parity.
- Fail a PR only for reproducible source, contract, or fixture failures. Record optional remote dependency or live-environment observations as release evidence, with a provisionable substitute for contributor CI.

## Evidence record template

| Commit | Case/gate | Command or fixture | Result | Environment | Timestamp | Receipt or trace |
|---|---|---|---|---|---|---|

No execution result is recorded in this template.

## GRAPHOS-FLEET-R030

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
| `GRAPHOS-FLEET-R034.7` | Typed fixtures reject missing bindings and dependency cycles. |
| `GRAPHOS-FLEET-R034.8` | A stale dependency or one denied tool prevents all workflow calls. |
| `GRAPHOS-FLEET-R034.9` | Served agents.run reaches the admission gate before AU execution. |
| `GRAPHOS-FLEET-R034.10` | Unknown tools, cyclic dependencies and revoked grants produce no new effects. |
| `GRAPHOS-FLEET-R034.11` | Independent trace reads prove atomic-before-composed ordering and exact tool binding. |
| `GRAPHOS-FLEET-R034.12` | Observe the configured model request and zero denied backend calls. |
| `GRAPHOS-FLEET-R024.4` | Typed nested request/result round trips preserve every allocation boundary. |
| `GRAPHOS-FLEET-R024.5` | Observe at least 200 concurrent instances and independent durable results under the declared budget. |
| `GRAPHOS-FLEET-R024.6` | The served operation reaches the approved SDK provisioner and returned-result consumer. |
| `GRAPHOS-FLEET-R024.7` | No denied allocation occurs; partial failures retain actual resource identities for cleanup. |
| `GRAPHOS-FLEET-R024.8` | Counters reconcile with independent runtime and durable result observations. |
| `GRAPHOS-FLEET-R024.9` | Mock allocations and smaller extrapolated runs cannot establish capacity. |
| `GRAPHOS-FLEET-R002.3` | Fixtures parse nested definitions and reject missing versions or unknown required fields. |
| `GRAPHOS-FLEET-R002.4` | Compatible updates publish atomically; incompatible evolution leaves the prior generation active. |
| `GRAPHOS-FLEET-R002.5` | Real served catalog probes supply both schema versions and verified producer receipts. |
| `GRAPHOS-FLEET-R002.6` | Missing or contradictory producer evidence never permits a generation swap. |
| `GRAPHOS-FLEET-R002.7` | Receipt reads identify exactly which definition each admitted workflow consumed. |
| `GRAPHOS-FLEET-R002.8` | MCP and REST expose the same complete admitted generation. |
| `GRAPHOS-FLEET-R027.1` | All phases retain one tenant, run identity and exact skill digest. |
| `GRAPHOS-FLEET-R027.2` | Independent graph and agent observations agree before a successful completion claim. |
| `GRAPHOS-FLEET-R027.3` | One served request traverses discovery, distribution, provisioning and ingested skill consumption. |
| `GRAPHOS-FLEET-R027.4` | Denied discovery returns no protected identity; no unauthorized agent or tool effect occurs. |
| `GRAPHOS-FLEET-R027.5` | The evidence proves graph identity consumption rather than matching copied skill text. |
| `GRAPHOS-FLEET-R027.6` | Separate helper tests cannot satisfy integrated acceptance. |
| `GRAPHOS-FLEET-R019.1` | The served integrated tests cover this consumer dependency. |
| `GRAPHOS-FLEET-R021.1` | The served integrated tests cover this consumer dependency. |
| `GRAPHOS-FLEET-R005.3` | The served integrated tests cover this consumer dependency. |

## Original parent acceptance coverage

These cases remain required in addition to the fleet execution extension.
Existing tests are reuse targets, not new evidence for these child IDs. The residual integration tests have not been executed for this change.

| Parent obligation | Child coverage | Existing test seam and required proof |
|---|---|---|
| R019 discover/load/call ordering and disabled filtering. | GRAPHOS-FLEET-R019.2 | Extend tests/api/test_policy_eunomia.py with served phase coverage and engine effect counters. |
| R019 three deployment defaults and off warning. | GRAPHOS-FLEET-R019.2 | Reuse test_profile_defaults; assert the deployment doctor warning for disabled policy. |
| R019 enabled denial and unreachable service. | GRAPHOS-FLEET-R019.2 | Reuse test_enabled_denial_and_unavailable_are_distinct and test_missing_pdp_fails_closed; exercise each served phase. |
| R021 exact scopes with no broad-admin substitute, including stdio. | GRAPHOS-FLEET-R021.2 | Serve both stdio and network clients; allow exact scopes and refuse each broader substitute. |
| R021 unknown native tool denial and active MCP policy middleware. | GRAPHOS-FLEET-R021.2 | Extend tests/mcp_server/test_graph_tool_surface.py; assert denied visibility and dispatch with middleware active. |
| R027 configured-server selection and fresh-store register/capture/attest/import. | GRAPHOS-FLEET-R027.7 | Reuse tests/fleet/test_fleet_onboarding.py config filtering and fresh-store multi-server cases. |
| R027 idempotency, admitted servers, per-server failure, registry outage and missing credential. | GRAPHOS-FLEET-R027.7 | Reuse the same onboarding test suite and assert healthy-server progress and no duplicate imports. |
| R027 complete CLI pass and non-zero exit on any failure. | GRAPHOS-FLEET-R027.7 | Reuse test_cli_runs_one_full_pass_and_fails_on_any_server through the actual command. |
| R027 background boot with non-blocking, non-fatal failure. | GRAPHOS-FLEET-R027.7 | Extend test_a_failing_pass_never_escapes_the_boot_thread with live serving and concurrent MCP requests. |

Record exact revisions and bound test IDs separately for these residual children.
A successful certification, workflow or provisioning test cannot substitute for original policy, scope or onboarding coverage.


## Repository source-effect served acceptance

These proposed tests live in tests/fleet/test_repository_source_effect_served.py. This definition change has not run them.
Each row includes positive and negative assertions; all original R003 and R003.1 through R003.3 acceptance remains required.

| Requirement | Proposed function | Required proof |
|---|---|---|
| GRAPHOS-FLEET-R003.4 | `test_original_fleet_restart` | Restart and fault each admitted writable profile. Observe original receipts without duplicate effects and no partial protocol state. Missing previews and mismatched keys must prevent writes. |
| GRAPHOS-FLEET-R003.5 | `test_request_binding` | Round-trip seeded byte fixtures through HTTP and MCP. Compare canonical projections and refuse each independently altered field. |
| GRAPHOS-FLEET-R003.6 | `test_trusted_composition` | A served request reaches the real admitted connector once. Unbound configuration and direct native bypass attempts reach it zero times. |
| GRAPHOS-FLEET-R003.7 | `test_commit_binding` | One exact approved preview commits. Each binding change, invalid lease and missing required step-up produces zero provider mutations. |
| GRAPHOS-FLEET-R003.8 | `test_actual_effect_readback` | Serve preview, approval and commit against a disposable provider. Independently read its revision and durable graph attempt. An echoed flag or unbound receipt must refuse success. |
| GRAPHOS-FLEET-R003.9 | `test_refusal_parity` | Observe zero denied mutations on both surfaces. Inject readback failure after an effect and observe uncertainty without another write. |
| GRAPHOS-FLEET-R003.10 | `test_restart_replay` | Race identical calls and crash at each effect boundary. Restart both processes and prove one effect or a truthful unresolved result. |


### Original parent coverage

| Original obligation | Retained coverage |
|---|---|
| Typed, previewed registry operation with no unguarded native child tool | R003.1, R003.2 and R003.4; verify actual served exposure and refused bypass |
| Preview/key matching and identical replayed receipt | R003.2 and R003.4; include generic writable profiles, not only repository files |
| Writable-fleet correctness across service restart and fault | R003.3 and R003.4; restart each admitted profile and observe durable results |

The original restart/fault acceptance includes absence of partial protocol state. Record uncertainty completely when an external effect is possible.
An uncertain result is not evidence that a provider write rolled back. Preserve original assertions; repository-only coverage is insufficient.

### Real served fixture and sequence

Start the actual GraphOS serving composition, a durable graph service and an admitted SDK connector in separate processes.
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


## OpenAPI operation schema preflight (`GRAPHOS-FLEET-R035`)

Run `uv run --no-sync pytest tests/fleet/test_multiplexer_schema_disclosure.py` against the frozen GraphOS lock and merged public SDK source. The fixture cases prove ordered reconstruction and compare both selected schemas before invocation. Stale manifests, missing fragments, absent output schema and mismatched action must refuse before the operation call. Changed catalog, denied session, OAuth-session mismatch and uncertain-write replay must also refuse before that call. These tests use fake child sessions and do not prove live provider publication, production credentials, deployment, or provider effects. Report focused consumer evidence separately from any later live/release evidence.

Cover non-prefixed OAuth aliases with stale disclosures and ordinary tools without schema metadata. Release preflight before cancelling its caller to prove the final-send cancellation fence. Cancel and refuse a half-open preflight, then prove a later healthy probe succeeds without replay. Compare CI's EG source revision with the lock and inspect the pinned SDK's public disclosure exports.

## Additional owner requirements

- `GRAPHOS-FLEET-R024.10`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `GRAPHOS-FLEET-R024.11`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `GRAPHOS-FLEET-R002.9`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `GRAPHOS-FLEET-R034.13`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `GRAPHOS-FLEET-R034.14`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.

## GRAPHOS-FLEET-R036 acceptance

| Case | Row | Proving test and observation |
|---|---|---|

| Model | `GRAPHOS-FLEET-R036.1` | `tests/fleet/test_connect_contract.py::test_contract_record_round_trips`: A round trip keeps every field and refuses a blank connector. |

| Behavior | `GRAPHOS-FLEET-R036.2` | `tests/fleet/test_connect_contract.py::test_only_changed_resources_import`: A second connect with one changed resource sends one import. |

| Wiring | `GRAPHOS-FLEET-R036.3` | `tests/fleet/test_connect_contract.py::test_list_changed_reaches_onboard`: A served connect and a `list_changed` event both reach `onboard`. |

| Refusal | `GRAPHOS-FLEET-R036.4` | `tests/fleet/test_connect_contract.py::test_contract_refusals`: Each case records its reason, and a rejected connector serves no tools. |

| Evidence | `GRAPHOS-FLEET-R036.5` | `tests/fleet/test_connect_contract.py::test_status_reports_contract`: The status read shows the digests and the engine receipts. |

| Hot-path proof | `GRAPHOS-FLEET-R036.6` | `tests/fleet/test_connect_contract_served.py::test_first_connect_and_list_changed_reimport_only_changed`: The first connect imports all resources, and the change imports only the seed graph. |


Hot-path proof for `GRAPHOS-FLEET-R036`: `tests/fleet/test_connect_contract_served.py::test_first_connect_and_list_changed_reimport_only_changed`.

## GRAPHOS-FLEET-R037 acceptance

| Case | Row | Proving test and observation |
|---|---|---|

| Model | `GRAPHOS-FLEET-R037.1` | `tests/fleet/test_job_loads.py::test_job_load_round_trips`: A round trip keeps every field and refuses an empty request. |

| Behavior | `GRAPHOS-FLEET-R037.2` | `tests/fleet/test_job_loads.py::test_loads_only_answered_tools`: A 300-tool fleet loads only the answered tools. |

| Wiring | `GRAPHOS-FLEET-R037.3` | `tests/fleet/test_job_loads.py::test_find_job_reaches_load_tools`: A served `find` with a job reaches `load_tools`. |

| Refusal | `GRAPHOS-FLEET-R037.4` | `tests/fleet/test_job_loads.py::test_job_load_refusals`: Each case loads no tool and returns its reason. |

| Evidence | `GRAPHOS-FLEET-R037.5` | `tests/fleet/test_job_loads.py::test_result_cites_decision`: Each loaded tool names its decision premise. |

| Hot-path proof | `GRAPHOS-FLEET-R037.6` | `tests/fleet/test_job_loads_served.py::test_find_job_lazily_loads_answered_tools`: The session lists only the answered search tools, and `tools/list_changed` fires once. |


Hot-path proof for `GRAPHOS-FLEET-R037`: `tests/fleet/test_job_loads_served.py::test_find_job_lazily_loads_answered_tools`.

## GRAPHOS-FLEET-R038 acceptance

| Case | Row | Proving test and observation |
|---|---|---|
| Evidence | `GRAPHOS-FLEET-R038` | `tests/fleet/test_protocol_roles.py::test_one_implementation_per_role`: An import-graph test fails when a graph-os module implements a connector transport or connector server scaffolding. |

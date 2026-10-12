# Verification contract

Every assertion is run against an exact commit and reports a pass/fail result. Fixture tests are required for a clean checkout and cloud PR; served release probes are separately recorded before ACCEPTED. Do not turn an unavailable external environment into a blocking source hook.

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

CI may run this probe with local containers/processes and synthetic credentials. A deployment-specific probe may additionally validate a real cluster, but its absence does not make unrelated cloud PR hooks fail. Never claim ACCEPTED until the served probe passes against landed code.

## Quality and contribution gates

- Run repository format/lint/type/unit suites, packaged contract regeneration in `--check` mode, and targeted MCP protocol tests from a clean checkout. Pin tools and fixtures in repository manifests; avoid host-only paths and ambient credentials.
- CCCC: new or changed functions meet the repository's cyclomatic/ABC limits. The target is at most 10; the absolute limit is 15. Split orchestration into small typed steps. KISS limits: at most 35 statements, 8 parameters, 20 locals, depth 5, 8 returns, 1 boolean parameter, 900 lines/file, 40 functions/file unless generated declarations require a documented different check. No skip, xfail, noqa, or type-ignore is used to hide a failure.
- jscpd: zero new duplicate pairs over merge base. Dupehound: zero new clones. Reuse the existing catalog reader, multiplexer, notification, and served composition paths rather than copying them.
- Static gates check no unregistered surface, no child transport call outside the fleet gateway, all native forwarders call `invoke`, no default-open unknown tool path, exact scope and service-grant registry, and generated artifact parity. Runtime gate checks policy mode matrix and authority parity.
- Fail a PR only for reproducible source, contract, or fixture failures. Record optional remote dependency or live-environment observations as release evidence, with a provisionable substitute for contributor CI.

## Evidence record template

| Commit | Case/gate | Command or fixture | Result | Environment | Timestamp | Receipt or trace |
|---|---|---|---|---|---|---|
| pending | pending | pending | NOT RUN | clean checkout | pending | pending |

## GRAPHOS-FLEET-R030

`tests/fleet/test_fleet_access_contracts.py` uses a fake pack with one `ontology://` entry and a fake parser.
- Onboarding registers one source connection and one mapping per contract.
- Each registered mapping is unapproved, and the catalog serves no mapping for its class.
- A second registration adds no duplicate mapping.
- A missing SDK module skips contracts and raises nothing.
- A domain ontology with triple-quoted literals never reaches the parser. A separate contracts body still registers.
- A contracts body that the parser rejects logs a warning with its URI. Onboarding continues.

## Certified fleet execution acceptance

All added cases are specified, NOT RUN. Use the actual served intent operations and REST invoke route.
The plan defines fixtures, authority boundaries, matrix coverage and measured capacity thresholds.

| Requirement | Served proof |
|---|---|
| `GRAPHOS-FLEET-R034.7` | Typed fixtures reject missing bindings and dependency cycles. |
| `GRAPHOS-FLEET-R034.8` | A stale dependency or one denied tool prevents all workflow calls. |
| `GRAPHOS-FLEET-R034.9` | Served agents.run reaches the admission gate before AU execution. |
| `GRAPHOS-FLEET-R034.10` | Unknown tools, cyclic dependencies and revoked grants produce no new effects. |
| `GRAPHOS-FLEET-R034.11` | Independent trace reads prove atomic-before-composed ordering and exact tool binding. |
| `GRAPHOS-FLEET-R034.12` | Observe the configured model request and zero denied backend calls; NOT RUN. |
| `GRAPHOS-FLEET-R024.4` | Typed nested request/result round trips preserve every allocation boundary. |
| `GRAPHOS-FLEET-R024.5` | Observe at least 200 concurrent instances and independent durable results under the declared budget. |
| `GRAPHOS-FLEET-R024.6` | The served operation reaches the approved SDK provisioner and returned-result consumer. |
| `GRAPHOS-FLEET-R024.7` | No denied allocation occurs; partial failures retain actual resource identities for cleanup. |
| `GRAPHOS-FLEET-R024.8` | Counters reconcile with independent runtime and durable result observations. |
| `GRAPHOS-FLEET-R024.9` | Mock allocations and smaller extrapolated runs cannot establish capacity; NOT RUN. |
| `GRAPHOS-FLEET-R002.3` | Fixtures parse nested definitions and reject missing versions or unknown required fields. |
| `GRAPHOS-FLEET-R002.4` | Compatible updates publish atomically; incompatible evolution leaves the prior generation active. |
| `GRAPHOS-FLEET-R002.5` | Real served catalog probes supply both schema versions and verified producer receipts. |
| `GRAPHOS-FLEET-R002.6` | Missing or contradictory producer evidence never permits a generation swap. |
| `GRAPHOS-FLEET-R002.7` | Receipt reads identify exactly which definition each admitted workflow consumed. |
| `GRAPHOS-FLEET-R002.8` | MCP and REST expose the same complete admitted generation; NOT RUN. |
| `GRAPHOS-FLEET-R027.1` | All phases retain one tenant, run identity and exact skill digest. |
| `GRAPHOS-FLEET-R027.2` | Independent graph and agent observations agree before a successful completion claim. |
| `GRAPHOS-FLEET-R027.3` | One served request traverses discovery, distribution, provisioning and ingested skill consumption. |
| `GRAPHOS-FLEET-R027.4` | Denied discovery returns no protected identity; no unauthorized agent or tool effect occurs. |
| `GRAPHOS-FLEET-R027.5` | The evidence proves graph identity consumption rather than matching copied skill text. |
| `GRAPHOS-FLEET-R027.6` | Separate helper tests cannot satisfy integrated acceptance; NOT RUN. |
| `GRAPHOS-FLEET-R019.1` | The served integrated tests cover this consumer dependency; NOT RUN. |
| `GRAPHOS-FLEET-R021.1` | The served integrated tests cover this consumer dependency; NOT RUN. |
| `GRAPHOS-FLEET-R005.3` | The served integrated tests cover this consumer dependency; NOT RUN. |

## Original parent acceptance coverage

These cases remain required in addition to the fleet execution extension.
Existing tests are reuse targets, not new evidence for these child IDs. Residual acceptance is NOT RUN for this change.

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

# GRAPHOS-DATA-MARKET — architecture and implementation design

## Ownership and reuse inventory

GraphOS is the composition root. Keep one live request path:

`REST or MCP -> GraphOS authentication/policy -> application service -> published
graph-engine or fleet contract -> typed response and audit`.

Reuse `graph_os.mcp_server.runtime` endpoint factories and `_run_json_endpoint`
for REST/MCP parity. The existing `graph_analyze_code_context_endpoint` and
`_code_context_kwargs` show the bounded context route pattern; reuse the pattern,
not its `graph_code` tool name or code-specific payload. Reuse
`graph_os.gateway.schemas.graph_core` Pydantic request discipline
(`extra="forbid"`), `graph_os.fleet.multiplexer` for admitted fleet calls,
`graph_os.gateway.fleet` for the fleet surface, and
`graph_os.control_plane.runs.admission` for run admission conventions. GraphOS
must not implement catalog extraction, ontology mapping, finance math, market
provider transport, or a second agent scheduler.

The current checkout has a served code-context route and generic graph tool
dispatch. It does **not** establish that schema context, finance ops, or durable
finance subscriptions are implemented. All three remain pending until code and
served tests provide evidence.

## Schema context

Define a strict request model in `graph_os.gateway.schemas.graph_core` (or a
small adjacent schema module) with `source_id`, `schema`, `object`, `intent`,
`depth` 0–3, `limit` 1–100, optional catalog revision. Define a versioned
response with source and tenant ID, object ID, catalog revision, `as_of`, facts,
proposals, dependency edges, citations, and `truncated`. Limits are enforced
before dispatch and again on returned payload. Add one application service,
then wire both `POST /graph/schema/context` and a `graph_schema` MCP action to
it. Both entrypoints must use identical identity, policy, and error mapping.

The graph engine serves requests through its published client API. Required
engine behavior includes a deterministic catalog graph. It must include declared
and inferred joins with evidence. It also includes approved ontology mapping
state and bounded dependency traversal. GraphOS projects these fields. It cannot derive unsupported matches. Capability discovery occurs at startup or first request. A missing or
incompatible version yields `UNAVAILABLE`; malformed input yields
`INVALID_ARGUMENT`; denied or hidden objects yield `NOT_FOUND` without leaking
existence. Engine timeout yields retryable `UNAVAILABLE`. Structured errors
carry a correlation ID and no secret source DSN.

## Application admission

Introduce an `ApplicationAdmission` aggregate in the existing control-plane
repository style with stable `(tenant_id, application_id, source_id)` identity
and states `DRAFT`, `VALIDATED`, `APPROVED`, `ATTACHING`, `VERIFYING`, `ACTIVE`,
`ROLLING_BACK`, `ROLLED_BACK`, and `FAILED`. Persist the idempotency key and
transition receipt before external actions. An operator can call dry-run,
approve, attach, verify, activate, and rollback via one service with REST/MCP
twins. The service calls the graph-engine catalog/mapping and query conformance
contracts, and the existing fleet control contract for connector state. The
source's credentials remain in the source service or secret provider; GraphOS
stores only opaque references.

Dry-run returns a signed or hash-addressed plan snapshot. Approval pins that
snapshot. Attach creates a named read-only source projection, then verifies
catalog revision, tenant scope, representative queries, drift monitor, and
rollback handle. Only an explicit separate retirement decision after `ACTIVE`
may disable the legacy ingest connector. Rollback restores routing first,
checks parity and health, then detaches projection. Any unknown effect leaves
the admission in a reconcilable state rather than claiming successful rollback.
Use local PostgreSQL and MariaDB fixtures; application identity is configurable
and examples use generic `sample_app` tables.

## Finance read, schedule, and event bridge

Build one finance application service exposed by REST and MCP. GraphOS receives
an authenticated tenant and resolves an explicit account binding before any
positions or paper-order call. It requests authoritative calculations and
records from the graph engine through a published client contract, and market
transport from an admitted connector service through the fleet. It never
imports provider clients into gateway widgets or accepts a process-global
account as proof of tenant ownership. Responses retain decimal values as
strings, source, as-of, session, currency/unit, freshness, and provenance;
avoid float money. Bound market query range, row count, and timeout before
dispatch. Server-side decimation applies to finalized series only.

Use the existing scheduler integration and workflow executor rather than a
second timer. Every job carries a tenant-scoped action token, schedule ID,
trigger ID, lease, and deterministic run key. Job types: historical backfill,
market scan, DCA due, and explanation. Backfill is read/idempotent; any action
that could create an order requires a separate write authorization. Timezone
handling uses IANA names and explicit DST policy. Explanation output must cite
the exact calculation/record IDs and label uncertainty; it cannot emit an
authorization token.

Subscribe through the durable finance outbox topic family. Project each event
to a delivery record keyed by `(tenant, subscriber, event_id, channel)` with a
durable acknowledgement and retry policy. A restart reads the cursor and
reconciles unacknowledged deliveries. Route to a configured channel adapter;
the adapter receives only the minimum data permitted by its subscriber policy.
An alert dispatch has no route to order submission. Paper trading remains
disabled until tenant-to-provider-account binding and graph-engine grant/lease
contracts are verified end to end.

For a recurring DCA plan, ask the existing scheduler for each due occurrence
and persist a proposal before any execution. The proposal key is
`(tenant, plan_id, plan_revision, scheduled_at_utc)`; an occurrence has one
proposal across restarts, timezone transitions and competing workers. Read
the current quote and account binding into a versioned preview. Paper mode
routes through the paper operation only after its grant and lease checks.
Live mode routes only through the connector SDK's governed write-back path
after a human approves the exact preview and the approval lease is consumed.
Approval and connector effect receipts are joined to the proposal, and an
unknown external effect enters reconciliation rather than being retried as a
new order. Pause, revocation, policy change or changed preview data cancels
the pending dispatch without claiming to reverse an earlier effect.

## Cross repository contract and dependency order

1. The graph engine publishes versioned schema-context, catalog revision,
   mapping status, finance record, and event APIs. GraphOS consumes public
   clients only. Its catalog/profiling and finance math are separate owners.
2. The connector SDK and connector services publish source identity, health,
   market transport, and account binding; GraphOS consumes an admitted fleet
   tool. No provider-specific import is allowed in GraphOS.
3. Agent workflows publish a scheduler and explanation interface; GraphOS
   handles authentication and dispatch while those workflows own agent logic.
4. Browser clients consume GraphOS read and operation APIs, with no privileged
   direct graph-engine or provider path.

For local development, use mock interfaces implementing these versioned
contracts. A mock proves GraphOS wiring, while final acceptance additionally
requires a served graph-engine fixture and a local database fixture. A missing
dependency is a typed `UNAVAILABLE`, never a compatibility alias.

## Quality and maintainability

Keep each behavior in one owning service and route both transports through it.
Run CCCC against changed Python modules, jscpd for duplicated code, dupehound
for semantic duplicates, KISS review for unnecessary abstraction, Ruff, mypy,
pytest, and the repository's release workflow. Any scanner finding requires a
real fix or compare it with a documented existing baseline. Never suppress a
finding to make a gate pass. Record exact commands, revisions, and results in
the spec evidence before marking `ACCEPTED`.

## Confirmed order and position changes (GRAPHOS-DATA-MARKET-R007)

Data flow: authenticated REST/MCP call -> plan confirmation -> admitted connector -> SDK proof verification and governed apply -> durable effect receipt.
Existing code: `_effect` at graph_os/api/invoke/pipeline.py:218 binds the plan at line 230.
The shared invocation path reserves durable audit and effect identity before dispatch.
Reachable today: no order or wallet operation exists. Signature verification and its trust resolver are new SDK producer wiring.

Producer: SDK-GOVERNED-WRITEBACK-R001.1 through R001.6 owns WalletProofRequest, WalletProofResult, canonical payload and supported verifier profiles.
GraphOS imports the published contract and consumes its authenticated result unchanged; it contains no crypto verifier or key registry.
Use EG's authorization decision and the existing account entitlement and effect reservation authorities.
Confirmation binds the immutable intent digest defined below. The separate proof must match that digest and all authority bindings.
Any missing producer, incompatible schema or unavailable durable proof binding keeps the operation UNAVAILABLE.
No backtest digest, signature-shaped string or client-submitted verified flag substitutes for proof.

The SDK requires trusted key/account binding, bounded validity and replay protection through existing durable effect identity.
Pass proof through the admitted connector operation; source credentials and native transaction handling remain with that connector.
Keep paper and live scopes distinct. Never relabel a live request as paper to avoid a proof or authorization requirement.
Replays resolve the same bound durable receipt; uncertain effects enter existing reconciliation, not a new order.
The producer's explicit dependency gaps remain runtime blockers even after these definitions are published.

Decision: reuse the shared plan/confirm and governed effect path. Reject a finance-only approval path or local replay store.
Cross-repository rows: SDK-GOVERNED-WRITEBACK-R001.1 through R001.6 and R004; GRAPHOS-DATA-MARKET-R003; GRAPHOS-OPS-R017.2.


## Immutable intent and separate execution proof

This contract is shared by SDK-GOVERNED-WRITEBACK-R001 and GRAPHOS-DATA-MARKET-R007. All new wiring remains SPECIFIED.
`WalletBusinessIntent` is a closed JSON object with these exact required keys:
`schema_version`, `operation_id`, `connector_id`, `account_id`, `wallet_id`, `network_id`, `mode`, `action`,
`destination`, `asset_id`, `amount`, `business_terms`, `change_set_id`, `idempotency_key`.
`schema_version` is the string `1`; `mode` is `paper` or `live`.
All other fields are nonempty strings except `business_terms`, which is a closed object under the registered operation schema.
Amounts and all quantities, prices, fees and limits use exact decimal strings; no floating-point values are permitted.
An operation without a destination, asset or amount uses explicit null only where its registered schema permits no such effect.
`business_terms` includes every remaining effective argument, including position/order identity, side, order type, deadlines and fee limits when applicable.
The registered schema must account for every provider argument and every authority-related field. Executor defaults cannot add unconfirmed business intent.
Unknown fields, duplicate keys, invalid Unicode, unbounded values and schema mismatches refuse before preparation.
Nested JSON allows strings, booleans, null, arrays, objects and exactly representable integers; object keys are schema-defined ASCII.
No normalization or mutation occurs after preparation. A changed default or schema requires a new plan and confirmation.

The complete immutable binding projection is exactly `P = {"intent": WalletBusinessIntent}`.
`plan_digest` is the lowercase hexadecimal SHA-256 of UTF-8 serialization of P.
Serialization is exactly `json.dumps(P, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`.
P contains plain JSON values, so GraphOS `claim_values(P)` preserves them.
This equals existing `graph_os/api/invoke/plan.py:24` `params_digest(P)`; it is not the signature payload digest.
Both owners use this exact definition and shared cross-language vectors, including non-ASCII values, nested terms and decimal strings.
`bind_plan` also binds authenticated principal, tenant, policy revision, registry digest, operation, effect and confirmation mode.
These authority bindings remain mandatory. Intent account, destination, amount, network and mode always participate in the digest.
SDK reconstructs P from the immutable durable change set and matches the authenticated confirmed-plan binding before verification.
The durable change-set digest covers business intent and source versions, excluding proof, confirmation metadata and the derived plan digest.
Their durable association is separate metadata; the change set must never contain a digest of its own proof.

`WalletExecutionEnvelope` has exactly `phase`, `params`, `plan_ref`, `confirmation_ref`, `wallet_proof` and `idempotency_key`.
`params` equals P. The other members are execution metadata, never business parameters.
`phase` is `prepare`, `confirm` or `execute`; nullable references and proof are absent only when their phase permits.
HTTP uses this exact body. MCP adds these explicit properties beside `op`; its existing `execute` flag must agree with phase.
Confirm requires an explicit authenticated human action recorded by the existing durable authority; a client reference cannot assert confirmation.
`wallet_proof` is the SDK WalletProofRequest; its plan digest equals the definition above.
Proof tenant/actor must match authenticated authority; connector, operation, account, wallet and network must equal intent.
Proof nonce equals `change_set_id`; envelope, intent and proof idempotency keys must agree.
All proof metadata is verified, including signature, current trusted key, validity, change-set digest and confirmation association.
Separating proof from P permits later signing; it does not permit unsigned business fields or client-selected projection rules.
The canonical signed payload remains the existing domain-separated RFC 8785 profile, with `plan_digest` as a value.
No plan digest, confirmation reference or proof occurs inside P. Signature insertion therefore introduces no digest cycle.

The shared `WalletExecutionEnvelope` extension is missing and blocks activation of these operations.
HTTP `graph_os/api/http/routes.py:72` currently merges the entire JSON body into params before invocation at line 127.
For these registered operations, its new closed envelope parser must pass only envelope.params as operation parameters.
MCP `graph_os/api/mcp/verbs.py:26` PARAMETERS and `_invoke_and_render` at line 258 need explicit wallet proof and confirmation metadata.
The MCP `params` property must carry the same P as HTTP; duplicate or conflicting metadata refuses.
The shared Invoke contract must carry typed envelope metadata separately through confirmation and connector dispatch.
This is an explicit shared transport dependency, not an existing side channel or handler-level field removal.
`pipeline._effect` at line 218, `bind_plan` at line 230 and `EgPlanStore.validate` at plan.py:160 must use P unchanged.
`audit_event` at audit.py:28 and `_journal_binding` must use that same business projection and preserve all existing authority bindings.
Proof evidence joins by digest/reference separately; raw signatures must not enter logs or create a different business-effect identity.
Shared envelope validation must precede plan consumption; current `_effect` consumes a matching lease and lacks this proof-aware lifecycle.
Preparation and explicit confirmation must persist a binding without dispatching or consuming the execution lease.
Execution validates proof and confirmation before consuming that lease through the existing governed reservation path.
Failure, races and restart must retain existing durable fencing and reconciliation guarantees. Do not add another approval or nonce store.
Until envelope parsing, durable association and lifecycle wiring exist for both transports, refuse with UNAVAILABLE and no effect.
SDK maps unavailable binding to `verification_unavailable`; mismatched intent or proof bindings cannot become verification success.

<!-- spec-definition-patch:eg66-reads -->
## Structured marketplace reads (GRAPHOS-DATA-MARKET-R003.1–R003.6)

Define the nine operation IDs `markets.list/search/get`,
`events.list/search/get`, and `orders.list/search/get`; route REST and MCP
through one typed finance service and the R010 envelope. Reuse
`graph_os/finance/markets.py` and the existing R017.1/R010 operation patterns.
The current checkout lacks planned `graph_os/api/ops/finance.py` and
`markets.py`. Contributors must add and wire those modules. This spec adds
definitions only.

List/search accept strict objects with `limit` (default 50, 1–100), optional
opaque cursor, and documented resource filters; search requires bounded
nonempty query text. Get requires one resource ID. Reject extra fields,
malformed IDs/cursors, empty query, and invalid limits before dispatch. Cursors
bind tenant, account, resource, filters, and sort position. List/search data
has exactly `schema_version` (1), `as_of` (RFC 3339 UTC), `items`, and
`page` (`limit`, nullable `next_cursor`); get has the version, timestamp,
and one `item`. Resource keys are specified in R003.4; decimals are strings,
optional values explicit nulls, and provider-only keys are excluded. Ordering
and cursor continuation are deterministic; a provider unable to meet bounded
pagination is unavailable, not emulated with an unbounded fetch.

Authorize `finance:read`, resolve tenant membership and verified account,
then send the scoped request to the provider. Caller-selected authority is
rejected. Missing/mismatched binding uses the parent's typed unavailable
response; scope refusal maps through R010. Missing and cross-tenant IDs are
indistinguishable. Malformed, denied, unavailable, and inaccessible requests
cause zero provider calls. Read handlers have no effect capability: no paper
submit, lease reservation, or `finance:paper-trade` grant. Preserve parent R003
paper submission and R017.2 subject ownership separately; do not add an
account selector, provider client, error code, lease, or receipt store.

Implementation awaits a public provider contract with bounded list/search/get
for all three resources. GraphOS also needs typed operation bindings. Keep reads
unavailable until dependencies and served tests exist. This specification claims
no implementation or test result.
<!-- /spec-definition-patch -->

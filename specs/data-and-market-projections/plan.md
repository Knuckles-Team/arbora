# ARBORA-DATA-MARKET — architecture and implementation design

## Ownership and reuse inventory

Arbora is the composition root. Keep one live request path:

`REST or MCP -> Arbora authentication/policy -> application service -> published
graph-engine or fleet contract -> typed response and audit`.

Reuse `arbora.mcp_server.runtime` endpoint factories and `_run_json_endpoint`
for REST/MCP parity. The existing `graph_analyze_code_context_endpoint` and
`_code_context_kwargs` show the bounded context route pattern; reuse the pattern,
not its `graph_code` tool name or code-specific payload. Reuse
`arbora.gateway.schemas.graph_core` Pydantic request discipline
(`extra="forbid"`), `arbora.fleet.multiplexer` for admitted fleet calls,
`arbora.gateway.fleet` for the fleet surface, and
`arbora.control_plane.runs.admission` for run admission conventions. Arbora
must not implement catalog extraction, ontology mapping, finance math, market
provider transport, or a second agent scheduler.

The current checkout has a served code-context route and generic graph tool
dispatch. It does **not** establish that schema context, finance ops, or durable
finance subscriptions are implemented. All three remain pending until code and
served tests provide evidence.

## Schema context

Define a strict request model in `arbora.gateway.schemas.graph_core` (or a
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
state and bounded dependency traversal. Arbora projects these fields. It cannot derive unsupported matches. Capability discovery occurs at startup or first request. A missing or
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
source's credentials remain in the source service or secret provider; Arbora
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

Build one finance application service exposed by REST and MCP. Arbora receives
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
A human approves the exact preview. The governed execution path consumes the approval lease. Live mode then routes only through the connector SDK's governed write-back path.
Arbora joins approval and connector-effect receipts to the proposal.
Unknown external effects enter reconciliation; Arbora must not retry them as
new orders. Pause, revocation, policy change or changed preview data cancels
the pending dispatch without claiming to reverse an earlier effect.

## Cross repository contract and dependency order

1. The graph engine publishes versioned schema-context, catalog revision,
   mapping status, finance record, and event APIs. Arbora consumes public
   clients only. Its catalog/profiling and finance math are separate owners.
2. The connector SDK and connector services publish source identity, health,
   market transport, and account binding; Arbora consumes an admitted fleet
   tool. No provider-specific import is allowed in Arbora.
3. Agent workflows publish a scheduler and explanation interface; Arbora
   handles authentication and dispatch while those workflows own agent logic.
4. Browser clients consume Arbora read and operation APIs, with no privileged
   direct graph-engine or provider path.

For local development, use mock interfaces implementing these versioned
contracts. A mock proves Arbora wiring, while final acceptance additionally
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

## Confirmed order and position changes (ARBORA-DATA-MARKET-R007)

Data flow: authenticated REST/MCP call -> plan confirmation -> admitted connector -> SDK proof verification and governed apply -> durable effect receipt.
Existing code: `_effect` at arbora/api/invoke/pipeline.py:218 binds the plan at line 230.
The shared invocation path reserves durable audit and effect identity before dispatch.
Reachable today: no order or wallet operation exists. Signature verification and its trust resolver are new SDK producer wiring.

Producer: SDK-GOVERNED-WRITEBACK-R001.1 through R001.6 owns WalletProofRequest, WalletProofResult, canonical payload and supported verifier profiles.
Arbora imports the published contract and consumes its authenticated result unchanged; it contains no crypto verifier or key registry.
Use EG's authorization decision and the existing account entitlement and effect reservation authorities.
Confirmation binds the immutable intent digest defined below. The separate proof must match that digest and all authority bindings.
Any missing producer, incompatible schema or unavailable durable proof binding keeps the operation UNAVAILABLE.
No backtest digest, signature-shaped string or client-submitted verified flag substitutes for proof.

The SDK requires trusted key/account binding, bounded validity and replay protection through existing durable effect identity.
Pass proof through the admitted connector operation; source credentials and native transaction handling remain with that connector.
Keep paper and live scopes distinct. Never relabel a live request as paper to avoid a proof or authorization requirement.
Replays resolve the same bound durable receipt; uncertain effects enter existing reconciliation, not a new order.
The producer's explicit dependency gaps still block runtime delivery after owners publish these definitions.

Decision: reuse the shared plan/confirm and governed effect path. Reject a finance-only approval path or local replay store.
Cross-repository rows: SDK-GOVERNED-WRITEBACK-R001.1 through R001.6 and R004; ARBORA-DATA-MARKET-R003; ARBORA-OPS-R017.2.


## Immutable intent and separate execution proof

This contract is shared by SDK-GOVERNED-WRITEBACK-R001 and ARBORA-DATA-MARKET-R007. All new wiring remains SPECIFIED.
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
P contains plain JSON values, so Arbora `claim_values(P)` preserves them.
This equals existing `arbora/api/invoke/plan.py:24` `params_digest(P)`; it is not the signature payload digest.
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
HTTP `arbora/api/http/routes.py:72` currently merges the entire JSON body into params before invocation at line 127.
For these registered operations, its new closed envelope parser must pass only envelope.params as operation parameters.
MCP `arbora/api/mcp/verbs.py:26` PARAMETERS and `_invoke_and_render` at line 258 need explicit wallet proof and confirmation metadata.
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
## Structured marketplace reads (ARBORA-DATA-MARKET-R003.1–R003.6)

The registry IDs are `markets.list`, `markets.search`, `markets.get`,
`events.list`, `events.search`, `events.get`, `orders.list`, `orders.search`,
and `orders.get`. All nine are read-only `finance:read` operations and use the
R010 response envelope.

| Operation IDs | Handler and typed provider method |
|---|---|
| `markets.list/search/get` | `arbora/api/ops/finance.py::operations` binds the three handlers to `FinanceReadPortV1.list_markets`, `.search_markets`, and `.get_market`. |
| `events.list/search/get` | The same module binds to `FinanceReadPortV1.list_events`, `.search_events`, and `.get_event`. |
| `orders.list/search/get` | The same module binds to `FinanceReadPortV1.list_orders`, `.search_orders`, and `.get_order`. |

`FinanceReadPortV1` is the new Arbora-owned upstream adapter contract in
`arbora/finance/markets.py`. The existing `MarketProvider.market_read` and
`MarketReadRequest` cover one symbol price only; they cannot provide these nine
resource reads. Bind the new port as `finance_read_port` in the composed `services` object. Every typed request carries the already-resolved tenant and account internally. Client parameters never select either identity. No provider port or registered operation currently implements this contract. The EG scope catalog also lacks `finance:read`. Do not admit or serve the operations until EG adds the scope and a provider exposes the capability. This specification assigns no external producer requirement ID. `ARBORA-DATA-MARKET-R003.6` defines the missing Arbora port and registration boundary. The existing owner links are `ARBORA-OPS-R010` (envelope), `ARBORA-OPS-R017.1` (subject/account authority), and `ARBORA-OPS-R017.2` (separate paper-trade subject ownership).

### Request contract

Each operation rejects unlisted fields. List/search accept `limit` from 1
through 100; the default is 50. They may also accept an opaque `cursor` up to
512 UTF-8 bytes. Get accepts only its resource ID. Market list/search filters are
`event_id`, `status`, `starts_at_from`, `starts_at_to`, `ends_at_from`, and
`ends_at_to`. Event list/search filters are `status`, `starts_at_from`,
`starts_at_to`, `ends_at_from`, and `ends_at_to`. Order list/search filters are
`market_id`, `status`, `side` (`buy` or `sell`), `created_at_from`, and
`created_at_to`. Time filters are inclusive RFC 3339 UTC instants.

All resource IDs are case-sensitive opaque ASCII strings matching
`[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`. This v1 grammar bounds untrusted IDs while
preserving common provider identifiers without decoding or case folding.
Search requires `query`: NFC text of 1–200 UTF-8 bytes. Search is a literal,
case-sensitive substring match over market question/outcome name, event
title/description, or order ID/market ID/outcome, respectively. Search does not rank results. List/search use deterministic ordering.
Markets sort by `starts_at` ascending with nulls last, then `market_id` ascending.
Events sort by `starts_at` ascending with nulls last, then `event_id` ascending.
Orders sort by `created_at` descending with nulls last, then `order_id` ascending.
The server fixes the sort. A cursor binds schema version, operation, resolved
tenant and account, filters, sort position, and source snapshot revision.

### Version-1 response contract

Each result schema rejects unlisted keys. List/search responses contain exactly `schema_version`, `as_of`, `items`, and `page`. The version is integer 1. The timestamp is the authoritative provider snapshot time in RFC 3339 UTC. The page contains exactly `limit` and nullable `next_cursor`. Get responses contain exactly `schema_version`, `as_of`, and `item`. Each item has `source` (nonempty source ID), `as_of` (provider record time), nullable `session`, `freshness`, and `provenance`. `freshness` has exactly `state` and `age_seconds`. State is `fresh`, `stale`, or `unknown`; age is a nonnegative integer or null. `provenance` has exactly `record_id` and nullable `revision`. Arbora returns provider metadata unchanged. Unknown freshness uses `state: unknown` and `age_seconds: null`. Arbora does not infer freshness or provenance.

`MarketItemV1` has exactly `market_id`, nullable `event_id`, `question`,
`status`, nullable `starts_at` and `ends_at`, `outcomes`, nullable `updated_at`,
and the common item metadata. Each outcome has exactly `name`, `price`,
`currency`, and `unit`. `price` is a nullable decimal string. `currency` is
null or a three-letter uppercase ISO 4217 code. `unit` is null or a bounded
code. `EventItemV1` has exactly `event_id`, `title`, nullable
`description`, `status`, nullable `starts_at` and `ends_at`, `market_ids`,
nullable `updated_at`, and the common item metadata. `OrderItemV1` has exactly
`order_id`, `market_id`, `side`, nullable `outcome`, nullable decimal-string
`price`, nullable `price_currency`, nullable `price_unit`, nullable
`quantity`, nullable `quantity_unit`, `status`, nullable `created_at` and
`updated_at`, and the common item metadata. Non-null currency values use the
same ISO 4217 grammar; units are nonempty codes of at most 32 ASCII characters
matching `[A-Za-z][A-Za-z0-9._-]{0,31}`. Decimal strings use base-10 notation
without exponent; floats are never accepted. Required fields are non-null.
Nullable fields remain present as explicit nulls. Arbora omits provider-only fields. Arbora preserves source, currency/unit, session, freshness, and provenance on price-bearing records.

### Attachment and acceptance

Declare operations in `arbora/api/ops/finance.py::operations`. The `get_registry` factory imports them from that module. Each handler calls only the typed `finance_read_port`; handlers cannot reserve a lease or reach paper submission. REST follows the real composition seam:
`NativeGatewayApplication.mount_rest_routes` validates contract admission,
then `arbora/api/http/app.py::create_api_application` registers
`POST /api/v1/ops/{op_id}`. The current process has no API dependencies,
so this adapter is not yet production-attached.

MCP follows `arbora/api/mcp/serving.py::register_intent_surface` and the
`/mcp` streamable-HTTP protocol route. Its JSON-RPC `tools/call` uses the
registered `ask` tool with `arguments.op` and `arguments.params`. The current
`arbora/mcp_server/server.py` still uses the legacy agent-utilities surface;
the operation-backed Arbora MCP registration is not active. These are explicit missing attachment edges, not served evidence. Keep all nine operations unavailable until Arbora admits the provider port and authority contract.

Add future served tests at `tests/mcp_server/test_data_market_reads_served.py`. The test-spec names each required function and route-level acceptance. Preserve FI-01 through FI-10 and parent paper-order acceptance. This definition adds no runtime or test result claim.
<!-- /spec-definition-patch -->

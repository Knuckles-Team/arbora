# GRAPHOS-DATA-MARKET — test and acceptance contract

Tests use local graph-engine and PostgreSQL/MariaDB fixtures plus a deterministic
market provider stub. No test depends on a private network, operator inventory,
real brokerage account, or live notification channel. Keep source fixtures
small, versioned, and synthetic.

| ID | Level | Scenario and expected evidence |
|---|---|---|
| SC-01 | unit | Reject unknown `intent`, oversized limit/depth, malformed source ID, extra request fields; zero downstream calls. |
| SC-02 | integration | Declared FK, inferred join and proposal, approved ontology class, owning app, and view/mapping impact return with stable IDs, catalog revision, provenance, and bounded `truncated` flag. |
| SC-03 | security | Tenant B asks for A's source or guessed table: same nonrevealing result as absent table, zero engine dispatch for denied scope. |
| SC-04 | contract | Missing/incompatible graph-engine schema capability or no approved catalog returns typed `UNAVAILABLE`; no fabricated facts. |
| SC-05 | served | MCP and REST requests on the same fixture yield equivalent normalized response and correlation/audit record. |
| AD-01 | unit | Dry-run computes requirements and rollback steps without mutation or connector retirement. |
| AD-02 | integration | Local Postgres and MariaDB fixture each pass catalog, mapping, conformance, parity and activation with explicit approval. |
| AD-03 | negative | Unapproved mapping, broad grant, missing conformance or missing rollback proof blocks approval and causes no attach call. |
| AD-04 | failure | Inject failure at each attach/verify/activate transition; routing restores old path and connector stays active. Unknown detach effect remains reconcilable. |
| AD-05 | durability | Duplicate idempotency key and restart/replay produce one admission record and one attach effect. Concurrent admissions for the same app serialize. |
| AD-06 | security | Tenant B cannot inspect, approve, or roll back tenant A's admission; no source calls on denial. |
| FI-01 | contract | Market response contains exact decimal strings, source, as-of, currency/unit, session, and staleness; bounded range/rows/time. |
| FI-02 | security | `finance:read` denial and missing tenant/account binding cause zero provider calls for market/position reads. Two tenants never share account responses. |
| FI-03 | parity | REST and MCP finance routes use the same service and normalized result/error; no second provider client. |
| FI-04 | schedule | Backfill and scan replay with same run key yield one work item; DST spring/fall cases follow specified policy. |
| FI-05 | explanation | All numerical claims cite input IDs; absent facts are labelled unknown; output cannot authorize order. |
| FI-06 | event | Duplicate/out-of-order finance events and process restart yield one user-visible delivery per subscriber/channel, durable cursor and retry receipt. |
| FI-07 | safety | Alerts and DCA-due messages cannot invoke paper or live order endpoints; hostile event payload is rejected. |
| FI-08 | paper | Without `finance:paper-trade`, tenant-bound account and lease, paper submit returns typed failure before any provider effect. Repeated valid key yields one receipt. |
| FI-09 | isolation | Concurrent A/B market reads, schedules, and alerts preserve tenant scoping and data minimization. |
| FI-10 | DCA recurrence and execution | Duplicate due events, DST ambiguity and worker restart create one proposal per plan revision/occurrence. Paused or revoked plans create no effect. Paper mode uses a paper grant/lease. Live mode without a human-approved exact preview and connector lease creates no provider call. Changed quote, amount, account, mode or policy invalidates the prior approval. An uncertain connector outcome enters reconciliation and is not blindly resent. |

## Fresh checkout execution

1. Clone `arbora` and follow this repository's `AGENTS.md` for `uv sync
   --extra test`. The release workflow supplies pinned public sibling
   dependencies; a local contributor may use published packages or clone their
   public sources using the repository's documented setup.
2. Start fixture graph engine and disposable PostgreSQL/MariaDB services with
   public test configuration. Seed synthetic `sample_app.customer` and
   `sample_app.order` tables and a fake provider account per tenant. Generate
   no fixture with real account or source data.
3. Run focused unit/contract tests, then `uv run pytest`, `uv run ruff check .`,
   `uv run ruff format --check .`, `uv run mypy arbora`, and the applicable
   repository hooks. Run CCCC, jscpd, dupehound and KISS review on changed code.
4. Start one Arbora server and call both MCP and REST surfaces with A/B
   identities. Capture sanitized request IDs, response schemas, denied-call
   counters, restart/replay results, and the exact tested revision.

## Acceptance record

| Check | Revision / command / artifact | Result |
|---|---|---|
| SC-01–05 | pending | NOT RUN |
| AD-01–06 | pending | NOT RUN |
| FI-01–10 | pending | NOT RUN |
| full repository quality gates | pending | NOT RUN |
| served local fixture | pending | NOT RUN |

Mark delivery `LANDED` only from a merged default-branch revision. Mark
acceptance `ACCEPTED` only after all applicable rows pass on that revision.

## Spec-audit rollup tests

- GRAPHOS-DATA-MARKET-R007: the hot-path proof test passes and the typed refusal leaves no effect.


## Wallet producer and consumer acceptance

`tests/integration/test_order_confirmation.py::test_wallet_proof_confirmation_and_replay` binds GRAPHOS-DATA-MARKET-R007.1 through R007.6.
Start Arbora, the actual SDK verifier and a synthetic connector with a running durable graph authority.
Enter both REST and MCP with the same authenticated caller and approved plan; observe exactly one controlled provider mutation.
Repeat with missing confirmation, invalid signature, changed payload, wrong account/network/tenant, expired proof and revoked key; observe zero mutations.
Remove each trust, verifier or durable-binding dependency and assert typed UNAVAILABLE before provider mutation.
Submit concurrent duplicates, then restart and replay; read the original receipt and assert no second effect.
Assert equivalent transport errors and matching audit, proof, confirmation and effect references without raw signatures or secrets.
These are planned served acceptance cases, not runtime evidence supplied by this specification change.


### Served immutable-intent acceptance

Start from an immutable durable change set produced by `GovernedWriteBack.dry_run`; its business fields and effect key define P.

1. POST /api/v1/ops/{op_id} with phase=prepare, params=P and null plan_ref, confirmation_ref and wallet_proof.
   Return frozen P, plan_ref, plan_digest, change-set ID/digest and effect key; assert zero provider effects.
2. POST the same operation with phase=confirm, unchanged P, returned plan_ref and the explicit authenticated human confirmation action.
   Persist the confirmation reference and unchanged P; assert zero effects and an unconsumed execution lease.
3. Sign the returned canonical SDK payload with a synthetic trusted key, after confirmation.
   Insert WalletProofRequest and confirmation metadata into the envelope without changing P.
4. POST the same operation with phase=`execute` and the completed envelope; require independent EG authorization and SDK verification.
   Assert the prepare, confirmation, SDK, plan validation, audit and reservation records contain the same plan_digest.
   Assert exactly one provider mutation and its durable receipt. Repeat the entire flow through MCP tools/call.
5. Change account, destination, amount, network, mode, operation or any nested business term after confirmation.
   Each change must yield PLAN_MISMATCH before dispatch, even if the attacker signs the changed intent with a trusted key.
   At the connector boundary, a binding mismatch yields scope_mismatch and zero effects.
6. Insert a valid proof into an unchanged confirmed P; assert neither recursive hashing nor PLAN_MISMATCH occurs.
   A changed signature, proof digest or confirmation association fails its separate validation without changing the computed intent digest.
7. Race two identical envelopes, restart and replay; read the same durable receipt without another effect.
   Missing envelope wiring, unavailable authority and uncertain completion refuse or reconcile without another submission.

These are required future served tests using the existing named acceptance cases. No runtime acceptance result is available.

<!-- spec-definition-patch:eg66-reads -->
## Structured marketplace reads (GRAPHOS-DATA-MARKET-R003.1–R003.6)

These are required future tests; no runtime result is claimed. The tests run
through the real mounted Arbora surfaces with an instrumented local provider.

- **FI-11 — registration and request schema:** `tests/mcp_server/test_data_market_reads_served.py::test_all_nine_operations_have_closed_v1_schemas` checks all nine operation IDs and closed request schemas. It pins resource filters and limit bounds. It covers the 512-byte cursor cap, ID grammar, and the 1–200-byte NFC query cap. It checks search fields and fixed ordering. It rejects extra keys before provider calls.
- **FI-12 — projection and metadata:** `tests/mcp_server/test_data_market_reads_served.py::test_v1_projections_preserve_metadata_and_paginate_deterministically` pins exact page/get/item keys and nullability. It checks decimal strings and UTC timestamps. It checks source/session/currency/unit/freshness/provenance and cursor continuation. It rejects provider-only keys. Arbora preserves unknown metadata as explicit null or unknown where allowed. Arbora never synthesizes these values.
- **FI-13 — authority refusal:** `tests/mcp_server/test_data_market_reads_served.py::test_denials_and_malformed_requests_precede_provider_calls` tests tenant A/B and missing or mismatched accounts on the real routes. REST calls `POST /api/v1/ops/markets.list`. MCP sends JSON-RPC `tools/call` to `POST /mcp` with `name: ask` and `arguments.op: markets.list`. Denials use R010, make zero provider calls, and reveal no cross-tenant records.
- **FI-14 — route parity:** `tests/mcp_server/test_data_market_reads_served.py::test_rest_post_and_mcp_tools_call_share_the_finance_port` exercises both registered entrypoints. Equivalent requests use one `finance_read_port`. Results and refusals normalize identically and retain audit correlation.
- **FI-15 — effect isolation:** `tests/mcp_server/test_data_market_reads_served.py::test_read_operations_never_reserve_or_submit_effects` spies on paper submission and effect reservations. Reads carry only `Effect.READ` and reach neither path.
- **FI-16 — paper submission preservation:** `tests/mcp_server/test_data_market_reads_served.py::test_paper_submit_keeps_separate_authority_and_receipt` retains parent R003 and FI-08 prerequisites: `finance:paper-trade`, verified account, lease, and idempotency. Denials cause no effect. A repeated valid key yields one receipt. Preserve R017.2 ownership.
- **FI-17 — unavailable dependency:** `tests/mcp_server/test_data_market_reads_served.py::test_missing_scope_or_provider_is_unavailable` verifies that a missing `finance:read` scope, `finance_read_port`, or resource capability returns typed unavailable before dispatch. The operation never fabricates an empty result.

The route contract is the generic v1 `POST /api/v1/ops/{op_id}` registered by `create_api_application`, and the `/mcp` streamable-HTTP JSON-RPC `tools/call` path invoking `ask` with an operation ID and params. Both require explicit composition and contract admission. The current process has not activated those finance bindings; these tests remain future acceptance.
<!-- /spec-definition-patch -->

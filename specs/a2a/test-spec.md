# ARBORA-A2A — test contract

## A2A task and approval projection
Status: **READY FOR IMPLEMENTATION**. Delivery: **NOT ACCEPTED**. Governing spec: [ARBORA-A2A](spec.md).

| Test | Requirement | Setup and expected result |
|---|---|---|
| A2A-T01 | A2A-01 | With fixture human and tenant, fetch filtered Agent Card, send a task with idempotency key, replay key and get same task, list it by cursor, cancel pending task. Missing key and foreign-tenant task refuse. |
| A2A-T02 | A2A-01, A2A-02 | Start local ephemeral engine/agent services. Stream ordered durable events; disconnect and resubscribe with cursor after restart. Each event arrives once by ID, terminal state persists, and task ID resolves to the same parent WorkItem/child relation. |
| A2A-T03 | A2A-02 | Assembly returns only caller-allowed agent skills and budgeted fleet items. Revoke a scope, refresh card and dispatch, and verify removed skill plus no alternate route. Missing upstream assembly returns `UNAVAILABLE`. |
| A2A-T04 | A2A-03 | Invoke read, write and fleet call via A2A, MCP and HTTP with the same principal. Codes, effect gate, audit shape and registry digest agree. Unknown op/method and forged actor/tenant fields refuse without executing. |
| A2A-T05 | A2A-04, A2A-05 | Pause on a PLAN effect; inspect durable `input-required` event and safe preview, restart Arbora, sign confirmation as original human, consume fence once, observe exactly one effect and one linked audit outcome. |
| A2A-T06 | A2A-05 | Table-drive different human, service/agent token, foreign tenant, invalid signature, missing/revoked grant, altered op/params/pending call/parent-child relation, stale policy/digest, expired lease, replay, canceled task and concurrent confirm. All return a distinct stable refusal with zero extra effects. |
| A2A-T07 | A2A-04, A2A-05 | Trigger identity admin or live approval requiring console. A2A returns `STEP_UP_REQUIRED` and safe URL; A2A confirm refuses; same-human fresh MFA console flow succeeds after two-person rule where required. |
| A2A-T08 | A2A-06 | Audit reservation outage prevents effect. Simulate outcome write failure after effect: response is indeterminate with reconciliation handle; no rollback claim. Error bodies omit token, tenant and private actor values. |
| A2A-T09 | A2A-07 | Clean public clone with pinned published dependencies passes contract suite and package build; no private service or sibling tree. CI-provisioned local served test passes streaming/approval/restart on exact commit. |

Run `uv run pytest tests/a2a tests/api`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy arbora`, `uvx --from pre-commit==4.6.0 pre-commit run --all-files`, and `uv build --wheel --out-dir dist`. CCCC and KISS remain within configured limits, jscpd records zero new pairs, and Dupehound records no new clones. Capture exact commit, command, versions, result and public CI link in a local `evidence.md` before changing delivery to `ACCEPTED`.

## A2A streaming, push notifications and transition-history projection
Status: **READY FOR IMPLEMENTATION**. Delivery: **NOT ACCEPTED** except `ARBORA-A2A-R009.1`. Governing [spec](spec.md).

| Test | Requirement | Setup and expected result |
|---|---|---|
| A2A-HT01 | A2A-H01, ARBORA-A2A-R009.1 | No authorities supplied: `A2AAgentCapabilities.from_wired_authorities()` returns all-false; `A2AAgentCapabilities()` default also all-false. Landed and proven by `tests/a2a/test_models.py::test_default_capabilities_are_all_false` and `::test_from_wired_authorities_with_none_stays_false`. |
| A2A-HT02 | A2A-H01, ARBORA-A2A-R009.1 | A conforming authority object for one capability is supplied: only that field is true, the other two stay false. Landed and proven by `tests/a2a/test_models.py::test_from_wired_authorities_advertises_only_what_is_wired` and `::test_from_wired_authorities_all_three_wired`. |
| A2A-HT03 | A2A-H01, ARBORA-A2A-R009.1 | A non-conforming object (missing the protocol's method) is supplied for any of the three authority kwargs: `from_wired_authorities` raises `TypeError`. No field is silently set true. Landed and proven by `tests/a2a/test_models.py::test_from_wired_authorities_refuses_non_conforming_object`. |
| A2A-HT04 | A2A-H02 | Planned projection tests must preserve event ordering and typed UNAVAILABLE on missing or stale events. Acceptance requires an owner-published AU task/tenant reader and real durable replay after restart. Scripted fixtures alone cannot close this producer dependency. |
| A2A-HT05 | A2A-H03 | With a scripted EG durable-kernel fixture, `tasks/get` with history requested returns exactly the fixture's transition ledger. (Open — depends on `ARBORA-A2A`.) |
| A2A-HT06 | A2A-H04 | No `A2ATransitionHistoryAuthority` wired, or the wired fixture authority raises on call: `tasks/get` history returns `A2ATransitionHistoryUnavailable` with a privacy-safe message and no history data. (Open.) |
| A2A-HT07 | A2A-H05 | Push target fails once then recovers: delivered once via the durable outbox, surviving a simulated process restart. A target that fails repeatedly past the outbox's configured attempt budget is dead-lettered and reported. (Open.) |
| A2A-HT08 | A2A-H06 | With the `ARBORA-A2A` dependency flag unset, composition refuses to wire a real streaming/push/history authority. Capability fields stay false regardless of attempted wiring. (Open.) |
| A2A-HT09 | A2A-H07 | Integration fixture exercises a chosen subset of streaming/push/history. A contract test asserts the Agent Card's advertised subset at that revision exactly matches what the fixture actually exercises. (Open.) |

Run `uv run pytest tests/a2a`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy arbora`, and `uvx --from pre-commit==4.6.0 pre-commit run --files <changed>` for this PR's `ARBORA-A2A-R009.1` slice (A2A-HT01–HT03). The remaining rows (A2A-HT04–HT09) require `ARBORA-A2A` and live AU/EG fixtures and stay open until implemented. Capture exact commit, command, versions and result in a local `evidence.md` before changing delivery to `ACCEPTED`.

## A2A context-budget tool-subset admission
Status: **READY FOR IMPLEMENTATION**. Delivery: **NOT ACCEPTED**. Governing spec: [ARBORA-A2A-002](spec.md).

| Test | Requirement | Setup and expected result |
|---|---|---|
| A2A-ADM-T01 | ARBORA-A2A-R016, ARBORA-A2A-R016.1 | Construct a `ToolSubsetAdmissionRequest` with a representative agent graph reference and `A2AContextBudget`. Call `admit_tool_subset`. Expect `admitted is False`, `admitted_tools == ()`, `refusal_reason == "EG_ASSEMBLE_UNAVAILABLE"`, and a non-empty `provenance` naming the unavailable EG operation. |
| A2A-ADM-T02 | ARBORA-A2A-R019 | For both the refused result in A2A-ADM-T01 and any future admitted result, assert the decision's `provenance` field is non-empty and, on refusal, `refusal_reason` is one of the typed enumeration values. |
| A2A-ADM-T03 | ARBORA-A2A-R017 (open) | Once an EG assembly client is injectable: a scripted committed `AgentAssemble` answer covering the request's agent graph and budget yields progress past `EG_ASSEMBLE_UNAVAILABLE`; a scripted abstention or unavailable answer still refuses with that same reason. |
| A2A-ADM-T04 | ARBORA-A2A-R018 (open) | Once wired: given a committed EG answer but an envelope lacking the allowed-tool-subset field, refuse with `ENVELOPE_LACKS_ALLOWED_TOOL_SUBSET`. Given both, admit with `admitted_tools` equal to the EG answer's subset and `provenance` naming the EG decision record and the signed envelope. |
| A2A-ADM-T05 | ARBORA-A2A-R020 | A scripted partial or stale upstream answer (fewer tools than the agent graph requires, or an expired decision record) refuses. It never reduces to a smaller admitted subset. |
| A2A-ADM-T06 | ARBORA-A2A-R021 | `uv run pytest tests/a2a/test_admission.py` passes from a fresh public clone with no external network, private sibling checkout, or live identity provider. |
| A2A-ADM-T07 | ARBORA-A2A-R016.2 | Run `tests/a2a/test_admission.py::test_request_requires_model_profile_and_task_class`; it must pass and bind to this row. |
| A2A-ADM-T08 | ARBORA-A2A-R016.3 | Run `tests/a2a/test_admission.py::test_admitted_decision_carries_harness_config_and_budget`; it must pass and bind to this row. |
| A2A-ADM-T09 | ARBORA-A2A-R016.4 | Run `tests/a2a/test_admission.py::test_refusal_reasons_are_closed_set`; it must pass and bind to this row. |
| A2A-ADM-T10 | ARBORA-A2A-R016.5 | Run `tests/a2a/test_routing.py::test_route_decision_carries_model_profile_and_task_class`; it must pass and bind to this row. |
| A2A-ADM-T11 | ARBORA-A2A-R017.1 | Run `tests/a2a/test_admission.py::test_decider_port_returns_typed_answer`; it must pass and bind to this row. |
| A2A-ADM-T12 | ARBORA-A2A-R017.2 | Run `tests/a2a/test_admission.py::test_admission_makes_one_engine_call`; it must pass and bind to this row. |
| A2A-ADM-T13 | ARBORA-A2A-R017.3 | Run `tests/a2a/test_admission.py::test_priority_is_set_from_principal_class`; it must pass and bind to this row. |
| A2A-ADM-T14 | ARBORA-A2A-R017.4 | Run `tests/a2a/test_admission.py::test_missing_budget_refuses_before_engine_call`; it must pass and bind to this row. |
| A2A-ADM-T15 | ARBORA-A2A-R017.5 | Run `tests/a2a/test_service_admission.py::test_send_message_refuses_without_admission`; it must pass and bind to this row. |
| A2A-ADM-T16 | ARBORA-A2A-R017.6 | Run `tests/a2a/test_admission_e2e.py::test_message_send_enqueues_envelope_with_harness_config`; it must pass and bind to this row. |
| A2A-ADM-T17 | ARBORA-A2A-R018.1 | Run `tests/a2a/test_authority_envelope.py::test_enqueue_passes_harness_config`; it must pass and bind to this row. |
| A2A-ADM-T18 | ARBORA-A2A-R018.2 | Run `tests/a2a/test_admission.py::test_envelope_lacking_harness_config_refuses`; it must pass and bind to this row. |
| A2A-ADM-T19 | ARBORA-A2A-R020.1 | Run `tests/a2a/test_admission.py::test_abstention_refuses_and_does_not_apply_default`; it must pass and bind to this row. |
| A2A-ADM-T20 | ARBORA-A2A-R020.2 | Run `tests/a2a/test_admission.py::test_unavailable_stale_or_partial_harness_answer_refuses`; it must pass and bind to this row. |
| A2A-ADM-T21 | ARBORA-A2A-R019.1 | Run `tests/a2a/test_admission.py::test_provenance_names_harness_decision_record`; it must pass and bind to this row. |
| A2A-ADM-T22 | ARBORA-A2A-R014.1 | Run `tests/a2a/test_agent_card.py::test_card_has_no_harness_field_before_admission_e2e`; it must pass and bind to this row. |
| A2A-HOLD-T01 | ARBORA-A2A-R005.3 | Run `tests/a2a/test_tool_action_admission.py::test_models_are_closed_and_frozen`; it must pass and bind to this row. |
| A2A-HOLD-T02 | ARBORA-A2A-R005.4 | Run `tests/a2a/test_tool_action_admission.py::test_admit_returns_engine_verdict_unchanged`; it must pass and bind to this row. |
| A2A-HOLD-T03 | ARBORA-A2A-R005.5 | Run `tests/a2a/test_tool_action_admission.py::test_confirm_dispatch_reaches_admitter`; it must pass and bind to this row. |
| A2A-HOLD-T04 | ARBORA-A2A-R005.6 | Run `tests/a2a/test_tool_action_admission.py::test_each_refusal_reason_denies`; it must pass and bind to this row. |
| A2A-HOLD-T05 | ARBORA-A2A-R005.7 | Run `tests/a2a/test_tool_action_admission.py::test_audit_entry_names_decision_without_values`; it must pass and bind to this row. |
| A2A-HOLD-T06 | ARBORA-A2A-R005.8 | Run `tests/a2a/test_tool_action_admission_e2e.py::test_confirm_returns_engine_verdict_and_audit`; it must pass and bind to this row. |
| A2A-HOLD-T07 | ARBORA-A2A-R006.3 | Run `tests/a2a/test_held_item_admission.py::test_models_are_closed_and_frozen`; it must pass and bind to this row. |
| A2A-HOLD-T08 | ARBORA-A2A-R006.4 | Run `tests/a2a/test_held_item_admission.py::test_program_needs_distinct_verifier_and_required_passes`; it must pass and bind to this row. |
| A2A-HOLD-T09 | ARBORA-A2A-R006.5 | Run `tests/a2a/test_held_item_admission.py::test_confirm_dispatch_reaches_held_item_admitter`; it must pass and bind to this row. |
| A2A-HOLD-T10 | ARBORA-A2A-R006.6 | Run `tests/a2a/test_held_item_admission.py::test_each_refusal_leaves_item_held`; it must pass and bind to this row. |
| A2A-HOLD-T11 | ARBORA-A2A-R006.7 | Run `tests/a2a/test_held_item_admission.py::test_audit_entry_names_verdict_and_item`; it must pass and bind to this row. |
| A2A-HOLD-T12 | ARBORA-A2A-R006.8 | Run `tests/a2a/test_held_item_admission_e2e.py::test_confirm_admits_or_rejects_held_item`; it must pass and bind to this row. |

Run `uv run pytest tests/a2a/test_admission.py`, `uv run ruff check arbora/a2a/admission.py tests/a2a/test_admission.py`, `uv run ruff format --check arbora/a2a/admission.py tests/a2a/test_admission.py`, and `uv run mypy arbora/a2a/admission.py`. A2A-ADM-T03 and A2A-ADM-T04 remain open until the EG and AU dependencies (`EG-DECISION-ENGINE-R036`, `AU-CONTROL-R035`) are callable; this PR lands A2A-ADM-T01, A2A-ADM-T02, and A2A-ADM-T06 only. Capture exact commit, command, and result before changing delivery to `ACCEPTED`.

Approval acceptance prerequisites: ARBORA-A2A-R005.6 requires full R005.2 and the R006.2/R006.2.1 verified-caller bridge. The served tool-action fixture must refuse before these prerequisites exist. It must prove input-required/signature binding. It must cover replay, changed-params, different-human, expired and revoked-grant cases. R006.5/R006.8 must exercise a dedicated engine held-item port through shared invoke with Surface.A2A. Preserve all fences and assert zero calls to operator-console elevation.

The served confirm fixture rejects missing, unknown and mixed confirmation_kind variants. It routes tool_action and held_item through the same async invoke/authority/audit path. It refuses missing engine policy evidence without a local threshold fallback.

## Critique-gated output release (ARBORA-A2A-R022)

- `tests/a2a/test_application.py::test_send_message_holds_output_until_bound_critique_passes` and `tests/a2a/test_application.py::test_send_message_rejects_untyped_preflight_and_failed_critique`: traverse held-item admission with a verified caller. Bind critique to the exact output. Missing, mismatched, rejected, or abstained verdicts leave output held. Routing alone cannot satisfy critique acceptance. These are proposed tests; no execution is claimed.

## Non-blocking `message/send` (ARBORA-A2A-R001.6)

- `tests/a2a/test_nonblocking_send.py::test_non_blocking_send_outlives_the_client_read_timeout`: a 31 s turn over the served HTTP route answers in under 5 s and `tasks/get` follows it to `completed`.
- `tests/a2a/test_nonblocking_send.py::test_send_without_blocking_false_keeps_blocking`: absent, empty and `blocking: true` configurations answer completed.
- `tests/a2a/test_nonblocking_send.py::test_unknown_send_configuration_is_refused_before_admission`: an unknown configuration field creates zero tasks.

## Additional owner requirements

- `ARBORA-A2A-R001.2`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.

## Verified caller and shared invocation acceptance

Planned tests/a2a/test_verified_caller_bridge.py must start the actual A2A HTTP application and current authenticator.
Use the real AU GraphSession, caller_from_session projection and verify_current_from_session refresh.
Bind credentials at the request authentication boundary; an ambient synthetic session alone cannot qualify that boundary.
Assert exact actor and tenant preservation through request dispatch into the shared invocation pipeline.
Test absent, expired, revoked, wrong-tenant and unsupported actor credentials with zero authority and backend calls.
Do not relabel ai_agent or automated_service to satisfy the human/service projection.
Use read, write and fleet operations from the actual registry and compare A2A, MCP and HTTP scope/refusal behavior.
The fixture must prove InvokeServices is composed by the production serving path, not injected only into an isolated helper.
Returning a caller without reaching that path cannot close R006.2.

The compatibility probe used a synthetic human session and real AU/Arbora projection and refresh functions.
It observed HTTP 501 and JSON-RPC -32011 for arbora.op/invoke and arbora.plan/confirm, with zero authority calls.
Those results establish the current refusal and compatible session shape only.
They do not establish production credential binding, deployed service, TLS, engine execution or signed human approval.
Preserve every R005.2 input-required, signature, binding, expiry, revocation and replay acceptance case.
R001/R010 durable replay and host receipt/log acceptance remain pending under their respective owner contracts.

| Requirement | Planned test | Acceptance |
|---|---|---|
| ARBORA-A2A-R006.2.1.1 | `tests/a2a/test_verified_caller_bridge.py::test_http_retains_and_refreshes_verified_caller` | Preserve request-bound caller identity and refuse before downstream calls. |
| ARBORA-A2A-R006.2.2 | `tests/a2a/test_verified_caller_bridge.py::test_serving_composition_reaches_shared_invoke` | Production composition reaches shared invocation and proves operation-specific surface parity. |

## Detached signature validation

`ARBORA-A2A-R005.2.2`: use ephemeral test keys with real Ed25519 signatures. Verify canonical binding bytes and reject changed identities, params digest, human, expiry and inactive grant snapshots. Exercise the existing `/a2a` confirmation refusal with no composed authority. A valid signature is only cryptographic evidence. It cannot prove live pending state, key ownership, revocation freshness or one-time consumption.

### ARBORA-A2A-R010.1

`tests/a2a/test_terminal_event.py` exercises safe projection, producer binding refusals, post-read authority refresh and zero reads for unauthorized tasks. HTTP dispatch and MCP share the same service option. These injected consumer tests do not establish native replay or released dependency compatibility.

## Tool-task evidence boundaries

`ARBORA-A2A-R002.2.2.1` binds to `tests/a2a/test_tool_task.py`.
It tests one invocation using controlled dispatcher and fence ports.
`ARBORA-A2A-R002.2.2.2` binds to the native adapter contract and temporary-engine tests.
These cover session expiry, changed scope, exact claims, lease loss and refusal to reclaim started items.
`ARBORA-A2A-R002.2.3` remains pending. Its served tests must exercise durable caller and request resolution, restart and uncertain-effect reconciliation.
No test for the first two children substitutes for that acceptance.

`ARBORA-A2A-R002.2.3.1` binds to `tests/a2a/test_tool_task_requests.py` and
`tests/a2a/test_tool_task_request_native.py`. Controlled tests reject wrong
callers, locators, digests, sessions and excessive payloads.
The native fixture stores through the published EG blob and WorkItem methods, then closes the client.
It restarts that same temporary engine store without re-enrollment.
A fresh client resolves the exact request. This proves request-data persistence,
not durable credential recovery, queue dispatch or execution authorization.

`ARBORA-A2A-R002.2.3.2` retains the parent served execution matrix and has no acceptance evidence in this increment.


## Baseline native unary reads

`tests/a2a/test_native_task_reads.py` binds ARBORA-A2A-R001 to the native
WorkItem caller view. It covers owner/schema filters, bounded empty pages with opaque
continuations, and tenant/owner cursor fences. It also covers malformed and foreign-row refusals
and native millisecond timestamps. A fresh authority recovers replies from
existing terminal RunEvent payloads only after task authorization; wrong-run,
truncated and ambiguous payloads refuse. Terminal-receipt fixtures isolate their
separate opt-in reader. The real serving gate must also prove listing and reply
recovery after both EG and Arbora restart. Each authentication mode requires this proof.

Native bounded-output acceptance extends `test_real_anonymous_boot.py` through
explicit simulator-only prompt triggers. One emits exactly 65,536 astral Unicode
characters; another emits enough astral characters for terminal event JSON to
exceed 1 MiB. Actual A2A send/get and the native EG caller query must agree on the
complete valid reply. Otherwise, both must agree on native completion, a versioned
payload-error marker, and a typed unavailable reply. Unique model receipts bind each request to its
response. Only the model is simulated; this gate must not replace graph storage,
authentication, provisioning or A2A authority. An unselected binary skips honestly.

- `ARBORA-A2A-R005.2.3`: exercise exact human, tenant, session and key ownership. Cover expiry, revocation, revision races, source renewal without approval extension and absent owners after evaluator reconstruction. Assert only snapshot evidence results. No test claims atomic consumption or production authority.

- `ARBORA-A2A-R005.2.4`: use real ephemeral Ed25519 signatures to test registration bytes and field tampering. Prove that invalid proof cannot reach registration. Exercise unsupported published methods through the real generated validator. These tests do not prove native producer integration or served approval.


## 2026-10-23 current-main approval reconciliation

The original approval snapshot evaluator and explicit registration adapter are
rebased without replacing current-main caller-run credential lease or process
expiry/revocation refusal behavior. Snapshot evidence is never execution
authority; atomic pending approval consumption remains unimplemented.

The producer prerequisite is the reconciled EG identity source with its
Human-owner guard and freshly generated contract. Existing installed generated
methods may refuse these new operations; that refusal is not integration
success. Local unit fixtures qualify only their injected snapshot and proof
fixtures. Native producer, durable CAS, installed consumer and served signed
approval acceptance remain open until exact-source evidence is available.

`ARBORA-A2A-R005.2.5` owns the remaining full-chain acceptance, not the snapshot or registration fixtures. Run the actual installed native and served path from pending call through signed approval to one effect. Include restart and concurrent replay, changed parameters, foreign human or tenant, expiry, revocation and intervening authority changes. No test in this increment supplies that proof. The row remains open and confirmation stays refused.

## Typed conversation admission foundation

R001.3.1: tests/test_conversation_admission.py covers unchanged generated conversation fields and command identity binding. It covers rejection of untyped and non-native carriers, exact-request conflicts on each conversation identity field, and lost-response readback. Existing work-item and native-admission tests guard legacy behavior. These source tests do not prove native ordering, served preview or history replay.

R001.3.2 retains the served restart, next-turn history, caller context listing and foreign-context refusal fixtures of R001.3. It remains open until actual composed history tests pass.

Internal prefix projection coverage under ARBORA-A2A-R001.3.2 uses `tests/a2a/test_conversation_preview.py`. It covers immutable input selection, explicit predecessor order, prefix-only completion, caller changes, changed displayed text and unrelated later appends. Foreign, missing, legacy, incomplete and oversized histories must refuse. This does not qualify branch submission, AU consumption, served routes or native restart.

## Reply cache follows terminal commit (2026-10-27)

Under R001.3, `test_terminal_reply_commit.py` captures the reply cache while the
native terminal commit runs and after that commit raises. Neither observation
may expose an uncommitted reply. A successful executor must run exactly once;
commit failure must propagate without automatic execution replay. A working task
must remain working without artifacts. These source regressions verify the terminal commit boundary independently of
the storage profile. Only an explicit committed acknowledgement publishes the
reply. An uncertain, refused or unrecognized result leaves no cached reply.
They do not prove native persistence or restart acceptance.

## Terminal-only completed-text consumer (R001.3.3)

Use generated native producer contracts for final integration. Source fixtures
exercise owner, tenant, graph, revision, first-attempt grant and prompt-digest
binding. Refuse malformed history and any current checkpoint before model calls.
Test copied and lost asynchronous context: private transcript content must not
fall back to generic RunEvent persistence. Preserve disabled legacy behavior.
Test exact replay of admission without choosing a newer predecessor, and refuse
missing continuation heads rather than creating replacement conversations.
Only a successful native terminal commit can publish reply text. A private
owner-only point read supplies historical reply text after restart; public event
markers cannot substitute. Installed native close/reopen, actual model-input
capture and no repeated effects remain required by R001.3.2.

Source cases cover a first browser label and its repeated use with a native head.
They also cover foreign head bindings, absent canonical heads and legacy task
membership without a native head. Each refusal occurs before new admission.
These fixtures do not prove an installed browser restart journey.

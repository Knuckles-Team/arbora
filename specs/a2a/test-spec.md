# GRAPHOS-A2A — test contract

## A2A task and approval projection
Status: **READY FOR IMPLEMENTATION**. Delivery: **NOT ACCEPTED**. Governing spec: [GRAPHOS-A2A](spec.md).

| Test | Requirement | Setup and expected result |
|---|---|---|
| A2A-T01 | A2A-01 | With fixture human and tenant, fetch filtered Agent Card, send a task with idempotency key, replay key and get same task, list it by cursor, cancel pending task. Missing key and foreign-tenant task refuse. |
| A2A-T02 | A2A-01, A2A-02 | Start local ephemeral engine/agent services. Stream ordered durable events; disconnect and resubscribe with cursor after restart. Each event arrives once by ID, terminal state persists, and task ID resolves to the same parent WorkItem/child relation. |
| A2A-T03 | A2A-02 | Assembly returns only caller-allowed agent skills and budgeted fleet items. Revoke a scope, refresh card and dispatch, and verify removed skill plus no alternate route. Missing upstream assembly returns `UNAVAILABLE`. |
| A2A-T04 | A2A-03 | Invoke read, write and fleet call via A2A, MCP and HTTP with the same principal. Codes, effect gate, audit shape and registry digest agree. Unknown op/method and forged actor/tenant fields refuse without executing. |
| A2A-T05 | A2A-04, A2A-05 | Pause on a PLAN effect; inspect durable `input-required` event and safe preview, restart GraphOS, sign confirmation as original human, consume fence once, observe exactly one effect and one linked audit outcome. |
| A2A-T06 | A2A-05 | Table-drive human and service/agent tokens, foreign tenants, and invalid signatures. Include missing and revoked grants. Include altered operations, params, pending calls, and parent-child relations. Include stale policy/digest, expired leases, replay, canceled tasks, and concurrent confirmation. Each refusal returns a distinct stable code with zero extra effects. |
| A2A-T07 | A2A-04, A2A-05 | Trigger identity admin or live approval requiring console. A2A returns `STEP_UP_REQUIRED` and safe URL; A2A confirm refuses; same-human fresh MFA console flow succeeds after two-person rule where required. |
| A2A-T08 | A2A-06 | Audit reservation outage prevents effect. Simulate outcome write failure after effect: response is indeterminate with reconciliation handle; no rollback claim. Error bodies omit token, tenant and private actor values. |
| A2A-T09 | A2A-07 | Clean public clone with pinned published dependencies passes contract suite and package build; no private service or sibling tree. CI-provisioned local served test passes streaming/approval/restart on exact commit. |

Run `uv run pytest tests/a2a tests/api`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy graph_os`, `uvx --from pre-commit==4.6.0 pre-commit run --all-files`, and `uv build --wheel --out-dir dist`. CCCC and KISS remain within configured limits, jscpd records zero new pairs, and Dupehound records no new clones. Capture exact commit, command, versions, result and public CI link in a local `evidence.md` before changing delivery to `ACCEPTED`.

## A2A streaming, push notifications and transition-history projection
Status: **READY FOR IMPLEMENTATION**. Delivery: **NOT ACCEPTED** except `GRAPHOS-A2A-R009.1`. Governing [spec](spec.md).

| Test | Requirement | Setup and expected result |
|---|---|---|
| A2A-HT01 | A2A-H01, GRAPHOS-A2A-R009.1 | No authorities supplied: `A2AAgentCapabilities.from_wired_authorities()` returns all-false; `A2AAgentCapabilities()` default also all-false. Landed and proven by `tests/a2a/test_models.py::test_default_capabilities_are_all_false` and `::test_from_wired_authorities_with_none_stays_false`. |
| A2A-HT02 | A2A-H01, GRAPHOS-A2A-R009.1 | A conforming authority object for one capability is supplied: only that field is true, the other two stay false. Landed and proven by `tests/a2a/test_models.py::test_from_wired_authorities_advertises_only_what_is_wired` and `::test_from_wired_authorities_all_three_wired`. |
| A2A-HT03 | A2A-H01, GRAPHOS-A2A-R009.1 | A non-conforming object (missing the protocol's method) is supplied for any of the three authority kwargs: `from_wired_authorities` raises `TypeError`. No field is silently set true. Landed and proven by `tests/a2a/test_models.py::test_from_wired_authorities_refuses_non_conforming_object`. |
| A2A-HT04 | A2A-H02 | With a scripted AU WorkItem event fixture, `message/stream` emits ordered status/step/terminal events matching the fixture. A missing or stale event in the fixture yields a typed `UNAVAILABLE`, not a fabricated event. (Open — depends on `GRAPHOS-A2A`.) |
| A2A-HT05 | A2A-H03 | With a scripted EG durable-kernel fixture, `tasks/get` with history requested returns exactly the fixture's transition ledger. (Open — depends on `GRAPHOS-A2A`.) |
| A2A-HT06 | A2A-H04 | No `A2ATransitionHistoryAuthority` wired, or the wired fixture authority raises on call: `tasks/get` history returns `A2ATransitionHistoryUnavailable` with a privacy-safe message and no history data. (Open.) |
| A2A-HT07 | A2A-H05 | Push target fails once then recovers: delivered once via the durable outbox, surviving a simulated process restart. A target that fails repeatedly past the outbox's configured attempt budget is dead-lettered and reported. (Open.) |
| A2A-HT08 | A2A-H06 | With the `GRAPHOS-A2A` dependency flag unset, composition refuses to wire a real streaming/push/history authority; capability fields stay false regardless of attempted wiring. (Open.) |
| A2A-HT09 | A2A-H07 | Integration fixture exercises a chosen subset of streaming/push/history. A contract test asserts the Agent Card's advertised subset at that revision exactly matches what the fixture actually exercises. (Open.) |

Run `uv run pytest tests/a2a`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy graph_os`, and `uvx --from pre-commit==4.6.0 pre-commit run --files <changed>` for this PR's `GRAPHOS-A2A-R009.1` slice (A2A-HT01–HT03). The remaining rows (A2A-HT04–HT09) require `GRAPHOS-A2A` and live AU/EG fixtures and stay open until implemented. Capture exact commit, command, versions and result in a local `evidence.md` before changing delivery to `ACCEPTED`.

## A2A context-budget tool-subset admission
Status: **READY FOR IMPLEMENTATION**. Delivery: **NOT ACCEPTED**. Governing spec: [GRAPHOS-A2A-002](spec.md).

| Test | Requirement | Setup and expected result |
|---|---|---|
| A2A-ADM-T01 | GRAPHOS-A2A-R016, GRAPHOS-A2A-R016.1 | Construct a `ToolSubsetAdmissionRequest` with a representative agent graph reference and `A2AContextBudget`. Call `admit_tool_subset`. Expect `admitted is False`, `admitted_tools == ()`, `refusal_reason == "EG_ASSEMBLE_UNAVAILABLE"`, and a non-empty `provenance` naming the unavailable EG operation. |
| A2A-ADM-T02 | GRAPHOS-A2A-R019 | For both the refused result in A2A-ADM-T01 and any future admitted result, assert the decision's `provenance` field is non-empty and, on refusal, `refusal_reason` is one of the typed enumeration values. |
| A2A-ADM-T03 | GRAPHOS-A2A-R017 (open) | Once an EG assembly client is injectable: a scripted committed `AgentAssemble` answer covering the request's agent graph and budget yields progress past `EG_ASSEMBLE_UNAVAILABLE`; a scripted abstention or unavailable answer still refuses with that same reason. |
| A2A-ADM-T04 | GRAPHOS-A2A-R018 (open) | Once wired: given a committed EG answer but an envelope lacking the allowed-tool-subset field, refuse with `ENVELOPE_LACKS_ALLOWED_TOOL_SUBSET`. Given both, admit with `admitted_tools` equal to the EG answer's subset and `provenance` naming the EG decision record and the signed envelope. |
| A2A-ADM-T05 | GRAPHOS-A2A-R020 | A scripted partial or stale upstream answer (fewer tools than the agent graph requires, or an expired decision record) refuses. It never reduces to a smaller admitted subset. |
| A2A-ADM-T06 | GRAPHOS-A2A-R021 | `uv run pytest tests/a2a/test_admission.py` passes from a fresh public clone with no external network, private sibling checkout, or live identity provider. |
| A2A-ADM-T07 | GRAPHOS-A2A-R016.2 | Run `tests/a2a/test_admission.py::test_request_requires_model_profile_and_task_class`; it must pass and bind to this row. |
| A2A-ADM-T08 | GRAPHOS-A2A-R016.3 | Run `tests/a2a/test_admission.py::test_admitted_decision_carries_harness_config_and_budget`; it must pass and bind to this row. |
| A2A-ADM-T09 | GRAPHOS-A2A-R016.4 | Run `tests/a2a/test_admission.py::test_refusal_reasons_are_closed_set`; it must pass and bind to this row. |
| A2A-ADM-T10 | GRAPHOS-A2A-R016.5 | Run `tests/a2a/test_routing.py::test_route_decision_carries_model_profile_and_task_class`; it must pass and bind to this row. |
| A2A-ADM-T11 | GRAPHOS-A2A-R017.1 | Run `tests/a2a/test_admission.py::test_decider_port_returns_typed_answer`; it must pass and bind to this row. |
| A2A-ADM-T12 | GRAPHOS-A2A-R017.2 | Run `tests/a2a/test_admission.py::test_admission_makes_one_engine_call`; it must pass and bind to this row. |
| A2A-ADM-T13 | GRAPHOS-A2A-R017.3 | Run `tests/a2a/test_admission.py::test_priority_is_set_from_principal_class`; it must pass and bind to this row. |
| A2A-ADM-T14 | GRAPHOS-A2A-R017.4 | Run `tests/a2a/test_admission.py::test_missing_budget_refuses_before_engine_call`; it must pass and bind to this row. |
| A2A-ADM-T15 | GRAPHOS-A2A-R017.5 | Run `tests/a2a/test_service_admission.py::test_send_message_refuses_without_admission`; it must pass and bind to this row. |
| A2A-ADM-T16 | GRAPHOS-A2A-R017.6 | Run `tests/a2a/test_admission_e2e.py::test_message_send_enqueues_envelope_with_harness_config`; it must pass and bind to this row. |
| A2A-ADM-T17 | GRAPHOS-A2A-R018.1 | Run `tests/a2a/test_authority_envelope.py::test_enqueue_passes_harness_config`; it must pass and bind to this row. |
| A2A-ADM-T18 | GRAPHOS-A2A-R018.2 | Run `tests/a2a/test_admission.py::test_envelope_lacking_harness_config_refuses`; it must pass and bind to this row. |
| A2A-ADM-T19 | GRAPHOS-A2A-R020.1 | Run `tests/a2a/test_admission.py::test_abstention_refuses_and_does_not_apply_default`; it must pass and bind to this row. |
| A2A-ADM-T20 | GRAPHOS-A2A-R020.2 | Run `tests/a2a/test_admission.py::test_unavailable_stale_or_partial_harness_answer_refuses`; it must pass and bind to this row. |
| A2A-ADM-T21 | GRAPHOS-A2A-R019.1 | Run `tests/a2a/test_admission.py::test_provenance_names_harness_decision_record`; it must pass and bind to this row. |
| A2A-ADM-T22 | GRAPHOS-A2A-R014.1 | Run `tests/a2a/test_agent_card.py::test_card_has_no_harness_field_before_admission_e2e`; it must pass and bind to this row. |
| A2A-HOLD-T01 | GRAPHOS-A2A-R005.3 | Run `tests/a2a/test_tool_action_admission.py::test_models_are_closed_and_frozen`; it must pass and bind to this row. |
| A2A-HOLD-T02 | GRAPHOS-A2A-R005.4 | Run `tests/a2a/test_tool_action_admission.py::test_admit_returns_engine_verdict_unchanged`; it must pass and bind to this row. |
| A2A-HOLD-T03 | GRAPHOS-A2A-R005.5 | Run `tests/a2a/test_tool_action_admission.py::test_confirm_dispatch_reaches_admitter`; it must pass and bind to this row. |
| A2A-HOLD-T04 | GRAPHOS-A2A-R005.6 | Run `tests/a2a/test_tool_action_admission.py::test_each_refusal_reason_denies`; it must pass and bind to this row. |
| A2A-HOLD-T05 | GRAPHOS-A2A-R005.7 | Run `tests/a2a/test_tool_action_admission.py::test_audit_entry_names_decision_without_values`; it must pass and bind to this row. |
| A2A-HOLD-T06 | GRAPHOS-A2A-R005.8 | Run `tests/a2a/test_tool_action_admission_e2e.py::test_confirm_returns_engine_verdict_and_audit`; it must pass and bind to this row. |
| A2A-HOLD-T07 | GRAPHOS-A2A-R006.3 | Run `tests/a2a/test_held_item_admission.py::test_models_are_closed_and_frozen`; it must pass and bind to this row. |
| A2A-HOLD-T08 | GRAPHOS-A2A-R006.4 | Run `tests/a2a/test_held_item_admission.py::test_program_needs_distinct_verifier_and_required_passes`; it must pass and bind to this row. |
| A2A-HOLD-T09 | GRAPHOS-A2A-R006.5 | Run `tests/a2a/test_held_item_admission.py::test_confirm_dispatch_reaches_held_item_admitter`; it must pass and bind to this row. |
| A2A-HOLD-T10 | GRAPHOS-A2A-R006.6 | Run `tests/a2a/test_held_item_admission.py::test_each_refusal_leaves_item_held`; it must pass and bind to this row. |
| A2A-HOLD-T11 | GRAPHOS-A2A-R006.7 | Run `tests/a2a/test_held_item_admission.py::test_audit_entry_names_verdict_and_item`; it must pass and bind to this row. |
| A2A-HOLD-T12 | GRAPHOS-A2A-R006.8 | Run `tests/a2a/test_held_item_admission_e2e.py::test_confirm_admits_or_rejects_held_item`; it must pass and bind to this row. |

Run `uv run pytest tests/a2a/test_admission.py`, `uv run ruff check graph_os/a2a/admission.py tests/a2a/test_admission.py`, `uv run ruff format --check graph_os/a2a/admission.py tests/a2a/test_admission.py`, and `uv run mypy graph_os/a2a/admission.py`. A2A-ADM-T03 and A2A-ADM-T04 remain open until the EG and AU dependencies (`EG-DECISION-ENGINE-R036`, `AU-CONTROL-R035`) are callable; this PR lands A2A-ADM-T01, A2A-ADM-T02, and A2A-ADM-T06 only. Capture exact commit, command, and result before changing delivery to `ACCEPTED`.

Approval acceptance prerequisites: GRAPHOS-A2A-R005.6 requires full R005.2 and the R006.2/R006.2.1 verified-caller bridge. The served tool-action fixture must refuse before these prerequisites exist. It must prove input-required/signature binding. It must cover replay, changed-params, different-human, expired and revoked-grant cases. R006.5/R006.8 must exercise a dedicated engine held-item port through shared invoke with Surface.A2A. Preserve all fences and assert zero calls to operator-console elevation.

The served confirm fixture rejects missing, unknown and mixed confirmation_kind variants. It routes tool_action and held_item through the same async invoke/authority/audit path. It refuses missing engine policy evidence without a local threshold fallback.

<!-- spec-definition-patch:batch-20261012-tests -->
- `GRAPHOS-A2A-R001.2`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `GRAPHOS-A2A-R022`: Future acceptance: tests/a2a/test_application.py::test_send_message_holds_output_until_bound_critique_passes and tests/a2a/test_application.py::test_send_message_rejects_untyped_preflight_and_failed_critique. Cover accepted typed input, missing/malformed input, absent/mismatched/failed verdict and zero release on refusal. Not run by this spec-only change.
<!-- /spec-definition-patch -->

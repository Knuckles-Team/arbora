# ARBORA-RELEASE — Test specification

Governing [spec](spec.md). Evidence remains PENDING.

| Test ID | Requirement | Level | Fixture and input | Expected observation |
|---|---|---|---|---|
| T-RL-01 | RL-01 | Contract | Signed candidate with full revisions and `sha256` artifacts | Canonical manifest digest and reviewable dry-run graph. |
| T-RL-02 | RL-01 | Negative | Mutable tag, malformed digest, bad signature, duplicate component | Refusal before apply with stable error code and no target mutation. |
| T-RL-03 | RL-02 | Unit | Engine → Arbora → WebUI and optional connector edges | Stable topological order; disabled optional component excluded. |
| T-RL-04 | RL-02 | Negative | Missing required predecessor or cycle | Refusal identifies offending edge; no partial rollout. |
| T-RL-05 | RL-03 | Integration | Disposable target with matching predecessor digests and probes. | Each dependent starts only after predecessor readiness and function pass. |
| T-RL-06 | RL-03 | Negative | Stale image, incompatible API, failing function or timeout | Dependent not started; exact stage failure receipt. |
| T-RL-07 | RL-04 | Served | Installed wheel, engine binary, local identity fixture, MCP/REST | Local canary and separate authenticated/denied served receipts tied to candidate. |
| T-RL-08 | RL-05 | Fault injection | Later stage fails after prior stage passes | Scheduling stops. Reversible previous digest restored and reprobed, or recovery pending. |
| T-RL-09 | RL-06 | CI and release | Clean checkout fixture and hosted release job | PR job needs no private service. Release job attaches digest, integration and consumer evidence. |
| T-RL-10 | RL-07 | Contract | Candidate with fixture secret references and tokens. | Receipt has digests, stage order, time and public evidence URL. It has no credential or private endpoint. |
| T-RL-11 | ARBORA-RELEASE-R003.9 | Served | Two-connector fleet fixture: one healthy connector and one stale pack | One PASS and one FAIL with cause `(d) pack stale`. The promotion step refuses. |
| T-RL-12 | ARBORA-RELEASE-R003.4.6 | Served | Mixed plan, then a call against an old pin | Calls reach the engine unchanged. Engine refusals pass through. The old pin refuses `UNAVAILABLE`. |
| T-RL-13 | ARBORA-RELEASE-R003.4.5 | Served | A permitted tool, an unscoped tool and a replayed record | The pipeline runs under caller authority. Refusal is typed. The replay repeats no effect. Each call has one audit event. |
| T-RL-14 | ARBORA-RELEASE-R003.4.8 | Served | Four step outcomes and a secret argument | Each step lists arguments, result, outcome, key and audit reference. The secret is masked. |
| T-RL-15 | ARBORA-RELEASE-R003.4.9 | Integration | `handle_plan_run` over the registry | The governed caller is bound to the invoke pipeline. No direct gateway call happens. |
| T-RL-16 | ARBORA-RELEASE-R003.4.10 | Served | Prompt step, then two tool steps on two fixture connectors, bound by path | Data passes by binding. A missing path fails the step with no model retry. |
| T-RL-17 | ARBORA-RELEASE-R003.4.12 | Served (planned) | `tests/api/test_child_message_served.py::test_retry_delivers_at_most_once`: active child, duplicate key, conflicting content, stale revision | One safe-boundary delivery, one receipt, no new root run. |
| T-RL-18 | ARBORA-RELEASE-R003.4.13 | Served (planned) | `tests/api/test_child_continuation_served.py::test_retry_starts_one_continuation`: completed child, repeated tuple, changed history | One new run, same receipt on replay, conflict on changed history. |
| T-RL-19 | ARBORA-RELEASE-R003.2 | Integration (planned) | `tests/release/test_exit_ingestion_receipts.py::test_sync_receipt_reads_back_through_ask` and `tests/deployment/test_exit_criteria.py::test_shipped_matrix_has_seven_rows_that_resolve_to_collected_tests` | Sync receipt reads back; each of the seven matrix rows resolves to a collected test. |
| T-RL-20 | ARBORA-RELEASE-R003.3 | Served (planned) | `tests/release/test_exit_query_proofs.py::test_uql_and_nl_questions_answer_from_ingested_facts` over HTTP and MCP | Answers carry provenance; an unsupported question returns the engine abstention unchanged. |
| T-RL-21 | ARBORA-RELEASE-R003.5 | Served (planned) | `tests/release/test_exit_connector_certification.py::test_certified_connector_onboards_and_the_engine_holds_its_pack` | A certified fixture connector onboards and `fleet.packs.status` shows its pack; an uncertified one is refused. |
| T-RL-22 | ARBORA-RELEASE-R003.6 | Served (planned) | `tests/release/test_exit_write_back_receipt.py::test_confirmed_commit_returns_the_engine_journal_receipt` | Preview, confirm and commit give one journal receipt; a repeated key runs no second effect. |
| T-RL-23 | ARBORA-RELEASE-R003.7 | Served (planned) | `tests/release/test_exit_login_probe.py::test_browser_login_through_the_issuer_reaches_a_verified_session` with the loopback issuer | Login reaches a verified session. |
| T-RL-24 | ARBORA-RELEASE-R003.8 | Served (planned) | `tests/release/test_exit_typed_abstention.py::test_capability_question_with_no_agent_returns_typed_abstention` | The reply carries `uncovered_capability` unchanged. |
| T-RL-25 | ARBORA-RELEASE-R003.10 | Integration (planned) | `tests/deployment/test_exit_criteria_served.py::test_release_plan_refuses_unmet_obligation` through the real CLI with a junit file | Exit 1 with `EXIT_CRITERIA_UNMET` and the missing obligation id; skipped tests count as missing. |

## Verification record

For each command and served probe, record exact source commit, candidate manifest digest, artifact digest, environment class, timestamp, CI URL and pass/fail. Run focused manifest/order/rollback tests, full applicable Python checks, cccc/KISS/Dupehound/jscpd gates, build/wheel smoke, and release-only hosted probes. A missing fixture or unavailable hosted environment leaves acceptance open; it does not create a pass.

`ARBORA-RELEASE-R003.4.4`: Start-up publishes every served tenant under the process authority. A request for an unpublished tenant refuses `UNAVAILABLE` and publishes nothing. A caller whose scopes differ from the process reads the published pin.

The pin serialization regression uses the released engine codec on the actual
export after generation, file serialization and parsing. Its digest must stay
unchanged, and outer metadata ordering must be deterministic. Startup rejects
body corruption or reordering even when the declared digest matches the engine.
The startup publisher sends the roundtripped body through the public generated
AgentComponent adapter and retains only the returned definition digest. The
contract test records the transport. It does not prove durable native storage.

The receipt follow-up uses the real Arbora action-policy owner with a recorded
audit backend. A new schema request binds its component, version, digest,
tenant, graph, actor and policy revision. A wrong action or digest, a changed
session, missing scope or failed audit must cause zero publication calls.
Default policy must hold the action. A matching current component reuses its
engine definition without a new write receipt. These are bounded adapter
tests, not native admission, approval-grant or durable run acceptance.
Force an audit ID collision and a write exception separately. Both must yield
no effect-authorizing receipt and zero schema publication calls.
Substituting an older same-intent receipt must also fail, including when a
fresh decision holds or denies the request. Check the audit ID and disposition.

## Child interaction acceptance (pending)

- `ARBORA-RELEASE-R003.4.12`: exercise the served invoke path with exact child targeting, duplicate keys, conflicting content, stale lifecycle, completion/cancellation race and queued-versus-applied receipts. Observe one safe-boundary delivery, no new root, no authority widening and no budget reset.
- `ARBORA-RELEASE-R003.4.13`: continue a completed grandchild through installed owner history and AU; assert one new run, explicit predecessor, correct child-only history, fresh turn counters and enclosing ceilings. A fake context ID, foreign child, failed/cancelled state, unsupported checkpoint or mismatched history revision produces no model/tool effect.

## Row coverage

| Row | Test |
|---|---|
| `ARBORA-RELEASE-R003.4.3` | `tests/architecture/test_dependency_manifest_pins_published_packages.py::test_manifest_declares_each_split_repository_package` and `tests/api/test_plan_run_ops.py::test_engine_without_task_plan_refuses_unavailable` |

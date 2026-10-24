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

## Verification record

For each command and served probe, record exact source commit, candidate manifest digest, artifact digest, environment class, timestamp, CI URL and pass/fail. Run focused manifest/order/rollback tests, full applicable Python checks, cccc/KISS/Dupehound/jscpd gates, build/wheel smoke, and release-only hosted probes. A missing fixture or unavailable hosted environment leaves acceptance open; it does not create a pass.

`ARBORA-RELEASE-R003.4.4`: Warmup without a running loop records the cause. Publication waits for the first served request.

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

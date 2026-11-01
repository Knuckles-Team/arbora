# Messaging channel supervision lifecycle — tasks

See [design](plan.md) and [tests](test-spec.md). Generated delivery evidence is recorded in the computed status (`pipelines-hook spec-status`).

- [x] T001 ARBORA-MESSAGING-R001.1 Define `ChannelSupervisionState`, the legal-transition table, and `transition()`/`IllegalSupervisionTransition` in `arbora/messaging/supervision.py` (`ARBORA-MESSAGING-R001.1`).
- [x] T002 ARBORA-MESSAGING-R001.1 Cover every legal transition and a representative illegal set in `tests/messaging/test_supervision.py` (`MSG-T01`, `MSG-T02`).
- [ ] T003 ARBORA-MESSAGING-R001 Wire `InboundRouter.start()`/`_supervise_backend()`/`stop()` to call `transition()` and expose a per-backend state read (`ARBORA-MESSAGING-R001` rollup, `ARBORA-MESSAGING-R002`, `ARBORA-MESSAGING-R004`).
- [ ] T004 ARBORA-MESSAGING-R005.1 Wire `MessagingRegistry.create_all_enabled` to record a typed `DEGRADED` entry for a missing/import-failed adapter instead of only logging (`ARBORA-MESSAGING-R005.1`).
- [ ] T005 ARBORA-MESSAGING-R003 Add the backoff-sequence and healthy-reset tests against the live router (`ARBORA-MESSAGING-R003`).
- [ ] T006 ARBORA-MESSAGING-R006 Add the static import-boundary check (`ARBORA-MESSAGING-R006`, `MSG-T08`).
- [ ] T007 ARBORA-MESSAGING-R005.1 Preserve and serve the typed missing-adapter behavior from MessagingRegistry.create_all_enabled; prove other configured channels still start (ARBORA-MESSAGING-R005.1, MSG-T07).
- [ ] T008 ARBORA-MESSAGING-R001.1 Compose the operator-enabled messaging projection into authenticated dashboard and graph_configure(action="health") responses while preserving detail-free unauthenticated probes (MSG-T09, MSG-T12).
- [ ] T009 ARBORA-MESSAGING-R001.1 Prove the configured/disabled/unconfigured/unavailable state matrix, safe reason codes, and alert-eligibility rule through the real served health surface (MSG-T10, MSG-T11).
- [ ] T010 ARBORA-MESSAGING-R001.1 Gate component-health alerts on alert_eligible. Disabled credentials never cause alerts; enabled configuration failures remain eligible.
- [ ] T011 ARBORA-MESSAGING-R001.1 Run acceptance at a merged revision. Record actual test and consumer evidence in generated status.json.
- [ ] T012 ARBORA-MESSAGING-R700.1 - Memory webhook subscription operations. Deliver as written in requirements.md.
- [ ] T013 ARBORA-MESSAGING-R700.2 - Signed delivery with retries. Deliver as written in requirements.md.
- [ ] T014 ARBORA-MESSAGING-R700.3 - Outbox pump from the engine. Deliver as written in requirements.md.
- [ ] T015 ARBORA-MESSAGING-R700.4 - Refusal, evidence and hot-path proof. Deliver as written in requirements.md.
- [ ] T016 ARBORA-MESSAGING-R002 Expose the health-gated start result through the per-backend state read.
- [ ] T017 ARBORA-MESSAGING-R004 Prove graceful stop drains through the stopping state, including cancellation during backoff.

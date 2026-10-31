# Messaging channel supervision lifecycle — tasks

See [design](plan.md) and [tests](test-spec.md). Generated delivery evidence is recorded in the computed status (`pipelines-hook spec-status`).

- [x] Define `ChannelSupervisionState`, the legal-transition table, and `transition()`/`IllegalSupervisionTransition` in `arbora/messaging/supervision.py` (`ARBORA-MESSAGING-R001.1`).
- [x] Cover every legal transition and a representative illegal set in `tests/messaging/test_supervision.py` (`MSG-T01`, `MSG-T02`).
- [ ] Wire `InboundRouter.start()`/`_supervise_backend()`/`stop()` to call `transition()` and expose a per-backend state read (`ARBORA-MESSAGING-R001` rollup, `ARBORA-MESSAGING-R002`, `ARBORA-MESSAGING-R004`).
- [ ] Wire `MessagingRegistry.create_all_enabled` to record a typed `DEGRADED` entry for a missing/import-failed adapter instead of only logging (`ARBORA-MESSAGING-R005.1`).
- [ ] Add the backoff-sequence and healthy-reset tests against the live router (`ARBORA-MESSAGING-R003`).
- [ ] Add the static import-boundary check (`ARBORA-MESSAGING-R006`, `MSG-T08`).
- [ ] Preserve and serve the typed missing-adapter behavior from MessagingRegistry.create_all_enabled; prove other configured channels still start (ARBORA-MESSAGING-R005.1, MSG-T07).
- [ ] Compose the operator-enabled messaging projection into authenticated dashboard and graph_configure(action="health") responses while preserving detail-free unauthenticated probes (MSG-T09, MSG-T12).
- [ ] Prove the configured/disabled/unconfigured/unavailable state matrix, safe reason codes, and alert-eligibility rule through the real served health surface (MSG-T10, MSG-T11).
- [ ] Gate component-health alerts on alert_eligible. Disabled credentials never cause alerts; enabled configuration failures remain eligible.
- [ ] Run acceptance at a merged revision. Record actual test and consumer evidence in generated status.json.
- [ ] `ARBORA-MESSAGING-R700.1`: Memory webhook subscription operations. Deliver as written in requirements.md.
- [ ] `ARBORA-MESSAGING-R700.2`: Signed delivery with retries. Deliver as written in requirements.md.
- [ ] `ARBORA-MESSAGING-R700.3`: Outbox pump from the engine. Deliver as written in requirements.md.
- [ ] `ARBORA-MESSAGING-R700.4`: Refusal, evidence and hot-path proof. Deliver as written in requirements.md.

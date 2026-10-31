# ARBORA-ACP — Implementation tasks

Governing [spec](spec.md) and [plan](plan.md).

- [ ] Inventory existing `arbora.identity` principal/session types and status/doctor reporting; record exact reusable interfaces.
- [x] `R001.1`: implement the typed ACP session-policy model and admission check (unauthenticated, over-limit and `chat_adapter_unavailable` refusals) with tests. No transport wiring in this slice.
- [ ] `R001.2`: wire the admission check behind the actual ACP endpoint once meristem's chat adapter is reachable.
- [ ] `R001.3`: wire idle/expiry policy into status/doctor reporting.
- [ ] Capture exact revision and focused-test evidence; update `status.json` only after the corresponding proof exists.

## Decomposition children (tracked)

- [x] **ARBORA-ACP-R001:** Conversational ACP gateway session admission and policy
- [ ] **ARBORA-ACP-R001:** Conversational ACP gateway session admission and policy
- [ ] **ARBORA-ACP-R001.1:** Remaining scope of ARBORA-ACP-R001 (slice .1): Conversational ACP gateway session admission and policy
- [ ] **ARBORA-ACP-R001.2:** Remaining scope of ARBORA-ACP-R001 (slice .2): Conversational ACP gateway session admission and policy

## Coding session admission (R001.3 to R001.8)

- [x] **ARBORA-ACP-R001.3:** Model for coding-session request, grant and admitted session
- [ ] **ARBORA-ACP-R001.4:** Behavior for trusted-root and permission rules
- [ ] **ARBORA-ACP-R001.5:** Wiring of `session/new` into the gateway route registration
- [ ] **ARBORA-ACP-R001.6:** Typed refusals for root, capability, permission and client
- [ ] **ARBORA-ACP-R001.7:** Evidence record for admission and forwarded requests
- [ ] **ARBORA-ACP-R001.8:** Hot-path proof through `session/new` with a fake client
- [ ] **ARBORA-ACP-R001.9:** Serve authenticated duplex reads and non-executing previews; qualify with an isolated real client.

- [ ] **ARBORA-ACP-R001.9.1:** Qualify the shared ACP SDK projection through real stdio and installed AU producer.
- [ ] **ARBORA-ACP-R001.9.2:** Register and qualify the authenticated, bounded experimental HTTP/SSE transport.
- [x] Bind the central source projection through HostedInvocation `agents.run`, retaining
  trusted ACP transport provenance separately from message metadata, existing
  native admission, idempotency, tools and exact admitted operation identifiers.
- [ ] Qualify live governed cancellation across admission, active work, outcome
  commit and disconnect; source owner-port tests are not live durable storage
  receipts. Retain same-owner history as a separate blocker to continuation.
- [ ] Qualify callback settlement with the live owner before production enablement.
  The source path cancels through the owner and checks readback. Failed settlement remains an explicit blocker.

- [x] **ARBORA-ACP-R001.9.3:** Qualify the opt-in central first-turn source path
  through actual admission, dependency execution and committed final answer.
- [x] Test owner cancellation and readback before/after admission, uncertain
  outcomes, nonterminal refusal and same-identity bearer renewal.
- [ ] Qualify the complete installed wheel pairing and live owner settlement.
- [ ] Compose central same-owner history and qualify continuation; do not infer
  it from the local process history or committed task event rows.

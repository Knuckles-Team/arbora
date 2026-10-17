# ARBORA-ACP — Implementation tasks

Governing [spec](spec.md) and [plan](plan.md).

- [ ] Inventory existing `arbora.identity` principal/session types and status/doctor reporting; record exact reusable interfaces.
- [x] `R001.1`: implement the typed ACP session-policy model and admission check (unauthenticated, over-limit and `chat_adapter_unavailable` refusals) with tests. No transport wiring in this slice.
- [ ] `R001.2`: wire the admission check behind the actual ACP endpoint once agent-utilities' chat adapter is reachable.
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

# ARBORA-ACP — Implementation tasks

Governing [spec](spec.md) and [plan](plan.md). Rows: [requirements.md](requirements.md).

Each task line reads `- [ ] T001 [P] [US1] <ID>-R012.4 description`. Tasks run producer first.

- [x] T001 [US1] ARBORA-ACP-R001.1 Implement the typed ACP session-policy model and admission check with the unauthenticated, expiry and `chat_adapter_unavailable` refusals.
- [x] T002 [US1] ARBORA-ACP-R001.3 Define the frozen coding-session request, grant and admitted types in `arbora/acp/coding_session.py`.
- [ ] T003 [US1] ARBORA-ACP-R001.4 Add `decide_effect` with the workspace trust and permission rules.
- [ ] T004 [US1] ARBORA-ACP-R001.6 Add `PERMISSION_DENIED` and return every typed reason as ACP error -32001 before any forwarded request.
- [ ] T005 [US1] ARBORA-ACP-R001.5 Add `_EffectTransport` so the effect path reaches the session policy.
- [ ] T006 [US1] ARBORA-ACP-R001.7 Emit one trace record per admission and capability request.
- [ ] T007 [US1] ARBORA-ACP-R001.8 Prove the coding session over the registered route with a fake connected client.
- [x] T008 [US2] ARBORA-ACP-R001.9.1 Project admitted AU runs through the ACP SDK over stdio.
- [x] T009 [US2] ARBORA-ACP-R001.9.2 Mount the authenticated, bounded HTTP/SSE profile on the gateway.
- [x] T010 [US3] ARBORA-ACP-R001.9.3 Project the governed central final answer through `agents.run`.

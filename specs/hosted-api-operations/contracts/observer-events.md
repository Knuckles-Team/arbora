# Observer events

Seam between the engine observer log and the arbora events read and emit path. The engine stores and pages the events. Arbora appends its producers' events and serves the read.

Provider: medulla MEDULLA-IDENTITY-R023.16
Provider: medulla MEDULLA-FEDERATED-QUERY-R745
Provider: medulla MEDULLA-DECISION-ENGINE-R902
Consumer: arbora ARBORA-OPS-R960.1
Consumer: arbora ARBORA-OPS-R960.2
Consumer: arbora ARBORA-OPS-R960.12

## Wire shape

- `ObserverAppend { kind, subject?, state?, refusal?, attrs }` returns `{ seq }`. Scope `events:write`.
- `ObserverList { kinds?, after_seq?, limit? }` returns `ObserverPage { events: [{ seq, kind, at_ms, subject, state, refusal, attrs }], next_after_seq }`. Scope `events:read`. `limit` is 1 to 500, default 200.
- `kind` is one of `admission`, `release_gate`, `ci_gate`, `alert`, `security_audit`, `remediation`, `evolution_proposal`, `a2a_handoff`, `sandbox`, `query`, `decision`.
- `attrs` holds at most 16 string entries. No key may contain `secret`, `token`, `password` or `credential`.
- The engine itself appends `query` events (R745) and `decision` events (R902). Arbora appends the other nine kinds.

## Refusal codes

- `INVALID_ARGUMENT`: a field breaks a limit, or an attribute key is secret-named.
- `CURSOR_EXPIRED`: `after_seq` is below the retained window. The detail holds the first retained sequence. Arbora passes it through.
- `SCOPE_REQUIRED`: the caller lacks the scope. The refusal happens at the engine dispatch chokepoint.

## Authoritative types

- `crates/medulla-protocol-types/src/result_contract/observer.rs` in medulla (planned by R023.16).
- `arbora/api/ops/events.py` and `arbora/observer/emit.py` (planned by R960.1 and R960.2).

## Test vectors

- `tests/identity/test_observer_log_served.py` in medulla.
- `tests/api/test_observer_events_served.py`.

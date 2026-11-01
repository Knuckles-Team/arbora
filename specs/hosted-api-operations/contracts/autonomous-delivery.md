# Autonomous delivery

Seam between `ARBORA-OPS` and Medulla. Owner rows: `MEDULLA-SDLC-R019.14` in the medulla repository and `ARBORA-OPS-R896.7` in this repository. The medulla contract `specs/development-lifecycle/contracts/autonomous-delivery.md` holds the full method table.

Provider: medulla MEDULLA-SDLC-R019.14
Consumer: arbora ARBORA-OPS-R896.7

## Wire shape

| Operation | Engine method | Answer |
|---|---|---|
| `development.delivery.list` | `DeliveryList{tenant, repo?, stage?}` | `[{delivery_id, stage, scope_rows[], proposals[]}]` |
| `development.delivery.status` | `DeliveryStatus{delivery_id}` | `{stage, mandate, proposals[], assignments[], attention?, ended?}` |
| `development.delivery.stop` | `DeliveryStop{delivery_id, principal, reason}` | `{outcome: stopped}` |
| `development.delivery.retry` | `DeliveryRetry{delivery_id, principal}` | `{assignment_id}` |
| `development.delivery.answer` | `DeliveryFeedback` with a `conversation_comment` event from the principal | `{event_id, deliveries[]}` |

## Refusal codes

- `DELIVERY_NOT_FOUND`: the delivery is unknown or belongs to another tenant.
- `PRINCIPAL_NOT_HUMAN`: a stop or an answer came from a non-human principal.
- `PERMISSION_DENIED`: the caller lacks `source:write` or `source:read`.

## Authoritative types

- medulla `crates/medulla-source-types/src/delivery.rs`: `Delivery` and `DeliveryReportState`.
- `arbora/api/ops/development.py`: the delivery handlers.

## Test vectors

- `tests/api/test_delivery_ops_served.py::test_delivery_ops_read_and_steer_without_local_state`.

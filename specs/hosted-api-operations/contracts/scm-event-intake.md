# Code-host event intake

Seam between `ARBORA-OPS`, the scion-sdk SCM contract and the code-host connectors. Owner rows: `SCION-SCM-R003.2` in scion-sdk, the code-host connector rows that `MEDULLA-SDLC-R019` lists, `MEDULLA-SDLC-R019.5` in medulla and `ARBORA-OPS-R896.8` and `ARBORA-OPS-R896.9` in this repository.

Provider: scion-sdk SCION-SCM-R003.2
Consumer: arbora ARBORA-OPS-R896.8

## Wire shape

The route `POST /api/v1/development/scm-events/{connector}` receives the raw webhook body and headers. The gateway identity middleware admits it by signature, delivery id and timestamp skew as one more admission method (the source's configured platform service principal), and the route runs through the invoke pipeline as `development.scm.event`. It calls the connector normalizer through `fleet.call` and receives a list of `ScmFeedbackEvent{event_id, provider, kind, proposal, actor, actor_kind, at, body_digest}`. It stamps `admitted_principal` on a person's event when the principal holds `source:write`. It sends each event to `DeliveryFeedback`. The polling fallback calls the connector tool `<connector>_list_scm_events{repository, number, cursor, limit}` -> `{events: [ScmFeedbackEvent], next_cursor}` with a stored cursor when no event arrived for 120 s per followed proposal.

## Refusal codes

- A refused admission answers the one uniform 401; the audit event carries `SIGNATURE_MISSING`, `SIGNATURE_INVALID`, `TIMESTAMP_SKEW`, `DELIVERY_ID_MISSING`, `DELIVERY_REPLAYED` or `SOURCE_UNKNOWN`.
- `SCM_SIGNATURE_INVALID`: the normalizer refused the signature or the caller was not admitted by signature.
- `SCM_EVENT_UNSUPPORTED`: the normalizer knows no mapping for the event type.
- `FEEDBACK_EVENT_INVALID`: the engine refused the normalized event.

## Authoritative types

- scion-sdk change-proposal records of `SCION-SCM-R003.1`.
- `arbora/api/ops/development_scm.py`: `handle_scm_event` and `admit_actor`.

## Test vectors

- `tests/development/test_scm_intake_served.py::test_signed_event_reaches_delivery_feedback_and_bad_signature_refuses`.
- `tests/development/test_scm_intake_served.py::test_only_principals_with_source_write_are_stamped`.

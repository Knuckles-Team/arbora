# Workstream record

Seam between `ARBORA-HARNESS-PLUGINS` and medulla. Owner rows: `MEDULLA-IDENTITY-R723` in medulla and `ARBORA-HARNESS-PLUGINS-R004.2` in this repository.

Provider: medulla MEDULLA-IDENTITY-R723
Consumer: arbora ARBORA-HARNESS-PLUGINS-R004.2

## Wire shape

Arbora writes one `Workstream` node with id `workstream:<tenant>:<workspace_digest>` and the properties `tenant`, `principal`, `workspace_digest`, `opened_at_ms` and `last_active_ms`. Arbora writes one `attachedTo` edge from `client_session:<tenant>:<session_id>` to the workstream, with the edge properties `harness`, `attached_at_ms` and `detached_at_ms`. A checkpoint is one `ConversationTurn` written through `MemoryRemember` with the workstream id as the session id.

## Refusal codes

- `PRINCIPAL_MISMATCH`: the write names a principal other than the verified caller.
- `NOT_FOUND`: the workstream is absent or belongs to another principal; arbora maps it to `WORKSTREAM_NOT_FOUND`.

## Test vectors

- `tests/api/ops/test_workstreams_served.py::test_two_harnesses_share_one_workstream`.

# Terminal peer

Seam between `ARBORA-HARNESS-PLUGINS` and tuig. Owner rows: `TUIG-RUNTIME-R013` in tuig and `ARBORA-HARNESS-PLUGINS-R004.2` in this repository.

Provider: arbora ARBORA-HARNESS-PLUGINS-R004.2
Consumer: tuig TUIG-RUNTIME-R013

## Wire shape

The terminal client calls `workstream.attach` with harness `tuig`, its session ID and its workspace root. It renders the returned context. It calls `workstream.checkpoint` with trigger `exit` before it closes, and then `workstream.detach`.

## Refusal codes

- `AUTH_REQUIRED`: the client has no valid credential; it shows the sign-in hint.
- `WORKSTREAM_NOT_FOUND`: the read names a workstream that the caller does not own.

## Test vectors

- `tests/api/ops/test_workstreams_served.py::test_two_harnesses_share_one_workstream`.

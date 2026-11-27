# Seam: host-log stream

Provider: arbora ARBORA-HOST-R009.8.2.2.2
Consumer: overstory OVERSTORY-APPS-R054

## Wire shape

`register_host_log_routes` serves `POST /host/logs/reference` and `POST /host/logs/read`. Both take exactly one bearer header and refuse a cookie or query string. A version-2 read answers `{version: 2, records, cursor, gap}`; each record carries the original `host_instance`, `sequence`, `request`, `timestamp`, `severity` and `message`.

## Refusal codes

`409 retention_gap` (stale cursor), `403 workspace_denied` (revoked or refused grant) and `503 log_unavailable`. Each answers a fixed `{detail: <code>}` with no log content, metadata or cursor. A fresh read after rotation answers 200 with `gap: true` and never replays rotated-out text.

## Authoritative types

`LogReadRequest` and `_metadata` in `arbora/agent_host/log_routes.py`. The consumer's copy of the shape is `src/lib/host-logs.ts` in overstory, and its full contract is `specs/arbora-app-surface/contracts/host-logs.md` there.

## Test vectors

`tests/agent_host/test_overstory_logs_served.py::test_retention_gap_clears_protected_content` holds the HTTP vectors. The consumer replays them in a real browser in `e2e/host-log-view.browser.ts` of overstory.

# Seam: host-log stream

Provider: arbora ARBORA-HOST-R009.8.2.2.2
Consumer: overstory OVERSTORY-APPS-R054

## Wire shape

`register_host_log_routes` serves `POST /host/logs/reference` and `POST /host/logs/read`. Both are POST and take exactly one credential and no query string: one `Authorization: Bearer` header (non-browser callers), or the signed-in browser session cookie `__Host-arbora_session` with an `Origin` equal to the configured public origin and the session-bound `X-CSRF-Token` (the browser-session proof in `arbora/identity/browser.py`). A cookie beside a bearer header is refused. The cookie value is the session credential the grant check resolves, so a revoked grant refuses a browser session as it does a bearer. A version-2 read answers `{version: 2, records, cursor, gap}`; each record carries the original `host_instance`, `sequence`, `request`, `timestamp`, `severity` and `message`.

## Refusal codes

`409 retention_gap` (stale cursor), `403 workspace_denied` (revoked or refused grant, a cookie without a valid CSRF token or exact origin, a cookie with a bearer, or a query string) and `503 log_unavailable`. Each answers a fixed `{detail: <code>}` with no log content, metadata or cursor. A fresh read after rotation answers 200 with `gap: true` and never replays rotated-out text.

## Authoritative types

`LogReadRequest` and `_metadata` in `arbora/agent_host/log_routes.py`. The consumer's copy of the shape is `src/lib/host-logs.ts` in overstory, and its full contract is `specs/arbora-app-surface/contracts/host-logs.md` there.

## Test vectors

`tests/agent_host/test_overstory_logs_served.py::test_retention_gap_clears_protected_content` holds the bearer HTTP vectors and `::test_browser_session_reads_logs_with_csrf_and_loses_them_on_revocation` the browser-session vectors. The consumer replays them in a real browser in `e2e/host-log-view.browser.ts` of overstory.

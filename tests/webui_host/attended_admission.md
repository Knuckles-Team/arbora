# Attended admission qualification

This probe records refusals from the actual canonical middleware at a trusted local Git revision.
It executes that module against installed dependencies without copying product auth files.
The selected revision must contain `arbora/webui_host/session_boundary.py` and `CanonicalWebUIBoundaryMiddleware`.
A branch without that module needs a locally available revision that contains it.
The probe does not fetch revisions, change settings, issue credentials or grant authority.

Run from an Arbora checkout with its locked test dependencies installed:

```bash
uv run --no-sync python tests/webui_host/qualify_attended_admission.py \
  --repository . --revision main --output /tmp/attended-admission.json
```

Use only trusted revisions because the probe executes their module source.
The output records the resolved commit, module digest and actual ASGI responses.
The downstream sentinel fails if any protected request reaches an application.
Expected refusals are HTTP 503 for arm routes and WebSocket close code 4401.
A changed refusal fails the probe and needs review; it never becomes a successful admission claim.

## Missing owner contract

The canonical boundary currently admits auth routes, canonical operations and public shell requests.
Its protected legacy routes refuse, including the attended arm and browser socket paths.
`BrowserAdmission` currently accepts HTTP scopes; a WebSocket handshake must not be relabeled as HTTP.
The auth owner must provide qualified admission and session continuity before the rendered journey can pass.

The existing WebUI client uses these same-origin endpoints:

- `GET /api/browser-control/attended-arm` returns `none`, `recent-auth` or `armed` readiness.
- `POST /api/browser-control/attended-arm` accepts `{route_id, next?}` and returns `{authorization_url, expires_at}`.
- `POST /api/browser-control/attended-arm/finalize` accepts `{route_id, registration_generation, catalog_digest, tools: [{tool_id, schema_digest}]}`.
- Finalization returns `armed` with server-computed `tool_scope_digest`, exact registration fields and expiry.
- `DELETE /api/browser-control/attended-arm` revokes the attended authority.

The server owns the HttpOnly receipt and binds its subject, tenant and login session.
Recent authentication must precede finalization and bind the exact catalog and tool scope.
Admission must retain exact-origin, CSRF and live session revalidation checks.
The real `/ws/browser-control` handshake needs qualified cookie admission and continued backchannel revalidation.
Proposal acceptance remains a trusted human gesture.
Mutation confirmation remains a separate exact-request human decision.

This probe establishes route refusal only.
It does not establish authenticated browser acceptance, native WebMCP availability, durable execution or session continuity.
Synthetic socket tests and rendered proposal-layout tests retain their separate evidence limits.

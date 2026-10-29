# Delivery tasks — ARBORA-IDENTITY-ACCESS

State: READY FOR IMPLEMENTATION. Check a task only with an exact commit and test reference in `evidence.md`.

- [ ] **GIA-01 (IA-01/02):** Define engine identity/access DTOs, typed errors, scope classes and test fixture. Implement durable mode read/CAS and local issuer key-store adapter. Prove IT-01.
- [ ] **GIA-02 (IA-03/14):** Add identity ports and one serving composition root. Route `none` bootstrap requests through the normal signed engine session. Enforce bind/Host/Origin policy and warning surfaces. Prove IT-02 and IT-13.
- [ ] **GIA-03 (IA-04):** Implement local account and session ceremonies with hashed server-side IDs, CSRF, rate limits, rotation, revocation and uniform errors. Prove IT-03.
- [ ] **GIA-04 (IA-05):** Add TOTP, WebAuthn, recovery-code and group-MFA policies while preserving action step-up. Prove IT-04.
- [ ] **GIA-05 (IA-06/07):** Build identity self/admin operations and service/API key path; wire REST/MCP/CLI to the same authority. Refuse to advertise unavailable engine-backed methods. Prove IT-05/06.
- [ ] **GIA-06 (IA-12/13):** Build access request, approval, lease, check and explanation service with exact-scope registry and fail-closed fleet/unknown-tool checks. Prove IT-11/12.
  - Partial (ARBORA-IDENTITY-R019): `arbora.access.service`/`arbora.api.ops.access` registers `access.elevation.request/list/revoke/approve`, `access.leases.list/get`, `access.check` and `access.explain_policy` against the installed EG scope contract. `approvals.list/get/grant/deny` are implemented in the same service module but not yet registered as operations: the installed EG contract does not publish `approvals:read`/`approvals:decide` scopes. Follow-up: register those four operations once EG adds the two scopes.
- [ ] **GIA-07 (IA-08/09/15):** Add mockable OIDC, LDAP, SCIM, brokered SAML and optional SMTP adapters with strict validation, source-tagged memberships and deprovisioning. Prove IT-07/08/09.
- [ ] **GIA-08 (IA-10):** Add `arbora identity` commands, configuration defaults, doctor diagnostics and public operator guidance within this repository's GitHub Pages source. Prove CLI portions of IT-09.
- [ ] **GIA-09 (IA-11):** Create and run the three-mode decision table and owned-data transition round trip. Use it across the engine and Arbora contract fixture. Prove IT-10.
- [ ] **GIA-09A (IA-16):** Inventory every Arbora engine client, carry a verified local-issuer or permitted service token through each one, prove allow/deny and no fallback with IT-14, then coordinate removal of the engine authentication opt-out. Record the exact engine and Arbora commits separately.
- [ ] **GIA-10:** Run all portable tests on the exact candidate commit; run Ruff, Mypy, hooks, CCCC, KISS, Dupehound, jscpd, frozen lock and wheel checks. Fix findings without weakening thresholds; record each job in `evidence.md`.
- [ ] **GIA-11:** Merge the reviewed implementation to default branch; record exact commit and re-run acceptance evidence against that commit. Update delivery and acceptance states individually.

## Rapid-delivery split (2026-10-09)

- [x] **GIA-RD-01:** Split `ARBORA-IDENTITY-R004`, `R006`, `R007` into `.1` (typed model +
- [x] **GIA-RD-02:** Split `ARBORA-IDENTITY-R008`, `R009`, `R010` into `.1` (typed model +
- [x] **GIA-RD-03:** Split `ARBORA-IDENTITY-R011`, `R012` into `.1` (typed model +
  validation + refusal tests under `arbora/identity/`) and `.2` (remaining behavior,
  stays `SPECIFIED`). Parents are now rollups. See `requirements.md`/`status.json`.

## Source ID coverage

`ARBORA-IDENTITY-R001`, `ARBORA-IDENTITY-R002`–`ARBORA-IDENTITY-R017`, `ARBORA-IDENTITY-R018`, `ARBORA-IDENTITY-R019`, `ARBORA-IDENTITY-R020`, `ARBORA-IDENTITY-R021`, `ARBORA-IDENTITY-R022`, and `ARBORA-IDENTITY-R023` map to IA requirements in `spec.md`. Source IDs identify intended work, not completion. Cross-repository owners implement their side of the contract stated in this spec. Arbora tasks cannot close their delivery without their own accepted evidence.

## Strict producer follow-through

- [ ] Qualify EG credential-derived context and authoritative source expiry;
  reject bare lookup and missing generated capability.
- [ ] Inject existing verified broker and secret-backed issuer, preserving the
  strict prepared claims and exact scope ceiling without local grants.
- [ ] Qualify EG session reference/expiry/pending/MFA response and atomic rotation.
- [ ] Implement the existing WebUI evidence port with request-local private
  bindings, after-await rechecks and exact C caller-session-instance pairing.
- [ ] Prove cleanup, cancellation, refresh and concurrent request isolation at
  the mounted E/C integration boundary before claiming browser availability.

## Decomposition children (tracked)

- [x] **ARBORA-IDENTITY-R004.1:** Typed loopback bootstrap-principal model with refusal tests
- [x] **ARBORA-IDENTITY-R004.2:** Bootstrap resolution path, Host/Origin validation, unsecured-mode indicators
- [x] **ARBORA-IDENTITY-R006:** Optional MFA with TOTP, WebAuthn, and recovery codes (rollup)
- [x] **ARBORA-IDENTITY-R006.1:** Typed MFA enrollment model with refusal tests
- [x] **ARBORA-IDENTITY-R006.2:** TOTP, WebAuthn, recovery-code ceremonies and group enforcement
- [x] **ARBORA-IDENTITY-R007:** Admin console tabs with mapping dry-run and mode wizard (rollup)
- [x] **ARBORA-IDENTITY-R007.1:** Typed admin-console tab model with refusal tests
- [x] **ARBORA-IDENTITY-R007.2:** Mapping dry-run preview and mode-transition wizard
- [ ] **ARBORA-IDENTITY-R008.1:** Typed API-key grant model with refusal tests
- [x] **ARBORA-IDENTITY-R008.2:** Scope intersection at use time and immediate revocation
- [x] **ARBORA-IDENTITY-R009:** Multi-provider OIDC with mapping rules and JIT policy
- [x] **ARBORA-IDENTITY-R009.1:** Typed OIDC mapping-rule model with refusal tests
- [ ] **ARBORA-IDENTITY-R009.2:** OIDC authentication flow (rollup)
- [ ] **ARBORA-IDENTITY-R009.2.1:** Ordered claim-to-rule evaluation
- [ ] **ARBORA-IDENTITY-R009.2.2:** ID-token validation inputs and typed refusal
- [ ] **ARBORA-IDENTITY-R009.2.3:** Callback handler wiring, link migration, hinted logout
- [ ] **ARBORA-IDENTITY-R009.2.4:** Live IdP probe
- [ ] **ARBORA-IDENTITY-R010:** LDAPS bind with nested group sync
- [ ] **ARBORA-IDENTITY-R010.1:** Typed LDAPS bind-config model with refusal tests
- [ ] **ARBORA-IDENTITY-R010.2:** Directory bind, filter escaping, nested-group sync, deprovisioning (rollup)
- [x] **ARBORA-IDENTITY-R010.2.1:** Group-DN-to-role mapping evaluation with refusal on no match
- [x] **ARBORA-IDENTITY-R010.2.2:** Bind through injected directory port, filter escaping
- [ ] **ARBORA-IDENTITY-R010.2.3:** Scheduled nested-group sync and deprovisioning (rollup)
- [x] **ARBORA-IDENTITY-R010.2.3.1:** Pure directory sync plan with empty-snapshot refusal
- [ ] **ARBORA-IDENTITY-R010.2.3.2:** Schedule the sync and apply the plan through the identity port
- [ ] **ARBORA-IDENTITY-R010.2.4:** Live directory probe
- [ ] **ARBORA-IDENTITY-R011.1:** Typed SCIM service-credential model with refusal tests
- [ ] **ARBORA-IDENTITY-R011.2:** SCIM create/update/patch/deactivate server surface (rollup)
- [x] **ARBORA-IDENTITY-R011.2.1:** Typed SCIM User resource model with refusal tests
- [x] **ARBORA-IDENTITY-R011.2.2:** SCIM credential check via `ScimServiceCredential.authorizes`
- [ ] **ARBORA-IDENTITY-R011.2.3:** SCIM create/patch/deactivate handlers through an injected identity port
- [ ] **ARBORA-IDENTITY-R011.2.4:** SCIM route registration
- [ ] **ARBORA-IDENTITY-R012:** Native SAML service provider (rollup)
- [ ] **ARBORA-IDENTITY-R012.1:** Typed SAML service-provider model with refusal tests
- [ ] **ARBORA-IDENTITY-R012.2:** Assertion verification and SAML sign-in (rollup)
- [ ] **ARBORA-IDENTITY-R012.2.1:** Typed parsed-assertion model and audience/recipient/time-window checks
- [ ] **ARBORA-IDENTITY-R012.2.2:** Signature verification through an injected verifier port
- [ ] **ARBORA-IDENTITY-R012.2.3:** Replay check and sign-in handler wiring
- [ ] **ARBORA-IDENTITY-R012.2.3.1:** Replay check through a seen-assertion-id store port
- [ ] **ARBORA-IDENTITY-R012.2.3.2:** Sign-in handler wiring into the browser authority
- [ ] **ARBORA-IDENTITY-R012.2.4:** Live IdP sign-in probe
- [ ] **ARBORA-IDENTITY-R016:** Optional SMTP adapter for identity email
- [ ] **ARBORA-IDENTITY-R016.1:** Remaining scope of ARBORA-IDENTITY-R016 (slice .1): Optional SMTP adapter for identity email
- [ ] **ARBORA-IDENTITY-R016.2:** Remaining scope of ARBORA-IDENTITY-R016 (slice .2): Optional SMTP adapter for identity email
- [ ] **ARBORA-IDENTITY-R017.1:** Remaining scope of ARBORA-IDENTITY-R017 (slice .1): Remove the unauthenticated opt-out once clients migrate
- [ ] **ARBORA-IDENTITY-R017.2:** Remaining scope of ARBORA-IDENTITY-R017 (slice .2): Remove the unauthenticated opt-out once clients migrate
- [ ] **ARBORA-IDENTITY-R004.2:** Bootstrap resolution path, Host/Origin validation, unsecured-mode indicators
- [ ] **ARBORA-IDENTITY-R006:** Optional MFA with TOTP, WebAuthn, and recovery codes (rollup)
- [x] **ARBORA-IDENTITY-R006.1:** Typed MFA enrollment model with refusal tests
- [ ] **ARBORA-IDENTITY-R006.2:** TOTP, WebAuthn, recovery-code ceremonies and group enforcement
- [ ] **ARBORA-IDENTITY-R007:** Admin console tabs with mapping dry-run and mode wizard (rollup)
- [x] **ARBORA-IDENTITY-R007.1:** Typed admin-console tab model with refusal tests
- [ ] **ARBORA-IDENTITY-R007.2:** Mapping dry-run preview and mode-transition wizard
- [ ] **ARBORA-IDENTITY-R025:** Register identity ops once the engine contract lists `identity:admin`.
- [ ] **ARBORA-IDENTITY-R026:** Hosted sessions are attributed to a client and segmented by principal (rollup).
- [ ] **ARBORA-IDENTITY-R026.1:** Client session record.
- [ ] **ARBORA-IDENTITY-R026.2:** Record at session open.
- [ ] **ARBORA-IDENTITY-R026.3:** Runs carry the session id.
- [ ] **ARBORA-IDENTITY-R026.4:** Audit event per run.
- [ ] **ARBORA-IDENTITY-R026.5:** Caller-scoped client listing.
- [ ] **ARBORA-IDENTITY-R026.6:** User and service account parity.
- [ ] **ARBORA-IDENTITY-R026.7:** Cross-client denial is uniform.

- [ ] **ARBORA-IDENTITY-R023.1:** Model: scoped identity synthesis.
- [ ] **ARBORA-IDENTITY-R023.2:** Behavior: scoped identity synthesis.
- [ ] **ARBORA-IDENTITY-R023.3:** Wiring: scoped identity synthesis.
- [ ] **ARBORA-IDENTITY-R023.4:** Refusal: scoped identity synthesis.
- [ ] **ARBORA-IDENTITY-R023.5:** Evidence: scoped identity synthesis.
- [ ] **ARBORA-IDENTITY-R023.6:** Hot-path proof: scoped identity synthesis.
- [ ] **ARBORA-IDENTITY-R017.3:** Model: scoped identity synthesis.
- [ ] **ARBORA-IDENTITY-R017.4:** Behavior: scoped identity synthesis.
- [ ] **ARBORA-IDENTITY-R017.5:** Wiring: scoped identity synthesis.
- [ ] **ARBORA-IDENTITY-R017.6:** Refusal: scoped identity synthesis.
- [ ] **ARBORA-IDENTITY-R017.7:** Evidence: scoped identity synthesis.
- [ ] **ARBORA-IDENTITY-R017.8:** Hot-path proof: scoped identity synthesis.
- [ ] **ARBORA-IDENTITY-R019.1:** Model: scoped identity synthesis.
- [ ] **ARBORA-IDENTITY-R019.2:** Behavior: scoped identity synthesis.
- [ ] **ARBORA-IDENTITY-R019.3:** Wiring: scoped identity synthesis.
- [ ] **ARBORA-IDENTITY-R019.4:** Refusal: scoped identity synthesis.
- [ ] **ARBORA-IDENTITY-R019.5:** Evidence: scoped identity synthesis.
- [ ] **ARBORA-IDENTITY-R019.6:** Hot-path proof: scoped identity synthesis.

## Additional owner requirements

- [ ] `ARBORA-IDENTITY-R026.8`: Deliver every requirement in requirements.md.
- [x] `ARBORA-IDENTITY-R002.1`: scope registry generated from EG `scopes.json`. Evidence: `tests/security/test_scope_registry.py::test_the_generated_module_is_the_installed_eg_registry`.
- [x] `ARBORA-IDENTITY-R027`: Process authority renews indefinitely; failures log their cause and readiness names the reason. Evidence: `tests/mcp_server/test_process_authority_renewal.py`.
- [x] `ARBORA-IDENTITY-R029`: A run renews its caller's authority while active, bounded and audited; stops on end, cancel or revocation; fan-out inherits. Evidence: `tests/mcp_server/test_caller_run_authority.py`.
- [ ] `ARBORA-IDENTITY-R029.4`: Issued bearers carry the opaque session-reference claim.
- [ ] `ARBORA-IDENTITY-R029.5`: Logout and session-revocation routes call `revoke_caller`.
- [ ] `ARBORA-IDENTITY-R029.6`: The renewal loop registers an engine-backed check via `CALLER_REVOCATIONS.add_check` using `EG-TYPED-PACKS-R085.3`.

## Delegated identity for connector calls (R028)

- [ ] `ARBORA-IDENTITY-R028`: Connector calls that need a user identity carry a delegated identity (rollup).
- [ ] `ARBORA-IDENTITY-R028.1`: Model.
- [ ] `ARBORA-IDENTITY-R028.2`: Behavior.
- [ ] `ARBORA-IDENTITY-R028.3`: Wiring.
- [ ] `ARBORA-IDENTITY-R028.4`: Refusal.
- [ ] `ARBORA-IDENTITY-R028.5`: Evidence.
- [ ] `ARBORA-IDENTITY-R028.6`: Hot-path proof.

## WebUI mode composition extension

- [ ] ARBORA-IDENTITY-R004.7: qualify explicit mode configuration with installed WebUI in none, EG and external OIDC modes. Source mode tests are not release qualification.
- [ ] ARBORA-IDENTITY-R004.4: qualify host admission middleware with a configured issuer/auth route owner, current EG session checks, renewal and WebSocket support. Current HTTP adapter deliberately refuses unsupported cookie WebSocket admission.
- [ ] ARBORA-IDENTITY-R004.5: finish and qualify same-owner HTTP dispatch composition. Source tests cover the bounded non-streaming fence; installed WebUI integration and real effect boundaries remain open. Keep browser streaming unavailable until a task-safe lifetime contract is qualified.
- [ ] ARBORA-IDENTITY-R004.6: migrate explicit anonymous effects through existing governed operations, preserve canonical action binding and containment/confirmation, and prove UI/WebMCP equivalence. Read-only safety scaffolding is not full no-auth support.

## Source-only delivery and open integration children

- [ ] ARBORA-IDENTITY-R004.7.1: Host mode configuration source contract. Source tests exercise typed settings, factory arguments and refusal with an older incompatible WebUI factory.
- [ ] ARBORA-IDENTITY-R004.7.2: Installed identity-mode startup. Installed package startup, provider failure and mode-change probes remain required.
- [ ] ARBORA-IDENTITY-R004.4.1: Bound HTTP adapter source contract. Source fixtures prove routing, cleanup, redaction and refusal; they do not prove deployed login.
- [ ] ARBORA-IDENTITY-R004.4.2: Installed browser identity lifecycle. Real issuer/session and browser lifecycle probes remain required.
- [ ] ARBORA-IDENTITY-R004.5.1: Private same-owner dispatch source contract. Synthetic-owner tests prove bounded non-streaming dispatch, mounted routing, races and cancellation without claiming native effects.
- [ ] ARBORA-IDENTITY-R004.5.2: Canonical WebUI factory safety source contract. Factory source tests use synthetic issuer/runtime facts and one shared-pipeline read. Cover malformed bodies, redaction, rate and capacity refusal.
- [ ] ARBORA-IDENTITY-R004.5.3: Installed and native dispatch qualification. Real effect-boundary, installed provider and streaming qualification remain open.

### Explicit anonymous local mode amendment

- [ ] Add explicit process-selected mode, separate anonymous caller/session provenance, and unsigned EG client composition.
- [ ] Preserve verified carrier rejection, human/service rules, approval scopes, and authenticated-default behavior.
- [ ] Add focused mode/provenance/ASGI/transport tests, including malformed identity and nonlocal/credential request refusals.
- [ ] Validate real native EG persistence, effect journaling, real served Arbora and actual TUI with only the model simulated.
- [ ] Validate separate fresh authenticated stack and missing-token refusal after anonymous acceptance.
- [ ] Complete independent review and upstream CI before merge.

- [ ] ARBORA-IDENTITY-R004.6: Qualify the separate local browser transport and discriminated attended proof before runtime wiring.
- [ ] ARBORA-IDENTITY-R004.6: Prove policy, gesture, exact action binding, revocation and separate mutation confirmation without an identity upgrade.
- [ ] `ARBORA-IDENTITY-R004.8`: Complete all anonymous task access children.
- [ ] `ARBORA-IDENTITY-R004.8.1`: Implement and test the served read contract with synthetic native evidence.
- [ ] `ARBORA-IDENTITY-R004.8.2`: Reconcile native anonymous ownership and qualify installed reads before activation.
- [ ] `ARBORA-IDENTITY-R004.8.3`: Qualify the existing public native client adapter with synthetic transport tests.


- [ ] `ARBORA-IDENTITY-R004.9`: Complete the local anonymous text execution children.
- [ ] `ARBORA-IDENTITY-R004.9.1`: Compose native admission and the received text-only harness in source tests.
- [ ] `ARBORA-IDENTITY-R004.9.2`: Qualify installed local sends and cancellation before activation.
- [ ] `ARBORA-IDENTITY-R004.9.3`: Qualify owner-bound durable answer recovery.

- [ ] `ARBORA-IDENTITY-R004.9.3.1`: Test bounded native event storage and committed-reference recovery in source composition.
- [ ] `ARBORA-IDENTITY-R004.9.3.2`: Qualify installed host and native restart recovery.

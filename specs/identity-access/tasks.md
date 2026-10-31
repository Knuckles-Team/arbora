# Delivery tasks — ARBORA-IDENTITY-ACCESS

State: READY FOR IMPLEMENTATION. Check a task only with an exact commit and test reference in `evidence.md`.

- **GIA-01 (IA-01/02):** Define engine identity/access DTOs, typed errors, scope classes and test fixture. Implement durable mode read/CAS and local issuer key-store adapter. Prove IT-01.
- **GIA-02 (IA-03/14):** Add identity ports and one serving composition root. Route `none` bootstrap requests through the normal signed engine session. Enforce bind/Host/Origin policy and warning surfaces. Prove IT-02 and IT-13.
- **GIA-03 (IA-04):** Implement local account and session ceremonies with hashed server-side IDs, CSRF, rate limits, rotation, revocation and uniform errors. Prove IT-03.
- **GIA-04 (IA-05):** Add TOTP, WebAuthn, recovery-code and group-MFA policies while preserving action step-up. Prove IT-04.
- **GIA-05 (IA-06/07):** Build identity self/admin operations and service/API key path; wire REST/MCP/CLI to the same authority. Refuse to advertise unavailable engine-backed methods. Prove IT-05/06.
- **GIA-06 (IA-12/13):** Build access request, approval, lease, check and explanation service with exact-scope registry and fail-closed fleet/unknown-tool checks. Prove IT-11/12.
  - Partial (ARBORA-IDENTITY-R019): `arbora.access.service`/`arbora.api.ops.access` registers `access.elevation.request/list/revoke/approve`, `access.leases.list/get`, `access.check` and `access.explain_policy` against the installed EG scope contract. `approvals.list/get/grant/deny` are implemented in the same service module but not yet registered as operations: the installed EG contract does not publish `approvals:read`/`approvals:decide` scopes. Follow-up: register those four operations once EG adds the two scopes.
- **GIA-07 (IA-08/09/15):** Add mockable OIDC, LDAP, SCIM, brokered SAML and optional SMTP adapters with strict validation, source-tagged memberships and deprovisioning. Prove IT-07/08/09.
- **GIA-08 (IA-10):** Add `arbora identity` commands, configuration defaults, doctor diagnostics and public operator guidance within this repository's GitHub Pages source. Prove CLI portions of IT-09.
- **GIA-09 (IA-11):** Create and run the three-mode decision table and owned-data transition round trip. Use it across the engine and Arbora contract fixture. Prove IT-10.
- **GIA-09A (IA-16):** Inventory every Arbora engine client, carry a verified local-issuer or permitted service token through each one, prove allow/deny and no fallback with IT-14, then coordinate removal of the engine authentication opt-out. Record the exact engine and Arbora commits separately.
- **GIA-10:** Run all portable tests on the exact candidate commit; run Ruff, Mypy, hooks, CCCC, KISS, Dupehound, jscpd, frozen lock and wheel checks. Fix findings without weakening thresholds; record each job in `evidence.md`.
- **GIA-11:** Merge the reviewed implementation to default branch; record exact commit and re-run acceptance evidence against that commit. Update delivery and acceptance states individually.

## Rapid-delivery split (2026-10-09)

- [x] T001 ARBORA-IDENTITY-R004 GIA-RD-01: Split , `R006`, `R007` into `.1` (typed model +
- [x] T002 ARBORA-IDENTITY-R008 GIA-RD-02: Split , `R009`, `R010` into `.1` (typed model +
- [x] T003 ARBORA-IDENTITY-R011 GIA-RD-03: Split , `R012` into `.1` (typed model +
  validation + refusal tests under `arbora/identity/`) and `.2` (remaining behavior,
  stays `SPECIFIED`). Parents are now rollups. See `requirements.md`/`status.json`.

## Source ID coverage

`ARBORA-IDENTITY-R001`, `ARBORA-IDENTITY-R002`–`ARBORA-IDENTITY-R017`, `ARBORA-IDENTITY-R018`, `ARBORA-IDENTITY-R019`, `ARBORA-IDENTITY-R020`, `ARBORA-IDENTITY-R021`, `ARBORA-IDENTITY-R022`, and `ARBORA-IDENTITY-R023` map to IA requirements in `spec.md`. Source IDs identify intended work, not completion. Cross-repository owners implement their side of the contract stated in this spec. Arbora tasks cannot close their delivery without their own accepted evidence.

## Strict producer follow-through

- Qualify EG credential-derived context and authoritative source expiry;
  reject bare lookup and missing generated capability.
- Inject existing verified broker and secret-backed issuer, preserving the
  strict prepared claims and exact scope ceiling without local grants.
- Qualify EG session reference/expiry/pending/MFA response and atomic rotation.
- Implement the existing WebUI evidence port with request-local private
  bindings, after-await rechecks and exact C caller-session-instance pairing.
- Prove cleanup, cancellation, refresh and concurrent request isolation at
  the mounted E/C integration boundary before claiming browser availability.

## Decomposition children (tracked)

- **ARBORA-IDENTITY-R004.1:** Typed loopback bootstrap-principal model with refusal tests
- **ARBORA-IDENTITY-R004.2:** Bootstrap resolution path, Host/Origin validation, unsecured-mode indicators
- **ARBORA-IDENTITY-R006:** Optional MFA with TOTP, WebAuthn, and recovery codes (rollup)
- **ARBORA-IDENTITY-R006.1:** Typed MFA enrollment model with refusal tests
- **ARBORA-IDENTITY-R006.2:** TOTP, WebAuthn, recovery-code ceremonies and group enforcement
- **ARBORA-IDENTITY-R007:** Admin console tabs with mapping dry-run and mode wizard (rollup)
- **ARBORA-IDENTITY-R007.1:** Typed admin-console tab model with refusal tests
- **ARBORA-IDENTITY-R007.2:** Mapping dry-run preview and mode-transition wizard
- **ARBORA-IDENTITY-R008.1:** Typed API-key grant model with refusal tests
- **ARBORA-IDENTITY-R008.2:** Scope intersection at use time and immediate revocation
- **ARBORA-IDENTITY-R009:** Multi-provider OIDC with mapping rules and JIT policy
- **ARBORA-IDENTITY-R009.1:** Typed OIDC mapping-rule model with refusal tests
- **ARBORA-IDENTITY-R009.2:** OIDC authentication flow (rollup)
- **ARBORA-IDENTITY-R009.2.1:** Ordered claim-to-rule evaluation
- **ARBORA-IDENTITY-R009.2.2:** ID-token validation inputs and typed refusal
- **ARBORA-IDENTITY-R009.2.3:** Callback handler wiring, link migration, hinted logout
- **ARBORA-IDENTITY-R009.2.4:** Live IdP probe
- **ARBORA-IDENTITY-R010:** LDAPS bind with nested group sync
- **ARBORA-IDENTITY-R010.1:** Typed LDAPS bind-config model with refusal tests
- **ARBORA-IDENTITY-R010.2:** Directory bind, filter escaping, nested-group sync, deprovisioning (rollup)
- **ARBORA-IDENTITY-R010.2.1:** Group-DN-to-role mapping evaluation with refusal on no match
- **ARBORA-IDENTITY-R010.2.2:** Bind through injected directory port, filter escaping
- **ARBORA-IDENTITY-R010.2.3:** Scheduled nested-group sync and deprovisioning (rollup)
- **ARBORA-IDENTITY-R010.2.3.1:** Pure directory sync plan with empty-snapshot refusal
- **ARBORA-IDENTITY-R010.2.3.2:** Schedule the sync and apply the plan through the identity port
- **ARBORA-IDENTITY-R010.2.4:** Live directory probe
- **ARBORA-IDENTITY-R011.1:** Typed SCIM service-credential model with refusal tests
- **ARBORA-IDENTITY-R011.2:** SCIM create/update/patch/deactivate server surface (rollup)
- **ARBORA-IDENTITY-R011.2.1:** Typed SCIM User resource model with refusal tests
- **ARBORA-IDENTITY-R011.2.2:** SCIM credential check via `ScimServiceCredential.authorizes`
- **ARBORA-IDENTITY-R011.2.3:** SCIM create/patch/deactivate handlers through an injected identity port
- **ARBORA-IDENTITY-R011.2.4:** SCIM route registration
- **ARBORA-IDENTITY-R012:** Native SAML service provider (rollup)
- **ARBORA-IDENTITY-R012.1:** Typed SAML service-provider model with refusal tests
- **ARBORA-IDENTITY-R012.2:** Assertion verification and SAML sign-in (rollup)
- **ARBORA-IDENTITY-R012.2.1:** Typed parsed-assertion model and audience/recipient/time-window checks
- **ARBORA-IDENTITY-R012.2.2:** Signature verification through an injected verifier port
- **ARBORA-IDENTITY-R012.2.3:** Replay check and sign-in handler wiring
- **ARBORA-IDENTITY-R012.2.3.1:** Replay check through a seen-assertion-id store port
- **ARBORA-IDENTITY-R012.2.3.2:** Sign-in handler wiring into the browser authority
- **ARBORA-IDENTITY-R012.2.4:** Live IdP sign-in probe
- **ARBORA-IDENTITY-R016:** Optional SMTP adapter for identity email
- **ARBORA-IDENTITY-R016.1:** Remaining scope of ARBORA-IDENTITY-R016 (slice .1): Optional SMTP adapter for identity email
- **ARBORA-IDENTITY-R016.2:** Remaining scope of ARBORA-IDENTITY-R016 (slice .2): Optional SMTP adapter for identity email
- **ARBORA-IDENTITY-R017.1:** Remaining scope of ARBORA-IDENTITY-R017 (slice .1): Remove the unauthenticated opt-out once clients migrate
- **ARBORA-IDENTITY-R017.2:** Remaining scope of ARBORA-IDENTITY-R017 (slice .2): Remove the unauthenticated opt-out once clients migrate
- **ARBORA-IDENTITY-R004.2:** Bootstrap resolution path, Host/Origin validation, unsecured-mode indicators
- **ARBORA-IDENTITY-R006:** Optional MFA with TOTP, WebAuthn, and recovery codes (rollup)
- **ARBORA-IDENTITY-R006.1:** Typed MFA enrollment model with refusal tests
- **ARBORA-IDENTITY-R006.2:** TOTP, WebAuthn, recovery-code ceremonies and group enforcement
- **ARBORA-IDENTITY-R007:** Admin console tabs with mapping dry-run and mode wizard (rollup)
- **ARBORA-IDENTITY-R007.1:** Typed admin-console tab model with refusal tests
- **ARBORA-IDENTITY-R007.2:** Mapping dry-run preview and mode-transition wizard
- **ARBORA-IDENTITY-R025:** Register identity ops once the engine contract lists `identity:admin`.
- **ARBORA-IDENTITY-R026:** Hosted sessions are attributed to a client and segmented by principal (rollup).
- **ARBORA-IDENTITY-R026.1:** Client session record.
- **ARBORA-IDENTITY-R026.2:** Record at session open.
- **ARBORA-IDENTITY-R026.3:** Runs carry the session id.
- **ARBORA-IDENTITY-R026.4:** Audit event per run.
- **ARBORA-IDENTITY-R026.5:** Caller-scoped client listing.
- **ARBORA-IDENTITY-R026.6:** User and service account parity.
- **ARBORA-IDENTITY-R026.7:** Cross-client denial is uniform.

- **ARBORA-IDENTITY-R023.1:** Model: scoped identity synthesis.
- **ARBORA-IDENTITY-R023.2:** Behavior: scoped identity synthesis.
- **ARBORA-IDENTITY-R023.3:** Wiring: scoped identity synthesis.
- **ARBORA-IDENTITY-R023.4:** Refusal: scoped identity synthesis.
- **ARBORA-IDENTITY-R023.5:** Evidence: scoped identity synthesis.
- **ARBORA-IDENTITY-R023.6:** Hot-path proof: scoped identity synthesis.
- **ARBORA-IDENTITY-R017.3:** Model: scoped identity synthesis.
- **ARBORA-IDENTITY-R017.4:** Behavior: scoped identity synthesis.
- **ARBORA-IDENTITY-R017.5:** Wiring: scoped identity synthesis.
- **ARBORA-IDENTITY-R017.6:** Refusal: scoped identity synthesis.
- **ARBORA-IDENTITY-R017.7:** Evidence: scoped identity synthesis.
- **ARBORA-IDENTITY-R017.8:** Hot-path proof: scoped identity synthesis.
- **ARBORA-IDENTITY-R019.1:** Model: scoped identity synthesis.
- **ARBORA-IDENTITY-R019.2:** Behavior: scoped identity synthesis.
- **ARBORA-IDENTITY-R019.3:** Wiring: scoped identity synthesis.
- **ARBORA-IDENTITY-R019.4:** Refusal: scoped identity synthesis.
- **ARBORA-IDENTITY-R019.5:** Evidence: scoped identity synthesis.
- **ARBORA-IDENTITY-R019.6:** Hot-path proof: scoped identity synthesis.

## Additional owner requirements

- [ ] T004 ARBORA-IDENTITY-R026.8 Deliver every requirement in requirements.md.
- [x] T005 ARBORA-IDENTITY-R002.1 scope registry generated from EG `scopes.json`. Evidence: `tests/security/test_scope_registry.py::test_the_generated_module_is_the_installed_eg_registry`.
- [x] T006 ARBORA-IDENTITY-R027 Process authority renews indefinitely; failures log their cause and readiness names the reason. Evidence: `tests/mcp_server/test_process_authority_renewal.py`.
- [x] T007 ARBORA-IDENTITY-R029 A run renews its caller's authority while active, bounded and audited; stops on end, cancel or revocation; fan-out inherits. Evidence: `tests/mcp_server/test_caller_run_authority.py`.
- [ ] T008 ARBORA-IDENTITY-R029.4 Issued bearers carry the opaque session-reference claim.
- [ ] T009 ARBORA-IDENTITY-R029.5 Logout and session-revocation routes call `revoke_caller`.
- [ ] T010 ARBORA-IDENTITY-R029.6 The renewal loop registers an engine-backed check via `CALLER_REVOCATIONS.add_check` using `MEDULLA-TYPED-PACKS-R085.3`.

## Delegated identity for connector calls (R028)

- [ ] T011 ARBORA-IDENTITY-R028 Connector calls that need a user identity carry a delegated identity (rollup).
- [ ] T012 ARBORA-IDENTITY-R028.1 Model.
- [ ] T013 ARBORA-IDENTITY-R028.2 Behavior.
- [ ] T014 ARBORA-IDENTITY-R028.3 Wiring.
- [ ] T015 ARBORA-IDENTITY-R028.4 Refusal.
- [ ] T016 ARBORA-IDENTITY-R028.5 Evidence.
- [ ] T017 ARBORA-IDENTITY-R028.6 Hot-path proof.

## WebUI mode composition extension

- ARBORA-IDENTITY-R004.7: qualify explicit mode configuration with installed WebUI in none, EG and external OIDC modes. Source mode tests are not release qualification.
- ARBORA-IDENTITY-R004.4: qualify host admission middleware with a configured issuer/auth route owner, current EG session checks, renewal and WebSocket support. Current HTTP adapter deliberately refuses unsupported cookie WebSocket admission.
- ARBORA-IDENTITY-R004.5: finish and qualify same-owner HTTP dispatch composition. Source tests cover the bounded non-streaming fence; installed WebUI integration and real effect boundaries remain open. Keep browser streaming unavailable until a task-safe lifetime contract is qualified.
- ARBORA-IDENTITY-R004.6: migrate explicit anonymous effects through existing governed operations, preserve canonical action binding and containment/confirmation, and prove UI/WebMCP equivalence. Read-only safety scaffolding is not full no-auth support.

## Source-only delivery and open integration children

- ARBORA-IDENTITY-R004.7.1: Host mode configuration source contract. Source tests exercise typed settings, factory arguments and refusal with an older incompatible WebUI factory.
- ARBORA-IDENTITY-R004.7.2: Installed identity-mode startup. Installed package startup, provider failure and mode-change probes remain required.
- ARBORA-IDENTITY-R004.4.1: Bound HTTP adapter source contract. Source fixtures prove routing, cleanup, redaction and refusal; they do not prove deployed login.
- ARBORA-IDENTITY-R004.4.2: Installed browser identity lifecycle. Real issuer/session and browser lifecycle probes remain required.
- ARBORA-IDENTITY-R004.5.1: Private same-owner dispatch source contract. Synthetic-owner tests prove bounded non-streaming dispatch, mounted routing, races and cancellation without claiming native effects.
- ARBORA-IDENTITY-R004.5.2: Canonical WebUI factory safety source contract. Factory source tests use synthetic issuer/runtime facts and one shared-pipeline read. Cover malformed bodies, redaction, rate and capacity refusal.
- ARBORA-IDENTITY-R004.5.3: Installed and native dispatch qualification. Real effect-boundary, installed provider and streaming qualification remain open.

### Explicit anonymous local mode amendment

- Add explicit process-selected mode, separate anonymous caller/session provenance, and unsigned EG client composition.
- Preserve verified carrier rejection, human/service rules, approval scopes, and authenticated-default behavior.
- Add focused mode/provenance/ASGI/transport tests, including malformed identity and nonlocal/credential request refusals.
- Validate real native EG persistence, effect journaling, real served Arbora and actual TUI with only the model simulated.
- Validate separate fresh authenticated stack and missing-token refusal after anonymous acceptance.
- Complete independent review and upstream CI before merge.

- ARBORA-IDENTITY-R004.6: Qualify the separate local browser transport and discriminated attended proof before runtime wiring.
- ARBORA-IDENTITY-R004.6: Prove policy, gesture, exact action binding, revocation and separate mutation confirmation without an identity upgrade.
- [ ] T018 ARBORA-IDENTITY-R004.8 Complete all anonymous task access children.
- [ ] T019 ARBORA-IDENTITY-R004.8.1 Implement and test the served read contract with synthetic native evidence.
- [ ] T020 ARBORA-IDENTITY-R004.8.2 Reconcile native anonymous ownership and qualify installed reads before activation.
- [ ] T021 ARBORA-IDENTITY-R004.8.3 Qualify the existing public native client adapter with synthetic transport tests.


- [ ] T022 ARBORA-IDENTITY-R004.9 Complete the local anonymous text execution children.
- [ ] T023 ARBORA-IDENTITY-R004.9.1 Compose native admission and the received text-only harness in source tests.
- [ ] T024 ARBORA-IDENTITY-R004.9.2 Qualify installed local sends and cancellation before activation.
- [ ] T025 ARBORA-IDENTITY-R004.9.3 Qualify owner-bound durable answer recovery.

- [ ] T026 ARBORA-IDENTITY-R004.9.3.1 Test bounded native event storage and committed-reference recovery in source composition.
- [ ] T027 ARBORA-IDENTITY-R004.9.3.2 Qualify installed host and native restart recovery.
- [ ] T028 ARBORA-IDENTITY-R001 Elevation request, approve and revoke operations on every surface.
- [ ] T029 ARBORA-IDENTITY-R002 Domain scopes consume the generated scope registry.
- [ ] T030 ARBORA-IDENTITY-R002.2 The scope registry is ingested from the handshake.
- [ ] T031 ARBORA-IDENTITY-R003 Persistent local token issuer with secret-backed key.
- [ ] T032 ARBORA-IDENTITY-R004.1 Typed bootstrap-principal model.
- [ ] T033 ARBORA-IDENTITY-R004.2 Unsecured-mode indicators.
- [ ] T034 ARBORA-IDENTITY-R004.3 Explicit tokenless local mode.
- [ ] T035 ARBORA-IDENTITY-R005 Local account lifecycle with server-side sessions (rollup).
- [ ] T036 ARBORA-IDENTITY-R005.1 Consume exact persisted session facts.
- [ ] T037 ARBORA-IDENTITY-R005.2 Account ceremonies and throttling.
- [ ] T038 ARBORA-IDENTITY-R006 Optional MFA with TOTP, WebAuthn and recovery codes (rollup).
- [ ] T039 ARBORA-IDENTITY-R006.1 Typed MFA enrollment model.
- [ ] T040 ARBORA-IDENTITY-R006.2 MFA ceremonies.
- [ ] T041 ARBORA-IDENTITY-R007 Admin console tabs with mapping dry-run and mode wizard (rollup).
- [ ] T042 ARBORA-IDENTITY-R007.1 Typed admin-console tab model.
- [ ] T043 ARBORA-IDENTITY-R007.2 Mapping dry-run and mode-transition behaviour.
- [ ] T044 ARBORA-IDENTITY-R009 Multi-provider OIDC with mapping rules and JIT policy (rollup).
- [ ] T045 ARBORA-IDENTITY-R010 LDAPS bind with nested group sync (rollup).
- [ ] T046 ARBORA-IDENTITY-R008.1 Typed API-key grant model.
- [ ] T047 ARBORA-IDENTITY-R008.2 Scope intersection and revocation.
- [ ] T048 ARBORA-IDENTITY-R009.1 Typed OIDC mapping-rule model.
- [ ] T049 ARBORA-IDENTITY-R009.2 OIDC authentication flow (rollup).
- [ ] T050 ARBORA-IDENTITY-R009.2.1 Ordered claim-to-rule evaluation.
- [ ] T051 ARBORA-IDENTITY-R009.2.2 ID-token validation inputs and typed refusal.
- [ ] T052 ARBORA-IDENTITY-R009.2.3 OIDC callback wiring.
- [ ] T053 ARBORA-IDENTITY-R010.1 Typed LDAPS bind-config model.
- [ ] T054 ARBORA-IDENTITY-R010.2 Directory bind and group sync (rollup).
- [ ] T055 ARBORA-IDENTITY-R010.2.1 Group-DN-to-role mapping evaluation.
- [ ] T056 ARBORA-IDENTITY-R010.2.2 Bind through an injected directory port.
- [ ] T057 ARBORA-IDENTITY-R010.2.3 Scheduled group sync (rollup).
- [ ] T058 ARBORA-IDENTITY-R010.2.3.1 Pure sync plan.
- [ ] T059 ARBORA-IDENTITY-R010.2.3.2 Scheduled directory sync apply.
- [ ] T060 ARBORA-IDENTITY-R010.2.4 Live directory probe.
- [ ] T061 ARBORA-IDENTITY-R012 Native SAML service provider (rollup).
- [ ] T062 ARBORA-IDENTITY-R011.1 Typed SCIM service-credential model.
- [ ] T063 ARBORA-IDENTITY-R011.2 SCIM server surface (rollup).
- [ ] T064 ARBORA-IDENTITY-R011.2.1 Typed SCIM User resource model.
- [ ] T065 ARBORA-IDENTITY-R011.2.2 SCIM credential check.
- [ ] T066 ARBORA-IDENTITY-R011.2.3 SCIM create/patch/deactivate handlers.
- [ ] T067 ARBORA-IDENTITY-R011.2.4 SCIM route registration.
- [ ] T068 ARBORA-IDENTITY-R012.1 Typed SAML service-provider model.
- [ ] T069 ARBORA-IDENTITY-R012.2 SAML assertion verification and sign-in (rollup).
- [ ] T070 ARBORA-IDENTITY-R012.2.1 Typed parsed-assertion model and condition checks.
- [ ] T071 ARBORA-IDENTITY-R012.2.2 Signature verification through an injected verifier port.
- [ ] T072 ARBORA-IDENTITY-R012.2.3 SAML replay check and sign-in wiring (rollup).
- [ ] T073 ARBORA-IDENTITY-R012.2.3.1 Replay check through a seen-assertion-id store port.
- [ ] T074 ARBORA-IDENTITY-R012.2.3.2 SAML sign-in handler.
- [ ] T075 ARBORA-IDENTITY-R012.2.4 Live SAML provider sign-in probe.
- [ ] T076 ARBORA-IDENTITY-R013 Identity CLI commands and doctor diagnostics (rollup).
- [ ] T077 ARBORA-IDENTITY-R014 Shared RBAC decision oracle across identity modes.
- [ ] T078 ARBORA-IDENTITY-R015 Identity operations documentation and cutover runbook.
- [ ] T079 ARBORA-IDENTITY-R016 Optional SMTP adapter for identity email.
- [ ] T080 ARBORA-IDENTITY-R016.1 Arbora-side mail capability model.
- [ ] T081 ARBORA-IDENTITY-R016.2 SMTP send transport for identity email.
- [ ] T082 ARBORA-IDENTITY-R017 Remove the unauthenticated opt-out (rollup).
- [ ] T083 ARBORA-IDENTITY-R017.1 Client entry-point token audit registry.
- [ ] T084 ARBORA-IDENTITY-R017.2 Engine opt-out removal.
- [ ] T085 ARBORA-IDENTITY-R017.3 Bearer model.
- [ ] T086 ARBORA-IDENTITY-R017.4 Bearer verification behaviour.
- [ ] T087 ARBORA-IDENTITY-R017.5 Bearer ingress wiring.
- [ ] T088 ARBORA-IDENTITY-R017.6 Bearer refusals.
- [ ] T089 ARBORA-IDENTITY-R017.7 Bearer evidence.
- [ ] T090 ARBORA-IDENTITY-R017.8 Bearer hot-path proof.
- [ ] T091 ARBORA-IDENTITY-R018 Identity administration operations (rollup).
- [ ] T092 ARBORA-IDENTITY-R019 Access operations for elevation and approval decisions (rollup).
- [ ] T093 ARBORA-IDENTITY-R019.1 Resource access model.
- [ ] T094 ARBORA-IDENTITY-R019.2 Resource grant evaluation.
- [ ] T095 ARBORA-IDENTITY-R019.3 Resource gate wiring at fleet.call.
- [ ] T096 ARBORA-IDENTITY-R019.4 Resource gate refusals.
- [ ] T097 ARBORA-IDENTITY-R019.5 Resource access evidence.
- [ ] T098 ARBORA-IDENTITY-R019.6 Resource access hot-path proof.
- [ ] T099 ARBORA-IDENTITY-R020 Domain scope registry covers finance, fleet, loops and ops.
- [ ] T100 ARBORA-IDENTITY-R021 Identity wiring depends only on identity ports.
- [ ] T101 ARBORA-IDENTITY-R022 Fleet authorization fails closed on exact scopes.
- [ ] T102 ARBORA-IDENTITY-R023 Native self-refreshing credentials for MCP clients (rollup).
- [ ] T103 ARBORA-IDENTITY-R023.1 Credential reuse model.
- [ ] T104 ARBORA-IDENTITY-R023.2 Credential reuse behaviour.
- [ ] T105 ARBORA-IDENTITY-R023.3 Credential reuse wiring.
- [ ] T106 ARBORA-IDENTITY-R023.4 Credential reuse refusals.
- [ ] T107 ARBORA-IDENTITY-R023.5 Credential reuse evidence.
- [ ] T108 ARBORA-IDENTITY-R023.6 Credential reuse hot-path proof.
- [ ] T109 ARBORA-IDENTITY-R024 Committed browser and SSO provider probe.
- [ ] T110 ARBORA-IDENTITY-R025 Register identity operations in the registry.
- [ ] T111 ARBORA-IDENTITY-R026 Hosted sessions are attributed to a client and segmented by principal (rollup).
- [ ] T112 ARBORA-IDENTITY-R026.1 Client session record.
- [ ] T113 ARBORA-IDENTITY-R026.2 Record at session open.
- [ ] T114 ARBORA-IDENTITY-R026.3 Runs carry the session id.
- [ ] T115 ARBORA-IDENTITY-R026.4 Audit event per run.
- [ ] T116 ARBORA-IDENTITY-R026.5 Caller-scoped client listing.
- [ ] T117 ARBORA-IDENTITY-R026.6 User and service account parity.
- [ ] T118 ARBORA-IDENTITY-R026.7 Cross-client denial is uniform.
- [ ] T119 ARBORA-IDENTITY-R650 External policy sync renders the engine's row-visibility bundle and fails closed.
- [ ] T120 ARBORA-IDENTITY-R029.1 Run lease renewal.
- [ ] T121 ARBORA-IDENTITY-R029.2 Stop conditions.
- [ ] T122 ARBORA-IDENTITY-R029.3 Inheritance and audit.
- [ ] T123 ARBORA-IDENTITY-R004.7 Explicit WebUI identity-mode composition.
- [ ] T124 ARBORA-IDENTITY-R004.4 Host browser-session adapter.
- [ ] T125 ARBORA-IDENTITY-R004.5 Same-owner HTTP dispatch fence.
- [ ] T126 ARBORA-IDENTITY-R004.6 Explicit anonymous effect migration.
- [ ] T127 ARBORA-IDENTITY-R004.7.1 Host mode configuration source contract.
- [ ] T128 ARBORA-IDENTITY-R004.7.2 Installed identity-mode startup qualification.
- [ ] T129 ARBORA-IDENTITY-R004.4.1 Bound HTTP adapter source contract.
- [ ] T130 ARBORA-IDENTITY-R004.4.2 Installed browser identity lifecycle qualification.
- [ ] T131 ARBORA-IDENTITY-R004.5.1 Private same-owner dispatch source contract.
- [ ] T132 ARBORA-IDENTITY-R004.5.2 Canonical WebUI factory safety source contract.
- [ ] T133 ARBORA-IDENTITY-R004.5.3 Installed dispatch and streaming qualification.
- [ ] T134 ARBORA-IDENTITY-R013.1 CLI skeleton with claim and link-claim.
- [ ] T135 ARBORA-IDENTITY-R013.2 CLI transition, reset-admin and rotate.
- [ ] T136 ARBORA-IDENTITY-R013.3 Doctor diagnostics for exposed mode and generated secret.
- [ ] T137 ARBORA-IDENTITY-R014.1 Mode transition operation.
- [ ] T138 ARBORA-IDENTITY-R018.1 Identity operations module and port.
- [ ] T139 ARBORA-IDENTITY-R018.2 Self operations.
- [ ] T140 ARBORA-IDENTITY-R018.3 User, session, API key and service account operations.
- [ ] T141 ARBORA-IDENTITY-R018.4 Role, group and provider operations.
- [ ] T142 ARBORA-IDENTITY-R018.5 Policy, mode, issuer, audit and SCIM client operations.
- [ ] T143 ARBORA-IDENTITY-R018.6 Admin gate.
- [ ] T144 ARBORA-IDENTITY-R018.7 Identity operations served parity.
- [ ] T145 ARBORA-IDENTITY-R019.7 Register approvals operations.
- [ ] T146 ARBORA-IDENTITY-R651 Share grant and revoke operations.
- [ ] T147 ARBORA-IDENTITY-R652 Graph ownership plan and apply operations.
- [ ] T148 ARBORA-IDENTITY-R030 Coding clients and terminal UIs authenticate natively (rollup).
- [ ] T149 ARBORA-IDENTITY-R030.1 Deliver `arbora clients enroll` creates one machine client.
- [ ] T150 ARBORA-IDENTITY-R030.2 Deliver `arbora token` prints a fresh bearer token for a header helper.
- [ ] T151 ARBORA-IDENTITY-R030.3 The served endpoint publishes protected-resource metadata.

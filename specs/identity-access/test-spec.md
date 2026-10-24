# Test contract — ARBORA-IDENTITY-ACCESS

These integration tests have not run for this spec. No pass is inferred from prior source snapshots.

## Portable fixture

Use an ephemeral engine process or in-memory contract fixture implementing the public identity/access methods, a generated local signing key, deterministic clock, mock OIDC issuer/JWKS, mock LDAP directory, SCIM client, mock SMTP port and browser test server. The ordinary PR suite must provision its own dependencies and data; it must not depend on an operator's live identity provider, host inventory, secret backend, network address or sibling checkout. Record a missing optional live adapter as a skip in a separate deployment profile. Never skip the local contract fixture for that reason. Secrets remain fixture-only.

| Test | Requirements | Level and fixture | Positive result | Negative / boundary result |
|---|---|---|---|---|
| IT-01 | IA-01, IA-02 | Engine contract + local issuer | First boot seeds mode once. Discovery/JWKS verify a token whose `sub` is stable. CAS increments epoch. | Competing epoch conflicts. Wrong `kid`, audience, tenant and expired token fail. Configuration drift does not overwrite stored mode. |
| IT-02 | IA-03 | Served loopback HTTP/MCP | `none` resolves `usr:bootstrap`, reaches engine RBAC, displays mode and warning in session/status/MCP initialize. | Non-loopback without exact acknowledgment refuses startup. DNS-rebinding Host and missing/cross-origin Origin on cookie mutation reject before token minting. Approver/live-order requests deny. |
| IT-03 | IA-04 | Browser + local account fixture | Register under policy and log in. Rotate session on login/privilege change, change password, reset once and log out. Next request sees revocation. | Unknown username and wrong password give the same public response; throttling applies. Missing/replayed CSRF, reused reset token, stale session and fixation ID fail. |
| IT-04 | IA-05 | TOTP/WebAuthn ceremony fixtures | Valid TOTP, passkey and recovery code authenticate. Configured group rule requires enrollment. Action step-up succeeds within freshness window. | Replayed TOTP step/code, wrong RP/origin/challenge, stale MFA, non-enrolled required-group member and bad sign counter fail. Optional-default user can sign in without MFA. |
| IT-05 | IA-06 | REST/MCP operation contract | Admin console can manage a user, role, session, key, provider, policy and audit. Self sees own redacted record. Dry-run names resulting roles. | Delegated or service session, absent exact `identity:admin`, stale MFA, wrong tenant and MCP admin mutation deny. Secret/hash fields never appear. |
| IT-06 | IA-07 | API-key + owner-scope fixture | A key with a subset of current owner scopes works. Refresh is in process; one session survives key rotation as specified. | Revoked/expired keys and removed owner scopes deny the next use. Approver scope, static tokens, missing backends and replayed refresh families deny. |
| IT-07 | IA-08 | Two mock OIDC issuers | PKCE, state/nonce and subject link produce stable principal. Group removal removes sourced role. `Id_token_hint` sent on logout. | Wrong issuer/audience/signature/nonce, callback replay, unverified-email auto-link, duplicate link and privileged mapping without fresh admin MFA deny. |
| IT-08 | IA-09 | Mock TLS LDAP, SCIM client, OIDC SAML broker | Escaped query returns bounded nested memberships. SCIM PATCH deprovisions and revokes session/key while preserving owned data. Brokered SAML yields verified OIDC identity. | Plain LDAP, filter injection, disabled AD account, SCIM token for another provider, malformed PATCH, SCIM restore without policy, and raw unverified SAML assertion deny. |
| IT-09 | IA-10, IA-15 | CLI/doctor and fake SMTP | Claim/link/transition/rotate produce typed result. Doctor reports exposure and optional SMTP. Configured SMTP sends a one-use reset. | Token not printed twice or logged. Absent SMTP hides email-only option. Exposed demo and automatic service secret cause diagnostic failure. |
| IT-10 | IA-11 | Shared YAML decision table on `none`, `local`, `external` with mock OIDC + LDAP | For each `(principal,scope,action,resource)`, effective claims digest, allow/deny and reason code match. `none→local→external→local→none→local` leaves principals, owners and grants unchanged. | Deliberate role/scope divergence fails oracle; stale mode token fails after transition; concurrent flip conflicts. |
| IT-11 | IA-12 | Engine access contract + REST/MCP | Each operation returns the engine decision and audit receipt. Cover request/approve/deny/revoke, approval list, lease, check and explain. Route and MCP twin agree. | Self approval, delegated approval, expired lease, unavailable engine, missing exact scope and graph-only privilege shortcut deny. No local fallback receipt. |
| IT-12 | IA-13 | Registry and dispatch property tests | Every advertised operation has an exact registered scope/class. Domain registry export matches engine source. Fleet operation passes with exact fleet grant. | `admin`, `kg:admin`, stdio transport, unknown tool and omitted registry entry never grant fleet/MCP access. |
| IT-13 | IA-14 | Import graph + composition test | One server/issuer/session implementation is constructed and injected through ports. REST/MCP/web host share it. | Import cycle, orphan implementation, duplicate authority or direct web-host import of MCP implementation fails. |
| IT-14 | IA-16 | Served engine verifier plus each Arbora client entry point | Bootstrap, REST, MCP, A2A, CLI and background service requests carry an accepted signed local-issuer or permitted service token. Check expected allow and deny decisions. | Missing/expired/wrong-audience tokens and explicit engine denial produce a typed error. The client never retries unsigned or uses fallback credentials or auth-opt-out branches. An old client fails closed after engine opt-out removal. |

## Acceptance evidence and release checks

For each test, `evidence.md` must link a reproducible job or command with exact commit, fixture revision, result and any justified skip. The public PR gate runs the full portable suite on a clean checkout. A separate served deployment qualification may exercise a real provider; it cannot substitute for portable contract and negative tests. Browser tests must prove rendered warnings, login/MFA, key revocation and admin restriction; API-only tests are insufficient for the browser story.

Run repository checks from a clean checkout with `uv sync --extra test`, `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy arbora`, `uvx --from pre-commit==4.6.0 pre-commit run --all-files`, and the release workflow's build/lock checks. The scanner workflow provisions pinned CCCC, KISS, Dupehound and jscpd. Use the repository's configured thresholds and differential baseline; do not change them to fit this implementation. The current hook configuration names `complexity-staged`, `kiss-staged`, `dupehound-changed`, `complexity-census`, `kiss-census`, `jscpd-differential`, `jscpd-census`, and `scanner-versions`; the release workflow pins tool versions. Apply KISS with ports and one composition root, preserve the orphan-module gate, and reject duplicate issuer, policy and RBAC implementations. Record unavailable tool/threshold as a gap, never as a pass.

No pass on an unrelated branch, unmerged patch or earlier commit qualifies the exact landed commit. Acceptance requires IT-01 through IT-14, scanner/quality/release jobs, portable fresh-checkout proof, and resolved design decisions. A row can remain partial even if this umbrella spec is otherwise accepted.

## Strict producer regression slice

Focused source tests cover malformed/missing authority fields, immutable copied
resolution/context sets, direct-only delegation, exact scope narrowing and
source-expiry-bounded local claim preparation. Browser tests cover duplicate or
invalid cookies, exact configured Origin, mutation CSRF, cookie lifetime and
private credential digests. They cover request/session-instance substitution, including
mutation across an await. Source fixtures do not qualify the EG producer, local
signing keys, session persistence, installed generated bindings or mounted
HTTP/MCP/WebUI serving. Those acceptance requirements remain outstanding.

Additional source tests verify RS256 signatures using ephemeral in-memory test
keys read through the injected existing key-store port. They check refusal without key
provisioning and current-authority changes during issuance. Request-lifecycle
fixtures exercise the actual WebUI evidence/export class with synthetic backend
and token-verifier owners: same caller-instance resolution, after-await request
mutation, rotation/revocation, refresh invalidation, cancellation cleanup and
concurrent same-subject isolation. These establish the composed source behavior
only; no real EG/session/key-store binding or mounted deployment is qualified.

Deterministic await regressions revoke, rotate, expire or alter caller authority
during token verification in both initial binding and pre-invocation checks.
All must refuse with no invocation or retained binding. A separate case changes
caller policy during the final session resolution and requires refusal before
evidence return. These preserve the earlier cookie and effective-route probes.
| ARBORA-IDENTITY-R026.1 | `tests/identity/test_client_session.py` | Client session record. |
| ARBORA-IDENTITY-R026.2 | `tests/identity/test_client_session_open.py` | Record at session open. |
| ARBORA-IDENTITY-R026.3 | `tests/identity/test_run_attribution.py` | Runs carry the session id. |
| ARBORA-IDENTITY-R026.4 | `tests/identity/test_run_audit.py` | Audit event per run. |
| ARBORA-IDENTITY-R026.5 | `tests/identity/test_list_clients.py` | Caller-scoped client listing. |
| ARBORA-IDENTITY-R026.6 | `tests/identity/test_principal_parity.py` | User and service account parity. |
| ARBORA-IDENTITY-R026.7 | `tests/identity/test_cross_client_denial.py` | Cross-client denial is uniform. |
| ARBORA-IDENTITY-R027 | `tests/mcp_server/test_process_authority_renewal.py` | Process authority renews over twelve hours of 300-second tokens; failures report a reason. |
| ARBORA-IDENTITY-R029.1 | `tests/mcp_server/test_caller_run_authority.py` | A run spanning more than 3x TTL stays authorized under a fake clock. |
| ARBORA-IDENTITY-R029.2 | `tests/mcp_server/test_caller_run_authority.py` | Revocation, cancellation and the maximum lifetime stop renewal. |
| ARBORA-IDENTITY-R029.3 | `tests/mcp_server/test_caller_run_authority.py` | Fan-out children inherit the parent's renewal; audit carries no token. |
| ARBORA-IDENTITY-R029.4 | `tests/security/test_session_reference_claim.py` | Session-backed bearers carry the reference claim without token text. |
| ARBORA-IDENTITY-R029.5 | `tests/api/test_session_revocation_routes.py` | Logout and revocation routes call `revoke_caller` and audit. |
| ARBORA-IDENTITY-R029.6 | `tests/mcp_server/test_caller_run_authority.py` | An engine-revoked session stops renewal; engine failure never renews. |


## Served credential and resource authority acceptance

Bind R023.1 through R023.6 in `tests/identity/test_scoped_credential_reuse.py.`
Bind R017.3 through R017.8 in `tests/identity/test_bearer_tenant_ingress.py.`
Bind R019.1 through R019.6 in `tests/identity/test_resource_protocol_access.py.`

1. Start actual Arbora REST/MCP ingress, a controlled local OIDC provider, real PKCE callback and approved synthetic vault authority.
   Use real signed tokens and HTTP exchanges. Decoded claims or direct broker-helper calls alone cannot prove acceptance.
2. Serve a credential-missing operation, complete the browser ceremony, verify S256/state/nonce and read back the vault receipt.
   Resume the unchanged operation once after fresh authorization. Retry and restart without duplicating the governed effect.
3. Grant reuse to a second client for the same tenant, exact provider/resource/audience and scopes.
   Its served operation succeeds through transport injection; an unrelated co-located client fails without receiving or using credentials.
4. Inject browser opening failure, unavailable callback, absent interactive channel, input timeout, cancellation and provider exchange failure.
   Assert bounded typed outcomes, invalidated challenges and zero resumed effects. Test cross-client state, replay and failed vault persistence.
5. Clear browser-session storage and call REST/MCP with a valid bearer; identity admission must still succeed with current EG authority.
   Missing tenant, wrong audience, expiry, future not-before, invalid signature and duplicate headers refuse before handler invocation.
   A cookie cannot rescue an invalid bearer. Revocation and unavailable policy authority remain refusals after restart.
6. Enter actual web, SSH, RDP and database protocol adapters against controlled local endpoints with connection counters.
   Prove one exact user grant and one verified role grant, then deny a sibling resource and an unrelated principal.
   Wrong tenant, expired grant, unknown resource, unsupported enforcement and unavailable EG authority cause zero upstream connections.
   Revoke an active grant and observe channel termination within the specified bound; no subsequent operation reaches the endpoint.
7. Inspect served responses, traces and logs: no code, verifier, token, cookie, pasted redirect response or secret value appears.
   Receipts must join caller/client/tenant, operation, grant and vault references without claiming runtime delivery from a planned test.

The four protocol fixtures must each use their real adapter boundary; a shared permission-helper test proves none of them alone.
Absent adapters or qualified EG producer fields remain explicit acceptance blockers. These integration tests have not run for this specification change.


### Served connector and token-profile acceptance

Extend the existing named served credential/PKCE tests through actual REST/MCP ingress, connector dispatch and controlled provider exchanges.
Grant connector A and prove its authorized call succeeds. Keep tenant, principal/client, provider, audience and scopes unchanged, then target connector B.
Caller-forged connector A metadata on B refuses before secret resolution or exchange.
A genuine B identity with only A's grant also refuses before those steps.
An explicit B grant permits B without changing A's authority. Assert challenge, vault receipt and transport evidence contain the admitted connector.
Use an OIDC provider returning a signed ID token plus opaque access and refresh tokens; the declared mixed profile must succeed.
Separately prove the JWT access-token profile succeeds with valid signatures, audiences, expiry and resource bindings.
Bad JWT signature, nonce, audience, issuer or expiry refuses; a JWT failure must never retry under an opaque profile.
For exchange-bound opaque tokens, validate authenticated provider/client/PKCE and required resource/account/consent evidence without manufacturing JWT claims.
For introspection-required opaque tokens, active and correctly bound evidence succeeds; inactive, unavailable or missing required fields refuses.
An opaque token without provider expiry may use only the explicit bounded host lease. Assert evidence distinguishes the two validity sources.
Forged profile selection, wrong connector, cross-tenant binding, excess scopes and unapproved resource audience all refuse with zero resumed effects.
The outbound opaque profile must not admit an unsigned Arbora API bearer or remove its required tenant claim.
Inspect responses and logs for token/code leakage. These acceptance cases have not run. They require the runtime wiring.

## Additional owner requirements

- `ARBORA-IDENTITY-R026.8`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.

## Atomic session record consumer

ARBORA-IDENTITY-R005.1 tests exercise existing generated transport selection,
strict principal/tenant correlation, minimum expiry, missing legacy metadata,
unknown fields, incomplete MFA, bootstrap and fresh refusal after a prior
success. These fixtures prove consumer behavior only. EG native persistence,
committed response publication and real admitted host startup must be tested
against the qualified producer before claiming served acceptance.

The existing HostStartAdmission accepts the consumer session port directly.
Controlled owner fixtures check fresh session revocation and reference changes
before admission returns. They do not qualify create_agent_server or native
startup. Generated new-metadata decoding remains producer-dependent.

## ARBORA-IDENTITY-R028 acceptance

| Case | Row | Proving test and observation |
|---|---|---|
| Model | `ARBORA-IDENTITY-R028.1` | A unit test round-trips the model and reads the actor flag from a fixture manifest. |
| Behavior | `ARBORA-IDENTITY-R028.2` | Tests cover one exchanged call, one process-credential call and one expired caller token. |
| Wiring | `ARBORA-IDENTITY-R028.3` | A served test calls an actor-required tool of a fixture connector and observes the bound subject. |
| Refusal | `ARBORA-IDENTITY-R028.4` | A served test asserts each code and zero child calls. |
| Evidence | `ARBORA-IDENTITY-R028.5` | A test reads the audit event and asserts the fields and no token. |
| Hot-path proof | `ARBORA-IDENTITY-R028.6` | `tests/fleet/test_delegated_identity_served.py::test_actor_required_tools_succeed_for_a_user` passes. |

Hot-path proof for `ARBORA-IDENTITY-R028`: `tests/fleet/test_delegated_identity_served.py::test_actor_required_tools_succeed_for_a_user`.

## WebUI mode and host-admission composition

ARBORA-IDENTITY-R004.7 and ARBORA-IDENTITY-R004.4: source checks live in
`tests/webui_host/test_session_boundary.py`. They cover protected defaults,
invalid/missing composition refusal, exact auth namespace routing and no
forwarding on synthetic revocation/unavailability. Required acceptance also
includes real installed startup, EG session revocation/rotation, caller binding
cleanup/cancellation, full browser login/logout/renewal, and WebSocket identity.
No source fixture or no-op factory callback satisfies those remaining checks.

### ARBORA-IDENTITY-R004.5: same-owner dispatch source tests

`tests/api/test_browser_invocation_fence.py` uses the real browser authority owner with synthetic session facts. It proves bounded HTTP dispatch, body-await revocation/scope narrowing/session rotation/expiry/MFA removal refusal, private caller/task identity, duplicate-use refusal, cancellation cleanup, registry visibility race refusal, and preservation of post-invocation failure semantics. Browser checkout returns unavailable rather than leaking authority across a streaming task. These tests do not establish a live issuer, installed WebUI composition or native effect-boundary qualification.

### ARBORA-IDENTITY-R004.6: anonymous effect migration acceptance

Test direct enhanced file read/write/removal and configuration handlers remain inaccessible without a migrated explicit policy. For each migrated operation test canonical parameter/target/action-digest binding and containment. Test duplicate consumption and required confirmation at the real effect boundary. UI and WebMCP decisions must match. Never substitute an authenticated identity or fabricated grant for an anonymous caller.

The mounted primitive tests also run actual FastAPI routing and real BrowserAdmission/ArboraBrowserAuthority with a synthetic issuer. It returns a fixture string, not a generated credential. They verify exact original session preservation, successful same-task dispatch and revocation during body receipt. They check ambiguity refusal before issuer use, cleanup and propagation of post-dispatch errors. Outer admission before a mount refuses because the immutable routing context changes. A fresh live issuer and installed WebUI remain untested.

`tests/webui_host/test_canonical_browser_composition.py` exercises the actual WebUI application factory with the guarded host boundary and real admission owner, using a TestModel, synthetic issuer facts and synthetic native runtime. One read traverses the real shared invocation pipeline; forged authority parameters are rejected by its existing validation. Other cases cover body-await revocation, absent cookie/CSRF, wrong origin, legacy effect and prefix-confusion refusal, host-owned auth routing and liveness. No WebUI grant is minted. Source acceptance does not establish deployed security or complete identity-mode features.

Canonical actual-factory negative cases cover oversized chunked and understated-length bodies stopping at the first over-limit chunk. They cover declared oversize and ambiguous framing before issuance or body receipt. They check provider HTTP and validation-detail masking and configured rate rejection with Retry-After. They verify capacity overflow without issuance and cancellation releasing capacity. Re-run these with installed qualified dependencies before activation; source tests use synthetic issuer/runtime facts only.

A non-loopback factory case with no explicit rate verifies the canonical rate and burst equal WebUI's positive resolved defaults even while Arbora's separate setting remains zero. Configuration changes are test-local monkeypatches; no listener or production security settings are modified.

## Exact source test bindings

| Child | Source evidence or remaining acceptance |
|---|---|
| ARBORA-IDENTITY-R004.7.1 | Mode settings and factory refusal tests in `tests/webui_host/test_session_boundary.py`. |
| ARBORA-IDENTITY-R004.7.2 | Installed startup with real providers remains open. |
| ARBORA-IDENTITY-R004.4.1 | Routing, admission refusal and cleanup tests in `tests/webui_host/test_session_boundary.py`. |
| ARBORA-IDENTITY-R004.4.2 | Real installed browser lifecycle and WebSocket identity remain open. |
| ARBORA-IDENTITY-R004.5.1 | Private caller, mounted dispatch and race tests in `tests/api/test_browser_invocation_fence.py`. |
| ARBORA-IDENTITY-R004.5.2 | Actual-factory safety tests in `tests/webui_host/test_canonical_browser_composition.py`. |
| ARBORA-IDENTITY-R004.5.3 | Installed providers, native effect journals and streaming qualification remain open. |

Only the four source children have test markers in this candidate. Broad parents and installed/native siblings have no new delivery markers.

## Explicit tokenless local admission (ARBORA-IDENTITY-R004.3)

`tests/security/test_anonymous_mode.py` exercises the public ASGI identity boundary and canonical HTTP caller projection. It covers immutable mode activation, strict bind/Host/Origin/credential admission, and graph-pool mode isolation. It tests verified carrier API rejection, exact anonymous caller invariants, human/service operation refusal, and honest effect-journal provenance. These focused tests do not substitute for native EG or actual TUI acceptance.

Native acceptance must also provision a fresh none-mode store through the official CLI and serve actual MCP/REST/A2A requests through Arbora. It must persist and restart graph state and preserve effect idempotency. It must run the real TUI against a deterministic local model. A separately provisioned authenticated store must then pass corresponding operations and reject missing credentials. Neither suite may bypass contract admission or operation approvals.

`tests/test_real_anonymous_boot.py` is the native, credential-free real serving gate. Set `ARBORA_NOAUTH_TEST_ENGINE_BINARY` to a built engine whose embedded contract matches the installed EG client and generated Arbora descriptors. It uses official first-start provisioning and checks REST, MCP, forbidden credentials/origins, and actual installed TUI A2A chat. The simulated model echoes a unique prompt nonce; separate model receipts and persisted task artifacts must match the TUI-rendered response. An unselected binary skips explicitly, not a success claim. Fullscreen native/Python PTY, restart, and authenticated-mode acceptance remain additional release gates.

PolicyGate regression coverage admits an exact anonymous caller for an ordinary scoped operation. It refuses forged objects, disabled mode, human/service rules, and missing scopes. An enabled PDP denial remains a denial; successful real REST and MCP queries must exercise this gate rather than only the A2A chat path.

The co-service fixture in `tests/webui_host/test_webui_contact_delivery.py` preserves both composition paths after browser discovery integration. It checks explicit mode forwarding to a synthetic WebUI factory and the exact browser service's owner-loop binding before serving. Existing runtime tests check binding cleanup on shutdown. These checks do not qualify installed authentication providers.

Canonical factory safety tests both the verifier-required host-bound entry point and explicit EG selection. The installed factory exercises every required-mode dispatch case. It must refuse unsupported explicit EG requests before issuance or dispatch. Separate mode-compatibility tests verify explicit none/EG/OIDC forwarding with the companion factory and refusal with an older installed factory. Unsupported explicit modes stay unavailable; this is not a production fallback.

### R004.6 local attended contract qualification

First preserve anonymous identity and the existing default transport refusal.
Reject attempts to replace required OIDC evidence with empty local placeholders.
Read and mutation proposals both require a configured policy and a trusted gesture.
Registration alone must produce no permitted operation or effect.

Before runtime delivery, test exact document, route, generation, catalog, tool-schema and action binding.
Test expiry, policy removal and revocation during authority awaits.
A duplicate action must not produce another prompt or effect.
Mutations must still require separate exact confirmation.
A local proof must never satisfy an OIDC proof or create authenticated identity.
These future acceptance tests require the owner implementation; current refusal fixtures do not qualify them.

| Anonymous task access | `ARBORA-IDENTITY-R004.8` | All children pass. Synthetic evidence does not complete native qualification. |
| Served read contract | `ARBORA-IDENTITY-R004.8.1` | `tests/a2a/test_anonymous_reads.py` tests exact owner, request, task and target binding. It tests host/origin/credential refusal, deadlines, cancellation, bounded input and no cached output disclosure. |
| Native anonymous ownership | `ARBORA-IDENTITY-R004.8.2` | Installed engine tests distinguish anonymous and authenticated task owners. They prove native namespace enforcement, restart reads and credential rejection. This acceptance remains open. |
| Native client source adapter | `ARBORA-IDENTITY-R004.8.3` | `tests/a2a/test_anonymous_native_reads.py` uses the actual public client and codec with a synthetic response transport. It checks method negotiation, immutable target binding, mode changes and no generic read fallback. |

## Local browser admission prerequisites (R004.6)

`tests/browser_control/test_local_browser_transport.py` checks the distinct,
explicitly configured transport. It preserves canonical anonymous identity and
raw Origin refusal. It covers exact Origin/Host, loopback peers, schemes,
credential/proxy refusal and mode checks on every request. It grants no authority.

`tests/access/test_action_policy_source.py` checks the canonical policy owner's
strict configured-source revision. It covers normalization, absent/fallback and
incomplete sources, same-snapshot evaluation, and source changes during audit or
caller waits. Exact policy requests bind the revision into their existing digest.
These prerequisite tests do not qualify the full attended browser path.

`tests/access/test_action_policy_source_async.py` checks same-loop source reads.
Released-native checks compare revisions and exercise public query decoding.
Cancellation checks require file-worker completion before task completion.
Source substitution, caller liveness and redacted failures must refuse.
These tests do not qualify factory admission, effects or shared-pool coexistence.

The isolated owner tests exercise genuine native lease issue, read and retirement.
They check scope/task substitution, cancellation, source changes and native revocation.
Retained file work must preserve all occupied capacity slots through cancellation.
Repeated same-loop owner checks must not reproduce pending-reader warnings.
No fixture or native lease JSON establishes factory or operation authority.

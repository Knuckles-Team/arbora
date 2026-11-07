# Identity and authentication

For the current explicit credentialless local instance, follow
[the anonymous deployment runbook](deployment.md#explicit-local-anonymous-mode).
That path has a fixed anonymous principal, no bootstrap user or claim token,
and a separate anonymous graph store. It cannot be converted in place by the
identity-broker transition commands described below. Those commands describe
the separately released broker-managed account lifecycle.

> The identity broker, local users, and mode transitions are available from the identity-broker release. Check the installed release and [capability status](status.md) before following this runbook. Earlier releases use the external OIDC configuration described in [deployment](deployment.md).

Arbora keeps identity state in the engine and serves browser sign-in from its own identity broker. The durable mode is `none`, `local`, or `external`. `ARBORA_AUTH_MODE` seeds only a fresh store; a later environment edit does not change the stored mode. Use `arbora-identity transition` to change it. The engine still applies the same scopes and RBAC decisions in each mode.

## Before first boot

1. Generate the configuration for the chosen profile and run `setup-config doctor --profile <profile>`. A production profile must not use an automatically generated `GRAPH_SERVICE_AUTH_SECRET` or an exposed `none` mode.
2. Deliver the engine data-key reference, arbora service credential, issuer signer material, `ARBORA_IDENTITY_ISSUER`, `ARBORA_IDENTITY_TENANT`, and optional `ARBORA_SETUP_CODE` through the deployment's secret backend. Store references in configuration, not secret values. The arbora service credential must match the engine's copy.
3. Put a TLS endpoint in front of any non-loopback browser listener. Keep the `none` profile loopback-only. Do not use `ARBORA_AUTH_NONE_EXPOSE` to turn a demo into a production deployment.
4. Back up the engine store and test restore before changing a live identity mode. Record the running image digest as the rollback anchor.

The first administrator for a fresh `local` or `external` store uses `/auth/setup` and the one-time setup code. A `none` install begins with `usr:bootstrap`; claim it with `arbora-identity claim --username <name>` before switching to local. The CLI prompts for passwords and second factors. Do not pass them on a command line or write them into a log.

## Harden a demo install

1. Run `setup-config doctor` and confirm the stored mode, bind address, issuer, tenant, secret delivery and policy status.
2. Claim the bootstrap administrator, choose an administrator-only registration policy, and enrol MFA. Administrators and approvers must have MFA. Keep recovery codes in the operator's secret store.
3. If email is unavailable, use administrator-issued one-time reset tokens and recovery codes. A reset token is shown once; a reset also revokes the user's sessions.
4. Rotate any auto-generated arbora service secret in both arbora and the engine, then restart the affected units together.
5. In local mode, prove a normal user and an administrator can sign in through the browser. Inspect `/auth/session` after each sign-in; a health request or a service-token call does not prove human login.
6. Configure an external IdP only after its issuer, redirect URI, trust roots and scope mappings are reviewed. Dry-run mapping rules on sample claims, especially rules that could grant `identity:admin` or an approver group.
7. Link the existing administrator to the IdP before transition. Run the link migration dry-run and require **zero ownership changes**. Then transition to `external` with `local_fallback=break-glass` and verify both the external browser path and the MFA-protected local break-glass account.
8. Export and verify the identity audit chain. Recheck sign-in, session revocation and engine authorization after the cutover.

Mode transitions rotate the local issuer signer and revoke sessions. Plan a maintenance window and a fresh administrator sign-in. A binary rollback cannot undo an identity-store transition; use the stored-mode CLI and an operator-tested recovery path. Never create a second identity store or copy a production reset token into a test environment.

## Native MCP client sign-in

Coding clients connect to the served `/mcp` endpoint with their own OAuth login (authorization code with PKCE). Arbora owns the OAuth client they sign in with. It declares the client and its client scopes, and it reconciles them in the identity provider at start and every five minutes. A client that a cleanup deletes is recreated without an operator.

What arbora declares:

- A public client (default id `arbora-native-mcp`) that allows the authorization-code flow only and requires PKCE `S256`. It has no secret, no implicit flow, no password grant and no service account.
- Loopback redirect URIs for the native clients: by default `http://localhost:33418/callback` and `http://127.0.0.1:19876/mcp/oauth/callback`.
- The default client scopes `mcp-fleet`, `roles`, `agent-services`, `offline_access`, `profile` and `email`. `offline_access` makes the refresh token an offline token, so a long session does not need a new login.
- The client scopes `mcp-fleet` and `agent-services`. `agent-services` carries an audience mapper for the configured audience, so issued tokens carry the audience that arbora verifies. `mcp-fleet` is shared with other clients, so arbora only makes sure it exists.

The deployment supplies configuration only, as one JSON object in `ARBORA_IDENTITY_CLIENT_CONTRACT`:

```json
{
  "admin_url": "https://idp.example",
  "realm": "<served realm>",
  "admin_realm": "master",
  "admin_client_id": "<scoped admin client>",
  "admin_client_secret_ref": "vault://<path>#client_secret",
  "native_client_id": "arbora-native-mcp",
  "native_redirect_uris": ["http://localhost:33418/callback"],
  "audience": "agent-services",
  "ca_bundle": "/path/to/ca.pem"
}
```

The first four keys are required. The others have the defaults shown above. `admin_client_secret_ref` is a secret reference (`env://`, `vault://` or `secret://`), never a value. The admin client must be limited to managing clients and client scopes of the served realm. Arbora refuses a token that holds the realm role `admin` or `create-realm`, reporting `identity_admin_too_broad`.

Reconciliation only creates and updates the items arbora declares. It never deletes or changes any other client or scope, and it never removes a dynamically registered client. If the provider cannot be reconciled, arbora keeps serving: issued tokens stay valid, and native clients can still register dynamically. `/health/ready` then reports `"identity": {"status": "degraded", "reason": "identity_<code>"}` without changing `status`. It reports `"identity": {"status": "ready"}` once the contract matches. The codes are `admin_credential_missing`, `admin_refused`, `admin_too_broad`, `admin_unreachable`, `client_drift`, `client_missing` and `scope_missing`. A degraded reconciler retries with exponential backoff, capped at five minutes.

To run one pass on demand, for example from a deployment job, use:

```bash
arbora identity reconcile
```

It prints one JSON line with `state`, `code`, `items` and `changed`. It exits `0` when the contract has converged, `1` when it is degraded, and `2` when `ARBORA_IDENTITY_CLIENT_CONTRACT` is not set. Do not keep a separate provisioning script with its own copy of the client definition.

## Browser acceptance

A qualifying probe exercises the actual sign-in form or IdP redirect, then requests `/auth/session` with the resulting browser cookie. It asserts the expected principal and server-resolved role. The local path also completes MFA for the test administrator. The external path starts at `/auth/oidc/{idp_id}/login`, follows the IdP authorization-code redirect, and verifies the callback sets `__Host-arbora_session`. A successful discovery document or Keycloak token endpoint is preparatory evidence only.

Use dedicated, non-production test users and keep their credentials in the deployment secret backend. Run the browser probe again against the production URL after rollout; a test namespace pass alone does not qualify production. Keep probe output free of cookies, tokens, credentials, and one-time codes.

For scopes, approver groups, service identities and mode-specific troubleshooting, see the deployment skill's `identity-and-access.md` reference. Browser UI role rendering never grants engine authority; the API and engine check the caller on every operation.

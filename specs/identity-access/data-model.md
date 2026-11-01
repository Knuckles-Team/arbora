# Data model — ARBORA-IDENTITY

Arbora keeps no durable identity tables. Every record below is a typed in-process model or a record the engine stores; the engine node labels are owned by the engine specs.

- `LoopbackBootstrapPrincipal`, `MfaEnrollment`, `AdminConsoleTab`, `ApiKeyGrant`, `OidcMappingRule`, `LdapBindConfig`, `ScimServiceCredential`, `ScimUser`, `SamlServiceProvider`, `ParsedSamlAssertion` and `SmtpAdapterSettings` are frozen dataclasses in `arbora/identity/` that refuse invalid construction.
- `IssuerSettings`, `LocalIssuer`, `Resolution` and `SessionRecord` carry the issued claims and the strict session facts read from the engine.
- `BearerVerificationProfile` and `VerifiedBearer` (planned, `arbora/security/bearer.py`) carry the stateless bearer checks and the verified tenant.
- `ResourceAccessRequest` and `ResourceGrant` (planned, `arbora/access/resource_grants.py`) carry exact resource grants; `CredentialUseRequest`, `CredentialUseGrant` and `AuthorizationChallenge` (planned, `arbora/access/credential_use.py`) carry scoped credential reuse; `DelegatedIdentity` (planned, `arbora/access/delegation.py`) carries the exchanged actor.
- `ClientSession` (planned, `arbora/identity/client_session.py`) is stored by the engine as a node labelled `ClientSession` under the id `client_session:<tenant>:<session_id>`.
- The scope registry maps each scope name to one class (user, domain, service-only, approver, admin) in `arbora/security/scope_registry.py`.

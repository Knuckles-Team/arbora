# ARBORA-ACP — Conversational ACP gateway session projection and policy

**Owner:** arbora. **Requirement ID:** ARBORA-ACP-R001 (Arbora session-admission partition).

## Purpose and user stories

Agent Terminal UI's architecture names conversational ACP for the terminal client as an explicit contract gap: REST is the live served path today, and no Arbora-hosted ACP (JSON-RPC + SSE) session exists yet. This spec covers only the Arbora projection and session policy for that gateway — admitting, binding, bounding and refusing an ACP session — not the chat adapter's conversation or agent-decision logic, which meristem owns, and not the terminal client's rendering, which tuig owns.

P0: A terminal client opens an ACP session carrying a verified Arbora principal; Arbora admits it only with that principal and binds the session to it for its lifetime. P0: An ACP session that goes idle past its configured limit, or exceeds its absolute lifetime, is refused on the next use rather than silently extended. P0: While the meristem chat adapter is not reachable, every ACP session request receives one typed "chat adapter unavailable" refusal instead of a silent REST fallback, a fabricated reply, or an unauthenticated pass-through.

## User Scenarios & Testing

### User Story 1 - Open a bound ACP session (Priority: P1)

A terminal client opens an ACP session with a verified Arbora principal and expects every later message to run under exactly that identity.

**Acceptance Scenarios**:

1. **Given** a request carrying a verified principal, **When** the client opens an ACP session, **Then** the session is admitted and bound to that principal, tenant and scope snapshot.
2. **Given** a request with a missing or invalid credential, **When** the client opens a session, **Then** a typed refusal is returned and no session exists.

### User Story 2 - Idle and expired sessions are refused (Priority: P1)

An operator relies on the configured idle limit and absolute lifetime to bound how long a session can act.

**Acceptance Scenarios**:

1. **Given** a session idle past the configured limit, **When** the client sends a message, **Then** the request is refused with a typed idle-expired reason.
2. **Given** a session past its absolute lifetime, **When** the client sends a message, **Then** the request is refused with a typed expired reason.

### User Story 3 - Adapter unavailable is explicit (Priority: P1)

A client must learn plainly when the chat adapter cannot serve it.

**Acceptance Scenarios**:

1. **Given** the chat adapter is unreachable, **When** a client opens a session or sends a message, **Then** one typed `chat_adapter_unavailable` refusal is returned and no reply is fabricated.

### Edge Cases

- A later message tries to widen scope on an existing session: the bound snapshot stays unchanged.
- A message arrives immediately after the absolute expiry: it is refused even though activity is recent.
- Status is read while the adapter is down: it reports the unavailable state and the limits without any token or secret.

## Assumptions

- The identity path of the access-control spec is the only credential authority.
- The chat adapter is owned by Meristem and may be absent; the refusal behavior is required either way.

## Functional requirements and acceptance

| ID | Requirement | Observable success |
|---|---|---|
| ACP-01 | Admit an ACP session only for a request carrying a verified Arbora principal produced by the existing identity path ([ARBORA-IDENTITY-R001](../identity-access/spec.md)); an absent, malformed, expired or unverifiable credential refuses before a session object is created. | Admission check returns a typed refusal for missing/invalid credentials; no session record exists afterward. |
| ACP-02 | Bind an admitted session to its exact principal ID, tenant and scope snapshot at admission time. The binding is immutable for the session's life and is never widened by a later message on the same session. | A session created for principal A cannot be reused, reassigned or escalated by any later request. The bound snapshot is unchanged on read. |
| ACP-03 | Enforce a configured idle limit: a session with no accepted message within that window is refused on its next use and does not auto-renew. | A session idle past the configured limit is refused with a typed idle-expired reason on next access; it is not silently kept alive. |
| ACP-04 | Enforce a configured absolute session lifetime independent of activity; a session past that lifetime is refused regardless of recent activity. | A session past its absolute expiry is refused even immediately after a message, with a typed expired reason. |
| ACP-05 | While the meristem chat adapter (`MERISTEM-RUN-R001`) is absent or unreachable, every ACP session admission and message returns one typed `chat_adapter_unavailable` refusal. Arbora never substitutes a local reply, silently falls back to REST, or admits the session without the adapter. | With the adapter marked unavailable, admission and message calls both return the same typed refusal code. No session is admitted and no reply is synthesized. |
| ACP-06 | The ACP session's principal, tenant and scope projection is the same verified identity path used by REST and MCP. No second ACP-only authority or shadow session store is introduced. | The ACP and REST paths for the same credential resolve to an identical principal/tenant/scope snapshot. |
| ACP-07 | Arbora status/doctor reports the ACP gateway's availability state (adapter reachable/unavailable) and session-policy limits without exposing raw credentials or session secrets. | A status read shows the adapter state and configured idle/absolute limits; no token or session secret appears in the output. |

## Architecture, interface, and boundaries

Arbora owns ACP session admission, identity binding, idle/expiry policy, the typed adapter-unavailable refusal, and reporting this state through its existing status/doctor surface. [meristem](https://github.com/Knuckles-Team/meristem) owns the chat adapter itself and all agent decisions made inside an admitted session (`MERISTEM-RUN-R001`); Arbora calls that adapter and never re-implements routing, model selection or task decomposition. [tuig](https://github.com/Knuckles-Team/tuig) owns the terminal client and its protocol adapter/runtime ([`docs/architecture.md`](https://github.com/Knuckles-Team/tuig/tree/main/docs/architecture.md) "Protocol connection" and "System overview"; the repository has no published stable-ID spec for this surface yet, so it is linked by document, not by ID) that opens and renders the ACP session from the other side of this contract. [Access control](../identity-access/spec.md) remains the sole identity, session-cookie and token authority Arbora consumes.

Out of scope here: the chat adapter's conversational behavior, the terminal UI's rendering or local persistence, and any new identity issuer. This spec does not require meristem's adapter to exist to be implementable: ACP-05 is exactly the behavior required while it does not.

## Success criteria and traceability

| Requirement | Requirement ID | Design | Tests | Acceptance evidence |
|---|---|---|---|---|
| ACP-01 | ARBORA-ACP-R001 | [Admission](plan.md#admission-and-binding) | T-ACP-01, T-ACP-02 | exact commit, unit evidence |
| ACP-02 | ARBORA-ACP-R001 | [Admission](plan.md#admission-and-binding) | T-ACP-03 | unit evidence |
| ACP-03 | ARBORA-ACP-R001 | [Policy limits](plan.md#idle-and-expiry-policy) | T-ACP-04 | unit evidence |
| ACP-04 | ARBORA-ACP-R001 | [Policy limits](plan.md#idle-and-expiry-policy) | T-ACP-05 | unit evidence |
| ACP-05 | ARBORA-ACP-R001 | [Adapter refusal](plan.md#adapter-unavailable-refusal) | T-ACP-06, T-ACP-07 | unit evidence |
| ACP-06 | ARBORA-ACP-R001 | [Admission](plan.md#admission-and-binding) | T-ACP-08 | integration evidence |
| ACP-07 | ARBORA-ACP-R001 | [Observability](plan.md#observability) | T-ACP-09 | unit evidence |

This spec represents only the Arbora session-admission and policy contract of ARBORA-ACP-R001. The meristem chat adapter and the tuig client each have their own owner and must satisfy this contract from their side before an end-to-end conversational ACP session can be accepted.

Requirement IDs are defined in [requirements.md](requirements.md); delivery state per ID is in `status.json`.

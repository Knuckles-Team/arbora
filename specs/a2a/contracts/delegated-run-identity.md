# Delegated run identity

Seam between `ARBORA-A2A` and Medulla. Owner rows: `MEDULLA-IDENTITY-R744` in the medulla repository and `ARBORA-A2A-R001.2.2.1` in this repository.

Provider: medulla MEDULLA-IDENTITY-R744
Consumer: arbora ARBORA-A2A-R001.2.2.1

## Wire shape

A reclaimed run executes under the host's own session. The verified request envelope of each engine call carries one extra optional claim, `delegation_id`, taken from the run's claim. The engine then runs the call as the delegation's principal, the run's owner, with exactly the delegation's operations as scopes. The host session must hold the service-only scope `identity:delegate`. A first run, and every call of the host that is not a reclaimed run, carries no `delegation_id`.

## Refusal codes

- `DELEGATION_INVALID`: the delegation is unknown, another tenant's, revoked, expired, not of kind `agent_run`, or its principal no longer holds every operation. The engine answers all of these alike, so the claim is no existence oracle. The run ends `failed`.
- `ACCESS_DENIED`: the signing host lacks `identity:delegate`. Arbora checks the scope first and refuses the same way before any step runs.

Arbora never falls back to acting as the host. A release whose envelope claim or served scope registry lacks the delegation refuses `UNAVAILABLE` naming the missing shape.

## Authoritative types

- Medulla `RequestContextClaims.delegation_id` and the scope `identity:delegate`.
- `arbora/a2a/run_delegation.py`: `acting_session`.
- `arbora/pin_lag/medulla.py`: `require_delegated_identity`.

## Test vectors

- `tests/a2a/test_checkpoint_resume_served.py::test_reclaimed_run_sends_the_delegation_claim_as_the_owner`.
- `tests/a2a/test_checkpoint_resume_served.py::test_reclaim_refuses_a_host_without_the_delegate_scope`.
- `tests/a2a/test_checkpoint_resume_served.py::test_a_first_run_carries_no_delegation_claim`.

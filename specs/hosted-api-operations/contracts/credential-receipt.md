# Credential receipt

Seam between `ARBORA-OPS` and Medulla. Owner rows: `MEDULLA-IDENTITY-R753` in the medulla repository and `ARBORA-OPS-R893` in this repository.

Provider: medulla MEDULLA-IDENTITY-R753
Consumer: arbora ARBORA-OPS-R893

## Wire shape

The engine method `CredentialReceipt` takes `{op: {op: "record", receipt}}` and answers `{receipt_id}`. The receipt is `{tenant, client, connector, resource, operation, outcome, binding_digest, at_ms}`. `binding_digest` is always `sha256:<64 hex>`: the admitted binding's digest, or for a refused call the digest of the ask. The write needs the `security:audit-write` scope.

## Refusal codes

- `ACCESS_DENIED`: the receipt names a tenant other than the caller's. Arbora forwards the code unchanged.

## Authoritative types

- Medulla `CredentialReceipt`, `CredentialReceiptOpRecord` and `CredentialReceiptRecorded`.
- `arbora/gateway/credential_engine.py::EngineReceiptSink` and `arbora/gateway/credential_authority.py`.

## Test vectors

- `tests/gateway/test_credential_authority.py::test_an_admitted_call_writes_one_ok_receipt_with_the_digest`.

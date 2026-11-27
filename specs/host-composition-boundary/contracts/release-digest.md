# Release digest seam with the connector SDK and the release workflow

Provider: scion-sdk SCION-CONNECTOR-CONTROL-R650.1.4
Provider: pipelines PIPE-RELEASE-R019
Consumer: arbora ARBORA-HOST-R037

## Wire shape

The release workflow records every connector release as a sealed engine `Release` node `release:<distribution>:<version>` whose `artifact_digests` include `content-sha256:<hex>`, the content digest of the wheel. The connector SDK's runner reads that node through the verified engine client that arbora hands to `default_services` and requires the digest a connector reports at `connector://release` to be among them. Arbora passes the client, the sink name and the pack-import authority. It passes no lock path, no pin and no setting that disables verification.

## Refusal codes

- `RELEASE_DIGEST_UNREPORTED`, `RELEASE_EVIDENCE_INVALID`, `RELEASE_PIN_UNAVAILABLE`, `RELEASE_PIN_MISSING` and `RELEASE_DIGEST_MISMATCH` from the runner, per connector and cycle.
- `ConnectorRunnerNotReadyError` when the injected sink is not ready.

## Authoritative types

- `arbora/connector_runner.py` `ConnectorRunnerComposition.services`.
- scion-sdk `scion_sdk/runner/composition.py` `default_services` and `scion_sdk/runner/release_pins.py` `MedullaReleasePins`.

## Test vectors

- `tests/test_connector_runner_composition.py::test_the_verified_client_reaches_the_sdk_for_release_pins`.
- `tests/test_connector_runner_composition.py::test_release_verification_has_no_lock_setting`.

## Owner rows

- Provider: scion-sdk SCION-CONNECTOR-CONTROL-R650.1.4 and pipelines PIPE-RELEASE-R019.
- Consumer: arbora ARBORA-HOST-R037.

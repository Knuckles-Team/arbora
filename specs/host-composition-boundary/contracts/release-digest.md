# Release digest seam with the connector SDK and the package index

Provider: scion-sdk SCION-CONNECTOR-CONTROL-R650.1.4
Provider: medulla MEDULLA-SDLC-R002.3
Consumer: arbora ARBORA-HOST-R037
Consumer: arbora ARBORA-HOST-R037.1

## Wire shape

Arbora records each connector release as a sealed engine `Release` node `release:<distribution>:<version>`. It builds the record from the public package index, not from a release job: it reads the JSON metadata of the exact distribution and version, downloads the wheel, checks the wheel's sha256 against the index, and computes the wheel content digest with the connector SDK's `wheel_release_evidence`. `artifact_digests` holds `sha256:<hex>` for the wheel and the sdist and `content-sha256:<hex>` for the content digest. The engine stores the record as the JSON text property `release_record`. The connector SDK's runner reads that record through the verified engine client that arbora hands to `default_services`, in the connector's own graph, and requires the digest a connector reports at `connector://release` to be among them. Arbora passes the client, the sink name and the pack-import authority. It passes no lock path, no pin and no setting that disables verification. No key, credential, endpoint or ingress takes part.

## Refusal codes

- Recording, reported by onboarding and never fatal to it: `RELEASE_INDEX_UNAVAILABLE`, `RELEASE_INDEX_INCOMPLETE`, `RELEASE_INDEX_DIGEST_MISMATCH`, `RELEASE_WHEEL_INVALID`, and the engine's `RELEASE_HEAD_UNKNOWN`, `RELEASE_VERSION_TAKEN`.
- `RELEASE_DIGEST_UNREPORTED`, `RELEASE_EVIDENCE_INVALID`, `RELEASE_PIN_UNAVAILABLE`, `RELEASE_PIN_MISSING` and `RELEASE_DIGEST_MISMATCH` from the runner, per connector and cycle.
- `ConnectorRunnerNotReadyError` when the injected sink is not ready.

## Authoritative types

- `arbora/fleet/release_record.py` `ReleaseRecorder`, `PackageIndex` and `release_request`.
- `arbora/connector_runner.py` `ConnectorRunnerComposition.services`.
- scion-sdk `scion_sdk/runner/composition.py` `default_services` and `scion_sdk/runner/release_pins.py` `MedullaReleasePins`.

## Test vectors

- `tests/fleet/test_release_record.py::test_onboarding_records_the_release_from_the_index_and_the_runner_accepts`.
- `tests/fleet/test_release_record.py::test_a_wheel_that_differs_from_the_index_digest_is_not_recorded`.
- `tests/test_connector_runner_composition.py::test_the_verified_client_reaches_the_sdk_for_release_pins`.
- `tests/test_connector_runner_composition.py::test_release_verification_has_no_lock_setting`.

## Owner rows

- Provider: scion-sdk SCION-CONNECTOR-CONTROL-R650.1.4 and medulla MEDULLA-SDLC-R002.3 (`RecordRelease`, which records a release with no source commit).
- Consumer: arbora ARBORA-HOST-R037 and ARBORA-HOST-R037.1.

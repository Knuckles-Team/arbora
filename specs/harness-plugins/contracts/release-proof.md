# Release proof

Seam between `ARBORA-HARNESS-PLUGINS` and pipelines. Owner rows: `PIPE-RELEASE-R028` in pipelines and `ARBORA-HARNESS-PLUGINS-R001.4` in this repository.

Provider: arbora ARBORA-HARNESS-PLUGINS-R001.4
Consumer: pipelines PIPE-RELEASE-R028

## Wire shape

The release workflow runs `arbora plugin bundle --harness <id> --out <tmp>` for each profile. It runs the harness validator where the runner has it, for example `claude plugin validate --strict <tmp>`. It runs the structural validator for each other profile. It uploads no bundle and commits no bundle.

## Refusal codes

- `HARNESS_UNKNOWN`, `ENDPOINT_UNSET` and `reingestion-unreconciled`: the bundle command exits 2 and the release proof fails.
- `BUNDLE_COMMITTED`: a tracked file matches a generated bundle path.

## Test vectors

- `tests/harness_plugins/test_bundle_served.py::test_console_builds_every_profile_and_each_validates`.

# Paired native-history restart gate

Status: prepared, **not executed** against the new producer artifacts. Passing
the controlled-model unit tests is not native or service-restart evidence.

## Required inputs

- Matching installed Medulla client/server artifacts containing explicit
  `completed_text_history` read/claim opt-in, private terminal snapshots and
  fresh-execution refusal. Generated schemas alone are insufficient.
- The candidate Arbora wheel with canonical regenerated registry and native
  history/extension binding; released Meristem's checkpoint provider seam.
- The matching native Tuig candidate with `--continue`, strict discovery and
  activation validation. Set `ARBORA_HISTORY_TUIG_BIN` to its actual binary.
- Linux x86_64, sufficient disk/memory for two isolated native-store runs,
  loopback networking and installed bootstrap/pytest dependencies.
- Real native task-plan decision/publication authority. The fixture controls
  model JSON proposals, never native decision receipts or authorization.
- An isolated retained graph enabled by the actual authenticated `kg:admin`
  operator fixture. No production graph, credentials or services are used.

## Execution

Run outside the source tree in the candidate installed environment, with no
source overlays. Copy this test and `tests/test_real_boot.py`,
`tests/_boot_model.py`, `tests/_boot_connector.py`,
`tests/_boot_ingest_connector.py` into a temporary `tests/` package (including
its `qualification/` subpackage). Retain the existing pytest marker setup.

```sh
ARBORA_RUN_NATIVE_HISTORY_ACCEPTANCE=1 \
ARBORA_HISTORY_TUIG_BIN=/absolute/path/to/candidate/tuig \
python -I -m pytest -p no:cacheprovider -rA \
  --basetemp=/absolute/path/to/isolated/evidence \
  --junitxml=native-history-restart.xml \
  tests/qualification/test_native_history_restart.py
```

Require **two passed, zero skipped, zero failed/errors**. The explicit flag
prevents ordinary source suites from accidentally provisioning native stacks.
When enabled, missing installed modules, binaries, capability or runtime
authority fail the test; they do not become acceptance skips.

## Evidence and limits

Both cases use real producer and Arbora subprocesses and stop/restart both
against the same store. They recover the first durable reply, replay its same
message/key without additional model calls, reject a foreign-tenant read, then
send only a fresh second prompt. Actual model request messages must contain
the first user and assistant turns. The first turn invokes the existing
governed `act(fleet.call)` echo tool; its connector invocation count and native
effect receipt identity must remain unchanged through restart and continuation.

`overstory-wire` repeats the browser's `webui:/<id>` label without an extension
header or client transcript. It is **backend wire-contract evidence**, not a
real-browser Overstory journey. `tuig` invokes the actual native CLI for the
second turn. Full browser acceptance and same-tenant distinct-owner coverage
remain additional gates; the included foreign-tenant case does not replace them.

The test records synthetic model-input/effect JSONL, process ID changes,
package versions and executable SHA-256 values in the pytest evidence directory.
Retain those with exact input artifact and producer/consumer commit provenance.
No claim is made that a source-shaped fixture or the evidence filenames alone
prove native receipt validity. The store/claim/commit paths are never mocked.

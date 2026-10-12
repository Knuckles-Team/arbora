# Release-qualification runbook

Companion procedure to the organization's exit-criteria matrix
(<https://knuckles-team.github.io/.github/build-first.html>). Each stage
below names the exact automated check or script a reviewer runs directly;
none depend on private infrastructure. Run the stages in order — a stage
is qualified only once every check it names passes on the revision being
released.

A failing stage stops qualification of this release candidate at that
stage. Record the exact revision, command, and result for each stage.
Do not qualify a later stage from an earlier revision.

## 1. Graph engine (`epistemic-graph`)

```bash
gh run list --workflow=release.yml -R Knuckles-Team/epistemic-graph --branch main --limit 1
```

The listed run must be the one built from the commit this release pins.
Re-run it directly with `gh run rerun <run-id> -R Knuckles-Team/epistemic-graph`
if it needs re-proving.

## 2. Connector SDK (`agent-connector-sdk`)

```bash
gh run list --workflow=release.yml -R Knuckles-Team/agent-connector-sdk --branch main --limit 1
```

Same proof shape as stage 1, against the revision this release's
`agent-connector-sdk` dependency pins.

## 3. Agent runtime (`agent-utilities`)

```bash
gh run list --workflow=release.yml -R Knuckles-Team/agent-utilities --branch main --limit 1
```

Same proof shape as stage 1, against the revision this release's
`agent-utilities` dependency pins.

## 4. GraphOS itself

From a clean GraphOS checkout at the candidate revision, after
`scripts/bootstrap.sh`:

```bash
uvx pre-commit run --all-files
uvx pre-commit run --hook-stage pre-push --all-files
uv run --no-sync pytest tests/deployment
uv build --wheel --out-dir dist
```

The release gates cache the final numeric extension with
`scripts/numeric_artifact_cache.py`. Its exact key binds the EG revision, manifests, locks, and build recipe.
It also binds Rust, maturin, Python ABI, platform, and build flags. There are no fallback keys.
The key includes both Cargo config filenames at the build working directory,
every ancestor, and `CARGO_HOME`. File locations and contents both affect it.
Cargo include directives stop identity generation before cache restore.
They require explicit support before this cache path accepts them.

Every hit must pass the receipt digest, artifact path, kernel marker, and
numeric smoke check under the locked Python interpreter. A rejected hit
rebuilds; a failed build or smoke check stops the job before cache save.
Actions cache cannot replace an existing entry for the same key.
Remove an invalid entry. A later build seeds it after the smoke check.

The Rust intermediate cache remains available for misses. A final-artifact
hit can avoid compilation, but cold and warm native timings still need
hosted evidence. No type, test, or scanner gate is bypassed by a hit.

Install the built wheel into a fresh environment and certify it is the
one that will be promoted:

```bash
graph-os-release-canary
```

`graph-os-release-canary` fails closed (nonzero exit, no partial pass) when
a declared console entry point, the packaged engine binary, the folded
numeric kernel, or the served FastMCP major is missing or mismatched — see
`graph_os/deployment/release_canary.py`.

## 5. Web UI (`agent-webui`, pending an in-flight package rename)

```bash
gh run list --workflow=release.yml -R Knuckles-Team/agent-webui --branch main --limit 1
```

Same proof shape as stage 1. The optional `webui` extra's package name
changes once that in-flight rename lands in both `agent-webui` and here;
this stage's check does not depend on that name.

## 6. Connector package fleet

```bash
agent-utilities-doctor --only mcp_fleet mcp_fleet_secrets --live
```

Both checks must report healthy for every connector package this release's
fleet catalog declares; `mcp_fleet` proves live reachability
(`--live`), `mcp_fleet_secrets` proves each declared secret reference
resolves. See `graph_os/deployment/doctor.py`'s `CHECKS` table.

## 7. Production redeployment

```bash
graph-os-production-ops backup --archive-root <path>
graph-os-production-ops restore-validate --archive-root <path> --scratch-root <path>
```

A qualified release must restore-validate from its own backup before
redeployment proceeds. See `graph_os/deployment/production_ops.py`.

## Scope and limits

This runbook proves each stage's own automated check; it is not itself a
live first-boot, identity, image-pull, or CNI-enforcement acceptance
receipt. Those remain tracked separately in the portable-deployment work
plan, `specs/portable-deployment/tasks.md`.

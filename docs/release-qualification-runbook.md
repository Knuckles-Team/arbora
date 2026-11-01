# Release-qualification runbook

Companion procedure to the organization's exit-criteria matrix
(<https://knuckles-team.github.io/.github/build-first.html>). Each stage
below names the exact automated check or script a reviewer runs directly;
none depend on private infrastructure. Run the stages in order. A stage
is qualified only once every check it names passes on the release revision.

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

## 2. Connector SDK (`scion-sdk`)

```bash
gh run list --workflow=release.yml -R Knuckles-Team/scion-sdk --branch main --limit 1
```

Same proof shape as stage 1, against the revision this release's
`scion-sdk` dependency pins.

## 3. Agent runtime (`meristem`)

```bash
gh run list --workflow=release.yml -R Knuckles-Team/agent-utilities --branch main --limit 1
```

Same proof shape as stage 1, against the revision this release's
`meristem` dependency pins.

## 4. Arbora itself

From a clean Arbora checkout at the candidate revision, after
`scripts/bootstrap.sh`:

```bash
uvx pre-commit run --all-files
uvx pre-commit run --hook-stage pre-push --all-files
uv run --no-sync pytest tests/deployment
uv build --wheel --out-dir dist
```

The release builds from this repository alone. Every dependency installs from
PyPI at the version `uv.lock` pins. That includes the epistemic-graph engine
wheels, so the release compiles nothing. Before a tag publishes,
`install-smoke` installs the built wheel and the locked PyPI dependencies.
It checks every hash and runs on Linux x64, Linux arm64, macOS arm64 and
Windows x64. The macOS x64 and Windows arm64 legs also run and report. They
stay non-blocking until the engine publishes wheels for them.

The Rust intermediate cache remains available for misses. A final-artifact
hit can avoid compilation, but cold and warm native timings still need
hosted evidence. No type, test, or scanner gate is bypassed by a hit.

Install the built wheel into a fresh environment and certify it is the
one that will be promoted:

```bash
arbora-release-canary
```

`arbora-release-canary` fails closed (nonzero exit, no partial pass) when
a declared console entry point, the packaged engine binary, the folded
numeric kernel, or the served FastMCP major is missing or mismatched — see
`arbora/deployment/release_canary.py`.

## 5. Web UI (`overstory`, pending an in-flight package rename)

```bash
gh run list --workflow=release.yml -R Knuckles-Team/overstory --branch main --limit 1
```

Same proof shape as stage 1. The optional `webui` extra's package name
changes once that in-flight rename lands in both `overstory` and here;
this stage's check does not depend on that name.

## 6. Connector package fleet

```bash
arbora-doctor --only mcp_fleet mcp_fleet_secrets --live
```

Both checks must report healthy for every connector package this release's
fleet catalog declares; `mcp_fleet` proves live reachability
(`--live`), `mcp_fleet_secrets` proves each declared secret reference
resolves. See `arbora/deployment/doctor.py`'s `CHECKS` table.

## 7. Production redeployment

```bash
arbora-production-ops backup --archive-root <path>
arbora-production-ops restore-validate --archive-root <path> --scratch-root <path>
```

A qualified release must restore-validate from its own backup before
redeployment proceeds. See `arbora/deployment/production_ops.py`.

## Scope and limits

This runbook proves each stage's own automated check; it is not itself a
live first-boot, identity, image-pull, or CNI-enforcement acceptance
receipt. Those remain tracked separately in the portable-deployment work
plan, `specs/portable-deployment/tasks.md`.

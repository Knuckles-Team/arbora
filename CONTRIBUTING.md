# Contributing to Arbora

Arbora is the deployable MCP, REST, control-plane, fleet, and WebUI
composition for the Knuckles agent platform. Read [AGENTS.md](AGENTS.md) for
source boundaries and development rules, and the
[organization contribution guide](https://github.com/Knuckles-Team/.github/blob/main/CONTRIBUTING.md)
for choosing work.

## Development setup

Every dependency installs from PyPI at the version `uv.lock` pins, so no
sibling source checkout is needed. Run:

```bash
scripts/bootstrap.sh              # uv >= 0.9, pinned Python, locked PyPI deps, git hooks
```

Bootstrap is idempotent. A hook whose prerequisite is missing prints
`SKIPPED (<gate>): <reason>` locally and fails closed (`CANNOT RUN`, exit 2)
in CI, so a fresh clone can commit while CI still runs every gate.

## Before the operator push

```bash
uvx pre-commit run --all-files
uvx pre-commit run mypy-env --hook-stage manual --all-files
uvx pre-commit run pytest --hook-stage manual --all-files
```

The release workflow runs this same `.pre-commit-config.yaml`, so a green local
run is the CI gate minus provisioning. Wiring gate terms are in
[docs/quality-gate-terms.md](docs/quality-gate-terms.md).

## Branch and pull-request flow

1. Branch from current `origin/main` (never commit to or push `main`), in the operator's
   own Git worktree when the checkout is shared.
2. Commit in logical steps, staging explicit paths only.
3. Push with `git push -u origin <branch>` and open a draft pull request
   against `main`. Mark it ready once hosted CI is green.

Report vulnerabilities through
[GitHub Security Advisories](https://github.com/Knuckles-Team/arbora/security/advisories/new).

## Spec-driven contributions

Every change follows one process: constitution, route, specify, clarify, plan, tasks, analyze, implement, land, release.
Each step names its skill and the files it writes, in the
[organization contribution guide](https://github.com/Knuckles-Team/.github/blob/main/CONTRIBUTING.md).
Read the [constitution](https://github.com/Knuckles-Team/pipelines/blob/main/.specify/memory/constitution.md),
the [spec standard](https://github.com/Knuckles-Team/pipelines/blob/main/reference/spec-standard.md) and the
[spec templates](.specify/templates), and pick an open row in
[Canopy](https://knuckles-team.github.io/.github/architecture/). Commit with a `Spec: <ROW-ID>` trailer and a bound test.

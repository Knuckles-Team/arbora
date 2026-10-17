# Public source bootstrap

Start with the [organization contribution guide](https://github.com/Knuckles-Team/.github/blob/main/CONTRIBUTING.md)
and choose an owner spec from the [build-first queue](https://knuckles-team.github.io/.github/build-first.html).
This recipe mirrors the editable paths in the six core repositories' `pyproject.toml`
files and Arbora's public `.github/workflows/release.yml`. Use Python 3.12,
`uv`, Rust/Cargo for engine work, and the Node/pnpm versions required by WebUI.
All source URLs below are public GitHub HTTPS URLs.

Run from a new directory of your choice:

```bash
mkdir arbora-src
cd arbora-src
for repo in epistemic-graph agent-connector-sdk agent-utilities arbora agent-webui; do
  git clone "https://github.com/Knuckles-Team/${repo}.git"
done

mkdir -p agent-utilities/.uv-workspace-siblings agent-webui/.uv-workspace-siblings
ln -s ../../epistemic-graph agent-utilities/.uv-workspace-siblings/epistemic-graph
ln -s ../../agent-connector-sdk agent-utilities/.uv-workspace-siblings/agent-connector-sdk
ln -s ../../agent-utilities agent-webui/.uv-workspace-siblings/agent-utilities
```

For repository-manager work, also run:

```bash
git clone https://github.com/Knuckles-Team/repository-manager.git
mkdir -p repository-manager/.uv-workspace-siblings
ln -s ../../agent-utilities repository-manager/.uv-workspace-siblings/agent-utilities
```

For work in `pipelines`, `agent-terminal-ui`, `emerald-exchange`,
`tunnel-manager`, or the organization hub, clone only the owning public
repository and any public consumer or producer repository named by its spec:

```bash
git clone https://github.com/Knuckles-Team/pipelines.git
git clone https://github.com/Knuckles-Team/agent-terminal-ui.git
git clone https://github.com/Knuckles-Team/emerald-exchange.git
git clone https://github.com/Knuckles-Team/tunnel-manager.git
git clone https://github.com/Knuckles-Team/.github.git organization-hub
```

Read each chosen repository's `AGENTS.md` and `specs/README.md` for its
language-specific setup, fixtures, and gates. These extra repositories do not
require the six-core editable sibling layout merely to read or author specs.

These symlinks are ignored local source bindings, not files to commit. If one
already exists, inspect where it points rather than replacing it blindly.
Arbora needs no links: every dependency, the EG engine wheels included,
installs from PyPI at the versions its `uv.lock` pins. For the other
repositories, check each release workflow's `actions/checkout` `ref` values
and align the clones to those revisions when reproducing CI or a specific PR.
Using all current `main` branches is useful
for integration development, but may not match the frozen `uv.lock` or a
published package version. Do not resolve around a mismatch by silently
substituting an older index wheel. Report the incompatible revision pair and
update the intended lock/source contract in the owning repository.

Arbora's test setup follows `.github/workflows/release.yml`: from
`arbora-src/arbora`, `scripts/bootstrap.sh` syncs the locked PyPI
environment and installs the git hooks; the equivalent manual commands are:

```bash
uv sync --frozen --extra test --extra webui
uv run --no-sync pytest
```

AU's release workflow checks out a pinned
SDK and skips EG wheel installation. Its supported test environment uses
`uv sync --frozen --extra test --group guardrails --no-install-package
epistemic-graph --no-install-package langfuse-agent`, then the tests in its
workflow. The SDK's public workflow runs
`scripts/bootstrap_epistemic_graph_contract.sh`,
`uv sync --frozen --no-install-package epistemic-graph`, and
`uv run --frozen python -m pytest -q` with `PYTHONPATH=.ci/epistemic-graph`.
For direct engine work, its `CONTRIBUTING.md` gives the Cargo, Python, and hook
commands. WebUI and repository-manager have their own `AGENTS.md` bootstrap
and test commands; use their declared local source bindings above when a
published dependency lags.

For one repository's focused work, use its `AGENTS.md` commands and test
fixtures. A normal PR should be provable with local/generated fixtures and
disposable dependencies provisioned by its workflow. A release tag, production
deployment, or credentialed integration has separate prerequisites; record that
boundary in the test evidence. No private service is needed to read or author
the repository-owned specs.

# Arbora — repository guide

<!-- agent-contract:start -->
## Agent contract

Tool-neutral rules for any AI agent or human working in this repository. This block is maintained by the fleet standard; edit outside the markers.

- **Owner boundary:** this repository owns the one door: admission, auth, served operations, the MCP server and multiplexer, A2A serving, the registry and `~/.config/arbora` (spec prefix `ARBORA-`). Feature routing across repositories: [ecosystem boundaries](https://github.com/Knuckles-Team/pipelines/blob/main/reference/ecosystem-boundaries.md).
- **Setup:** run `scripts/bootstrap.sh`. It is idempotent and non-interactive: it pins uv and Python, syncs the locked environment, installs the git hooks and prints the gate command.
- **Gates:** `uvx pre-commit run --all-files` must pass before you push. Do not bypass hooks to get green.
- **Commits:** one logical change per commit with a `Spec: <ID>` trailer (or `Spec: none (<reason>)`), plus a `Co-Authored-By:` trailer naming the assisting agent. Stage explicit paths only; never `git add -A` or `git add .`. Use your own configured Git identity, never a tool or bot name.
- **Org rules:** [organization AGENTS.md](https://github.com/Knuckles-Team/.github/blob/main/AGENTS.md).
<!-- agent-contract:end -->

## Spec-driven development

Every feature starts with a spec, in this order. Read `.specify/memory/constitution.md` first; it inherits the [organization constitution](https://github.com/Knuckles-Team/pipelines/blob/main/.specify/memory/constitution.md). Route the feature with the [ecosystem boundaries](https://github.com/Knuckles-Team/pipelines/blob/main/reference/ecosystem-boundaries.md) and file each part in its owner's `specs/`. Load the `arbora-development` skill, which runs the flow. Rules: [spec standard](https://github.com/Knuckles-Team/pipelines/blob/main/reference/spec-standard.md); templates: `.specify/templates/`, vended by pipelines.

| Step | Skill | Writes | Checked by |
|---|---|---|---|
| Constitution | `spec-generator` | `.specify/memory/constitution.md` | spec-standard constitution check |
| Specify | `spec-intake-wizard`, `spec-generator` | `specs/<id>/spec.md`, `requirements.md` | spec-standard |
| Clarify | `spec-generator` | resolves each `[NEEDS CLARIFICATION: ...]` marker | spec-standard clarification check |
| Plan | `spec-generator` | `plan.md` (upstream sections, Build contract, Production caller chain, Cross-repo seams) | spec-standard plan check |
| Tasks | `task-planner` | `tasks.md`: `- [ ] T001 [P] [US1] <ROW-ID> ...` | spec-standard task-line check |
| Analyze | `spec-verifier` | `checklists/requirements.md`, gap fixes | spec-standard analysis and checklist |
| Implement | `feature-development-pipeline` | code and bound tests (`mark.spec("ID")` or `// spec: ID`) | `pipelines-hook spec-trailers` |
| Land | repository development skill | commit with `Spec: <IDs>` directly above `Co-Authored-By` | `spec-status` computes SPECIFIED, LANDED, VERIFIED, RETIRED |
| Release | repository development skill | pipelines `releases/*.yml` bundles (PIPE-RELEASE-R020 to R024) | `release-standard` |

Never write code for a row that is not SPECIFIED in this repository's `specs/`. If no row owns the behavior, extend the owning spec first. Never write `status.json` or a `Status:` line; the state is computed.

Arbora is the deployable composition layer for the Knuckles agent platform:
MCP/REST composition, MCP fleet supervision, control-plane policy, optional
Agent WebUI hosting, unary A2A, governed browser control, and deployment
operations under `arbora/`. Public operator documentation lives at
<https://knuckles-team.github.io/arbora/>.

Current capability limits are documented in `docs/status.md`. A capability
waiting on another repository's public contract must fail closed and remain
marked unavailable. Do not add a compatibility alias, static fallback, or
fabricated receipt to make it appear complete.

## Boundaries

- **Owns:** the one door. That covers admission, auth, approvals, served operations and the tool and operation registry. It also covers the MCP server and multiplexer, A2A serving and routing, config at `~/.config/arbora`, and the EG methods served as `eg.*`.
- **Never builds:** facts, stores, policy tables or decisions (route them to epistemic-graph). Protocol libraries and connector contracts go to agent-connector-sdk. The run loop goes to agent-utilities.
- **Allowed dependencies:** epistemic-graph and agent-connector-sdk. From meristem, only `host_ports`, `harness`, `runtime` and `core.paths`. Every package installs from PyPI.

Route every feature with the [ecosystem boundaries standard](https://github.com/Knuckles-Team/pipelines/blob/main/reference/ecosystem-boundaries.md) before any code. A part this repository does not own goes to its owner's specs.

## Start here

| Need | Read |
|---|---|
| Architecture boundaries, module map, build-host/environment setup, required quality gates, contract regeneration, identity setup, and branching & isolation for working *in this repository* | [`arbora/skills/arbora-repository-development/SKILL.md`](arbora/skills/arbora-repository-development/SKILL.md) |
| Choosing which Arbora ecosystem repository owns a spec, and the public cross-repository contribution flow | [`arbora/skills/arbora-development/SKILL.md`](arbora/skills/arbora-development/SKILL.md) |
| End-user / operator usage of a running Arbora | [`arbora/skills/using-arbora/SKILL.md`](arbora/skills/using-arbora/SKILL.md) |
| Standing up Arbora (bare metal, Compose, Swarm, Kubernetes) | [`arbora/skills/arbora-genesis/SKILL.md`](arbora/skills/arbora-genesis/SKILL.md) |

## Non-negotiables

- Module ownership and dependency boundaries (what belongs to `epistemic-graph`,
  `meristem`, `agent-connector-sdk`, `agent-webui` vs. Arbora itself):
  see [`arbora/skills/arbora-repository-development/SKILL.md`](arbora/skills/arbora-repository-development/SKILL.md).
- MCP and REST routes share the same application service and authorization
  decision.
- Security is fail closed: validated identity, tenant isolation, and the
  configured TLS/authorization posture are required before dispatch; unknown
  effects never claim rollback.
- Never commit credentials, bearer tokens, private endpoints, operator
  inventories, generated live configuration, or plaintext secrets.
- Never push to `main`. Work on a topic branch in a dedicated worktree (see
  the repository development skill's "Branching & isolation" section) and
  open a pull request; hosted CI (`release.yml`) is the merge gate.
- Fresh worktree: `scripts/bootstrap.sh --siblings-only && uv run pytest tests/<file>`.

---
*Navigation index — kept lean on purpose. Detailed procedure lives in the
linked skills above; regenerate this file's content there, not here.*

## Specs: extend first

Find the spec row that owns the behavior before any code change. Search with `git grep -n "<term>" -- specs`. Cite the row ID in the commit `Spec:` trailer. Extend the owning spec before any new spec text. Add a child row, a new rollup, or a dated `plan.md` "Amendments" entry. Create a new spec only for a capability that no spec owns. Audits, reviews, and carry-overs land in the owning spec. They never get a spec directory. Reuse an existing function, module, or store before adding one. The rules are the ecosystem [spec standard](https://github.com/Knuckles-Team/pipelines/blob/main/reference/spec-standard.md).

## Release

Bump the version before tagging. A tag on a commit with the old version rebuilds the old version, and PyPI refuses it.

1. Bump first on a topic branch: `uv run bump-my-version bump --new-version X.Y.Z --no-commit --no-tag`. It edits `arbora/_version.py` and `CHANGELOG.md`. `uv.lock` holds no arbora version and no fixture embeds one. Confirm with `git grep -n "<old version>"`.
2. Open a PR and rebase-merge the bump with `gh pr merge --rebase` after the `release.yml` gates pass.
3. On the merged `origin/main` commit, check that `__version__` and the bumpversion `current_version` both equal `X.Y.Z`. Check that PyPI lacks it: run `python3 -I <pipelines>/.github/actions/verify-release-version/verify_release_version.py --root . --ref refs/tags/vX.Y.Z` from Knuckles-Team/pipelines main. Or request `https://pypi.org/pypi/arbora/X.Y.Z/json` and expect HTTP 404.
4. Only then tag that commit and push the tag: `git tag vX.Y.Z <sha> && git push origin vX.Y.Z`.
5. Confirm PyPI lists both the wheel and the sdist. `uv build` makes both, and `release.yml` uploads both.
6. If the tag sits on the wrong commit and nothing published, delete it with `git push origin :refs/tags/vX.Y.Z` and re-tag. If anything published, cut the next patch version.

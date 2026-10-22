# Arbora — repository guide

<!-- agent-contract:start -->
## Agent contract

Tool-neutral rules for any AI agent or human working in this repository. This block is maintained by the fleet standard; edit outside the markers.

- **Owner boundary:** o Feature routing across repositories: [ecosystem boundaries](https://github.com/Knuckles-Team/pipelines/blob/main/reference/ecosystem-boundaries.md).
- **Setup:** run `scripts/bootstrap.sh`. It is idempotent and non-interactive: it pins uv and Python, syncs the locked environment, installs the git hooks and prints the gate command.
- **Gates:** `uvx pre-commit run --all-files` must pass before you push. Do not bypass hooks to get green.
- **Commits:** one logical change per commit with a `Spec: <ID>` trailer (or `Spec: none (<reason>)`), plus a `Co-Authored-By:` trailer naming the assisting agent. Stage explicit paths only; never `git add -A` or `git add .`. Use your own configured Git identity, never a tool or bot name.
- **Org rules:** [organization AGENTS.md](https://github.com/Knuckles-Team/.github/blob/main/AGENTS.md).
<!-- agent-contract:end -->

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
- **Allowed dependencies:** epistemic-graph and agent-connector-sdk. From agent-utilities, only `host_ports`, `harness`, `runtime` and `core.paths`. Every package installs from PyPI.

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
  `agent-utilities`, `agent-connector-sdk`, `agent-webui` vs. Arbora itself):
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

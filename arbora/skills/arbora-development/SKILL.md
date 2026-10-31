---
name: arbora-development
domain: development
skill_type: skill
description: >-
  Develop a feature in any Arbora ecosystem repository from public GitHub
  checkouts. Use when choosing the owner of any Arbora ecosystem spec,
  preparing an isolated source and test environment, or proving its deliverable.
license: MIT
tags: [arbora, development, public-contribution, specs]
metadata:
  version: '1.0.1'
---

# Arbora ecosystem development

Start at the public [organization hub](https://github.com/Knuckles-Team/.github):
its [build-first queue](https://knuckles-team.github.io/.github/build-first.html)
lists work still to build, the [spec status report](https://knuckles-team.github.io/.github/spec-status.html)
links owner-native contracts, and the [contribution guide](https://github.com/Knuckles-Team/.github/blob/main/CONTRIBUTING.md)
walks a new contributor or coding agent from checkout to pull request.

Use the **owning repository's tracked `specs/<id>/`** as the complete contribution
contract. Its `spec.md`, architecture/design `plan.md`, `test-spec.md`, and
`tasks.md` must contain enough context, interfaces, decisions, fixtures, and
acceptance criteria for a contributor with only public GitHub access. Include
local contracts or public links to a related repository's `specs/<id>/` when a
change crosses boundaries. Do not require a private ledger, unpublished draft,
operator inventory, internal URL, or live deployment to interpret a spec.

## Rapid delivery: the 5-minute contract

Specs are already designed. The work is implementation, delivered in very small, continuously landed slices.

**Extend first (anti-sprawl)**
- Before any code change, find the spec row that owns the behavior: "git grep -n "<term>" -- specs". Cite its ID in the commit `Spec:` trailer.
- Before any new spec text, extend the owning spec: a child row (`R012.4`), a new rollup at the end of that spec, or a dated entry in the `plan.md` section "Amendments". Create a new spec only for a capability that no spec owns, and record the search in "Why a new spec".
- Audits, reviews, gap analyses and carry-overs never get a spec directory. Their findings land in the spec that owns the row; a finding against another repository lands in that repository's spec.
- Before any new function, module, planner, selector or store, search for the existing one and reuse it. A second implementation of an owned capability is a defect, not a delivery.
- The rules are in "specs/README.md". The `spec-standard` gate enforces the names, file set, title and index.

**Time and size**
- One agent delivers one PR in 5 minutes or less. No agent or sub-agent runs longer than 5 minutes; an orchestrator dispatches the next slice to a fresh agent.
- Self-timer (hard): run "date +%s > /tmp/agent-start-$$" as your FIRST command, and before every later step check elapsed with "echo $(( $(date +%s) - $(cat /tmp/agent-start-*) ))". At 300 s, stop starting new work. At 480 s, commit what passes, push it, open the PR, report and STOP. Never exceed 600 s. Never wait on CI or background jobs; use "git commit --no-verify" and "git push --no-verify" after running pre-commit on the changed files yourself.
- CI turns around in 5 minutes or less. PR CI runs the fast subset: lint, type check, the spec check, and only the tests and crates the diff touches. Pushes to main run the full suite.
- CI timer (hard): every PR-path job carries "timeout-minutes: ${{ github.event_name == 'pull_request' && 10 || 60 }}", so a PR gate is killed at 10 minutes and a slow gate shows up as a failure instead of a silent queue.
- Never spawn sub-agents from a delivery agent.

**Investigation budget**
- At most 2 minutes and about 10 tool calls of reading before the first edit, and at most about 25 tool calls per PR.
- If the change is not clear by then, ship the `.1` slice (typed model plus refusal test) or skip the row with a one-line note. Do not write deferral essays.
- Trust the orchestrator's evidence and ID list; do not re-verify it.

**Sizing (deterministic)**
- Split a requirement when its size score is above 6, it names more than 2 code roots, or it is cross-repo. Children are `<ID>.<n>`, producer first.
- Net-new work is sliced, never skipped: `.1` typed model plus validation and refusal tests, `.2` the one entry point that uses it, `.3`+ each further behavior.
- Cross-repo moves split into one child per repo: the destination copies the behavior first, then the source switches importers and deletes.

**Specs update themselves (land VERIFIED, not just LANDED)**
- Every commit that delivers a requirement ends with the trailer "Spec: <ID>[, <ID>...]". A commit that touches code without delivering one says "Spec: none (ci|deps|refactor|fix-forward)".
- Every delivered ID gets at least one test bound to it in the same PR. Python: "@pytest.mark.spec("<ID>")". Rust/TypeScript: a `// spec: <ID>` line directly above the test.
- Spec status is computed at read time and never committed ("pipelines-hook spec-status --write" writes an ignored local copy). Never write placeholder SHAs. If the work is only part of a requirement, decompose it into `<ID>.<n>` children first and deliver whole children.
- Write the marker with the literal ID, never a variable. Put `Spec:` and `Co-Authored-By:` on consecutive lines in the last paragraph of the commit message, or `Spec:` is not read as a trailer.
- The generator applies the rules. Run "pipelines-hook spec-status" to see each row's state.

**Delivered means the row's acceptance is met**
- Never delete or stub out behaviour whose replacement is not wired. Never make a caller "fail closed" in place of repointing it. Never substitute a scope, weaken an assertion or add an allowlist entry to pass a gate.
- If the replacement lives in another repository, do not deliver: record the blocker in the row, name the missing capability, and stop. The orchestrator adds the row in the owning repository.
- Never re-add a path that another requirement retired; report the conflict.
- Valid outcomes are DELIVERED (with a bound test), SPLIT, BLOCKED and GAP (a "landed" row that is not really delivered). A half-delivery is not an outcome.

**Where progress shows**
- The repository's spec-delivery page computes each state at read time from rows, `Spec:` trailers and test bindings. SPECIFIED is written, LANDED is on main, VERIFIED is on main with a bound test.

**Before every push**
- Work in a real "git worktree add" from `origin/main`. Never edit a shared checkout, never "git stash", never "git add -A", never force-push.
- Run the repo's own pre-commit on the changed files: "uvx --from pre-commit==4.6.0 pre-commit run --files $(git diff --name-only origin/main...HEAD)". Fix every failure. No `noqa`, `type: ignore`, skip, xfail or whitelist entries to pass a gate.
- Run the targeted tests only, never a full suite. Run the type checker on every changed file, and keep each function at cyclomatic complexity 10 or less.

**Landing**
- Every PR lands within minutes of going green; no PR sits idle. Mechanical conflicts (generated files, Markdown) are resolved automatically by the merge-train tool. Real conflicts are additive in most cases and are resolved, not deferred.
- Many open PRs land together as a merge train: one integration branch and one CI run.
- Full CI on main catches what the fast subset missed; regressions are fixed forward immediately.
- Landing in this repo: `gh pr merge --auto --merge` when the PR opens. The required checks are the fast PR subset. If the PR goes DIRTY, merge `origin/main` into it and push.

**Report**: at most 8 lines: PR URL, requirement IDs, test result, and any `ID:<main sha>` proof for rows already on main.

## Step 0: route the feature

Route the feature before any code. The [ecosystem boundaries standard](https://github.com/Knuckles-Team/pipelines/blob/main/reference/ecosystem-boundaries.md) is the one ownership table.

1. Split the feature into parts with its routing procedure.
2. Find the owner of each part in its repository table.
3. Write each part as a row in its owner's `specs/`. Extend an existing row first.
4. Write code for a part only in the repository that owns it.

## Spec-driven flow

Run this before any code. Full table and gates: the Spec-driven development section of `AGENTS.md` and the [spec standard](https://github.com/Knuckles-Team/pipelines/blob/main/reference/spec-standard.md).

1. Read `.specify/memory/constitution.md`; create it from pipelines `specs/_template/constitution.md` if absent (`spec-generator`).
2. Specify the feature: `spec-intake-wizard` gathers the inputs, `spec-generator` writes `specs/<stable-id>/spec.md` and `requirements.md`.
3. Clarify: resolve every `[NEEDS CLARIFICATION: ...]` marker (`spec-generator`).
4. Plan: `plan.md` from pipelines `specs/_template/plan.md`, including Build contract, Production caller chain and Cross-repo seams (`spec-generator`).
5. Tasks: `tasks.md` lines `- [ ] T001 [P] [US1] <ROW-ID> ...` (`task-planner`).
6. Analyze: fix the gaps and fill `checklists/requirements.md` (`spec-verifier`).
7. Implement and land with the delivery contract in this skill (`feature-development-pipeline`); release through pipelines `releases/*.yml` bundles.

Never write code for a row that is not SPECIFIED. Feature directories use the stable spec ID, never a numbered prefix. The state is computed from rows, `Spec:` trailers and test bindings; never write `status.json` or a `Status:` line.

## Ownership and reuse

Other connector, frontend and service repositories may own additional
requirements. Use the owner named by the public spec and verify it against
actual code and interfaces. A related consumer spec does not transfer authority
from the component that implements the behavior.

Read each affected repository's `AGENTS.md`, `CONTRIBUTING.md`, own spec, public
interfaces, and tests. Extend the existing owner and execution path. A new
component needs a real caller and a test of that edge. Keep a capability in one
owner and use its exported contract in other repositories; avoid copied policy,
parallel registries, and compatibility scaffolding without a named consumer.
If the needed behavior has no owner spec, write the self-contained spec there
before implementation. A merged source change, test pass, and acceptance result
are distinct states; record evidence for each before calling a deliverable done.

## Public source environment

For a fresh checkout, use [the public bootstrap recipe](references/bootstrap.md).
It reproduces the source paths declared by the repositories and the public
Arbora release workflow; it does not assume a preexisting monorepo, private
package index, credentials, or running services. `repository-manager` is needed
only when working on its code or release-readiness logic. Work in a personal Git
branch/worktree and stage only reviewed paths. Never use a shared `git stash` or
an agent harness worktree action in a multi-worktree repository.

Install [universal-skills](https://github.com/Knuckles-Team/universal-skills)
and use `spec-generator`, `task-planner`, `sdd-implementer`, `spec-verifier`,
and `sdd-full-lifecycle` as needed. Read all six owner files: `spec.md`,
`plan.md`, `test-spec.md`, `tasks.md`, and `requirements.md` (status is computed, never committed)
(see `specs/README.md`'s own "Structure" section). A complete public
spec supplies its own design, tests, fixtures, and acceptance criteria; never
ask a contributor to retrieve private drafts or local operator inventory.

Check the target repository's own `AGENTS.md` for current commands and tool
versions. Run focused tests first, then its ordinary pre-commit/PR checks.
For Arbora, its release workflow is the executable source for the pinned
sibling revisions and source-verified test environment. Release-tag
dependency-readiness and live/credentialed checks have different prerequisites;
report them separately and do not claim they passed during an ordinary PR.
Do not weaken deterministic correctness, security, or code-quality failures.

The implementation and test evidence must cover the relevant spec requirements,
including failure behavior, interface compatibility, and existing wiring. Run
the owning repository's configured CCCC, `jscpd`, Dupehound, and KISS checks
where applicable; cite their actual commands and output, including any absent
tool or unsupported local stage. Do not invent a pass or a threshold.

## Release

Bump the version before tagging. A tag on a commit with the old version rebuilds the old version, and PyPI refuses it.

1. Bump first on a topic branch: `uv run bump-my-version bump --new-version X.Y.Z --no-commit --no-tag`. It edits `arbora/_version.py` and `CHANGELOG.md`. `uv.lock` holds no arbora version and no fixture embeds one. Confirm with `git grep -n "<old version>"`.
2. Open a PR and rebase-merge the bump with `gh pr merge --rebase` after the `release.yml` gates pass.
3. On the merged `origin/main` commit, check that `__version__` and the bumpversion `current_version` both equal `X.Y.Z`. Check that PyPI lacks it: run `python3 -I <pipelines>/.github/actions/verify-release-version/verify_release_version.py --root . --ref refs/tags/vX.Y.Z` from Knuckles-Team/pipelines main. Or request `https://pypi.org/pypi/arbora/X.Y.Z/json` and expect HTTP 404.
4. Only then tag that commit and push the tag: `git tag vX.Y.Z <sha> && git push origin vX.Y.Z`.
5. Confirm PyPI lists both the wheel and the sdist. `uv build` makes both, and `release.yml` uploads both.
6. If the tag sits on the wrong commit and nothing published, delete it with `git push origin :refs/tags/vX.Y.Z` and re-tag. If anything published, cut the next patch version.

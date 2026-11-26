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

## Delivery contract

Specs are already designed. The work is implementation.

**A row is delivered when**
- its behaviour is fully implemented, as the row says;
- it is wired to its production entry point (a production caller exists outside the new code and its tests);
- every test level its test-spec names passes;
- the tests are bound to the row;
- the gates verify it.

There is no time limit, no size limit per agent or per change, and no rule about how many agents deliver a row.

**Recommendations (optional)**
- Extend first: find the spec row that owns the behaviour (`git grep -n "<term>" -- specs`), cite its ID in the commit `Spec:` trailer, and reuse the existing function or module rather than adding a second implementation.
- Group rows that share an entry point into one bundle and deliver bundles in dependency order, producer first. Split a requirement into `<ID>.<n>` children when it names more than 2 code roots or is cross-repo; the destination repository copies a behaviour first, then the source switches importers and deletes.
- Net-new work can be sliced: `.1` typed model plus validation and refusal tests, `.2` the one entry point that uses it, `.3`+ each further behaviour.
- Every delivered ID gets at least one test bound to it in the same PR. Python: `@pytest.mark.spec("<ID>")`. Rust and TypeScript: a `// spec: <ID>` line directly above the test. Never write generated status files; the state is computed.
- Run the targeted tests for the change; leave the full suite to the main-branch gates.
- Report the PR URL, requirement IDs, test result, and any `ID:<main sha>` proof for rows already on main.

**Before every push**
- Work in a real `git worktree add` from `origin/main`. Never edit a shared checkout, never `git stash`, never `git add -A`, never force-push.
- Run the repo's own pre-commit on the changed files: `uvx --from pre-commit==4.6.0 pre-commit run --files $(git diff --name-only origin/main...HEAD)`. Fix every failure. No `noqa`, `type: ignore`, skip, xfail or whitelist entries to pass a gate.

**Landing**
- Open the pull request and merge it when every required check is green. Mechanical conflicts (generated files, Markdown) resolve by merging `origin/main`; real conflicts are additive in most cases and are resolved, not deferred.
- Full CI on main catches what the pull-request subset missed; regressions are fixed forward.

## Bulk delivery

High-rate delivery follows the [bulk delivery standard](https://github.com/Knuckles-Team/pipelines/blob/main/reference/bulk-delivery.md).

- Four separate stages, run by different workers: writing, batched validation, fixing red results only, and automatic merge on green.
- Writers implement one served-method slice of at most 8 rows, push the branch, and never build, test or wait on CI. They format only and run both hook stages.
- Validation workers build branches in batches and record green as a commit status on the exact head SHA.
- A writer never makes an open design choice. It records the question; the decision is written into the owning spec row first, then the row is implemented.
- Only rows implemented completely, with their bound test, go in the `Spec:` trailer. Remainders are re-queued.

## Step 0: route the feature

Route the feature before any code. The [ecosystem boundaries standard](https://github.com/Knuckles-Team/pipelines/blob/main/reference/ecosystem-boundaries.md) is the one ownership table.

1. Split the feature into parts with its routing procedure.
2. Find the owner of each part in its repository table.
3. Write each part as a row in its owner's `specs/`. Extend an existing row first.
4. Write code for a part only in the repository that owns it.

## Spec-driven flow

Run this before any code. Full table and gates: the Spec-driven development section of `AGENTS.md` and the [spec standard](https://github.com/Knuckles-Team/pipelines/blob/main/reference/spec-standard.md).

1. Read `.specify/memory/constitution.md`; create it from the [constitution template](.specify/templates/constitution-template.md) if absent (`speckit-constitution`).
2. Specify the feature: `speckit-specify` gathers the inputs and writes `specs/<stable-id>/spec.md` and `requirements.md`.
3. Clarify: resolve every `[NEEDS CLARIFICATION: ...]` marker (`speckit-clarify`).
4. Plan: `plan.md` from `.specify/templates/plan-template.md`, including Build contract, Production caller chain and Cross-repo seams (`speckit-plan`).
5. Tasks: `tasks.md` lines `- [ ] T001 [P] [US1] <ROW-ID> ...` (`speckit-tasks`).
6. Analyze: fix the gaps and fill `specs/<family>/checklists/requirements.md` (`speckit-analyze`).
7. Implement and land with the delivery contract in this skill (`speckit-implement`); release through pipelines `releases/*.yml` bundles.

Never write code for a row that is not SPECIFIED. Feature directories use the stable spec ID, never a numbered prefix. The state is computed from rows, `Spec:` trailers and test bindings; never write `specs/<family>/status.json` or a `Status:` line.

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
and use `speckit-specify`, `speckit-plan`, `speckit-tasks`, `sdd-implementer`, `speckit-analyze`,
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

1. Bump first on a topic branch: `uv run bump-my-version bump --new-version X.Y.Z --no-commit --no-tag`. It edits `arbora/version.py` and `CHANGELOG.md`. `uv.lock` holds no arbora version and no fixture embeds one. Confirm with `git grep -n "<old version>"`.
2. Open a PR and rebase-merge the bump with `gh pr merge --rebase` after the `release.yml` gates pass.
3. On the merged `origin/main` commit, check that `__version__` and the bumpversion `current_version` both equal `X.Y.Z`. Check that PyPI lacks it: run `python3 -I <pipelines>/.github/actions/verify-release-version/verify_release_version.py --root . --ref refs/tags/vX.Y.Z` from Knuckles-Team/pipelines main. Or request `https://pypi.org/pypi/arbora/X.Y.Z/json` and expect HTTP 404.
4. Only then tag that commit and push the tag: `git tag vX.Y.Z <sha> && git push origin vX.Y.Z`.
5. Confirm PyPI lists both the wheel and the sdist. `uv build` makes both, and `release.yml` uploads both.
6. If the tag sits on the wrong commit and nothing published, delete it with `git push origin :refs/tags/vX.Y.Z` and re-tag. If anything published, cut the next patch version.

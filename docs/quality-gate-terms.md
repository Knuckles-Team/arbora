# Terms of Acceptance — the wiring gates

This repository has no accepted exceptions to the wiring gates. An exception
must be a documented rule about a defensible class of code, never a file
allowlist, frozen count, or inline suppression.

## Orphan-module rule — enforced

The `check-orphan-modules` pre-commit hook (`scripts/check_wiring.py orphans`)
fails any module with neither production fan-in nor production fan-out that is
not a declared root. See the `arbora-repository-development` skill's
"Orphan-module wiring gate (Python)" section.

## Reachability report — informational, manual stage only

A module with nonzero fan-in/fan-out passes the orphan check above even when
no running process ever reaches it (it only imports, and is imported by,
other equally unserved modules). `scripts/check_wiring.py unreachable` (the
`check-unreachable-modules` pre-commit hook) reports every `arbora` module
that walking every import edge — static and dynamic/name-based — from the
declared roots (`pyproject.toml`'s `[project.scripts]` / `[project.entry-points]`
and the top-level package) never reaches. It runs at the `manual` stage
only, so it never blocks `pre-commit` or `pre-push` and CI's blocking gate
set is unchanged; it exists to make staged, not-yet-served code visible and
trackable, not to force an immediate fix. See the
`arbora-repository-development` skill's "Reachability report (Python) —
staged, not-yet-served code" section.

## Running the gates

```bash
pre-commit run check-orphan-modules --all-files                # Python wiring gate
pre-commit run check-unreachable-modules --hook-stage manual --all-files  # reachability report
```

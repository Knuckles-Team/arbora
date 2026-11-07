# Door skills

Seam between `ARBORA-HARNESS-PLUGINS` and universal-skills. Owner rows: `US-CONTENT-R007` in universal-skills and `ARBORA-HARNESS-PLUGINS-R001.2` in this repository.

Provider: universal-skills US-CONTENT-R007
Consumer: arbora ARBORA-HARNESS-PLUGINS-R001.2

## Wire shape

The content package holds `skills/canopy/SKILL.md` and `skills/session-handoff/SKILL.md`. Each frontmatter carries `name` equal to the folder name, at most 64 characters of lowercase letters, digits and hyphens. Each carries a one-line `description` of at most 1024 characters. Harness-specific keys appear only under `metadata`. The composer reads the folders through the SDK content convention and copies them unchanged. Every frontmatter passes the strictest agent contract as written, so no renderer transforms it.

## Refusal codes

- `SKILL_NOT_PORTABLE`: a frontmatter breaks the portable rules; the composer refuses the bundle.
- `SKILL_MISSING`: a declared skill folder is absent.

## Test vectors

- `tests/test_harness_portable_skills.py::test_every_skill_frontmatter_is_portable`.

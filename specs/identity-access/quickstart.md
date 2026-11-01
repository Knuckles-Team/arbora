# Quickstart — ARBORA-IDENTITY

The shortest path that shows identity and access working is the portable test suite, which provisions its own engine fixture and mock providers.

1. Install the development environment of the repository (`uv sync`).
2. Run the identity proofs: `uv run pytest tests/identity tests/access tests/security -q`.
3. Run the served access operations: `uv run pytest tests/api/test_access_ops.py -q`.
4. Run the anonymous-mode served proofs: `uv run pytest tests/security/test_anonymous_mode.py tests/a2a/test_anonymous_reads.py -q`.

Rows whose tests are listed under "Planned tests" in `test-spec.md` have no running proof yet. Run the scenario for a planned row once its test file exists, with the same `uv run pytest <path> -q` form.

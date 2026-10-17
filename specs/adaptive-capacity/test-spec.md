# Test contract

From a clean clone run `uv sync --extra test`, `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy arbora`, and the normal pre-commit hooks. Pure controller and fake-port tests are required on every PR; they need no live cluster or credential. A disposable served fault-injection job can provision its own engine and child service, and labels its result separately.

| Fixture | Expected result |
|---|---|
| Healthy windows after a breach | Bounded additive recovery, never above the fresh engine/policy ceiling. |
| Retryable service failures exceed budget | Deterministic multiplicative decrease; duplicate window ID leaves one receipt and one state transition. |
| Caller errors, policy refusals, too few samples | No child-health penalty; reason visible in status. |
| Observe mode | Decision receipt exists but fleet admission remains at configured base limit. |
| Enforce mode | Admission rejects/queues above the effective limit with typed retry guidance. |
| Two tenants or children | Independent windows, limits and visibility; no cross-tenant status leak. |
| Missing telemetry, stale engine premise, child disconnect | Explicit degraded state and conservative refusal/probe; no fabricated successful health. |
| Non-admin mode change, stale policy, unavailable audit reservation | No mode change or effect; same refusal on MCP and HTTP. |

Property tests should generate outcome sequences and assert `floor <= effective <= engine_headroom` and that automatic transitions never widen beyond an external ceiling. Run CCCC, KISS, jscpd and dupehound gates from the repository's pinned pipeline when source changes. Fix new findings without suppression or copied controller logic. Record exact command, commit, CI URL, fixture seed and redacted served receipt in `tasks.md`.

## Additional owner requirements

- `GRAPHOS-CAPACITY-R001.2`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `GRAPHOS-CAPACITY-R003`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `GRAPHOS-CAPACITY-R004`: Planned tests in `tests/api/test_agent_load_served.py` cover terminal latency, failures, and threshold boundaries. `test_agent_run_profile_measures_terminal_latency_and_failures` enters the served agent route. `test_agent_run_profile_rejects_missing_samples_and_exceeded_limits` rejects invalid or incomplete qualification. No execution is claimed.
- `GRAPHOS-CAPACITY-R001.3`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.

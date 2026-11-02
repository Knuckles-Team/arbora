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

- `ARBORA-CAPACITY-R001.2`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-CAPACITY-R003`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-CAPACITY-R004`: Planned tests in `tests/api/test_agent_load_served.py` cover terminal latency, failures, and threshold boundaries. `test_agent_run_profile_measures_terminal_latency_and_failures` enters the served agent route. `test_agent_run_profile_rejects_missing_samples_and_exceeded_limits` rejects invalid or incomplete qualification. No execution is claimed.
- `ARBORA-CAPACITY-R001.3`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-CAPACITY-R001.4`: Future tests in `tests/fleet/test_throttle_controller_served.py` cover breach, bounded recovery, idempotent replay and partition isolation as requirements.md defines. Tests have not run.
- `ARBORA-CAPACITY-R001.3.1`: Planned test `tests/fleet/test_capacity_proposals.py::test_builder_fills_fields_and_digest_is_stable` builds a proposal and asserts the fields, the stable digest and no proposal for an unchanged limit. Tests have not run.
- `ARBORA-CAPACITY-R001.3.2`: Planned test `tests/fleet/test_capacity_proposals_served.py::test_refused_and_unavailable_verdicts_hold_the_limit` replays a recorded engine reply for allow, each refusal code and failure. Tests have not run.
- `ARBORA-CAPACITY-R001.3.3`: Planned test `tests/fleet/test_capacity_proposals_served.py::test_apply_modes_and_epoch_conflict` asserts the narrow, the raise, the scaling intent and the epoch-conflict re-proposal. Tests have not run.
- `ARBORA-CAPACITY-R001.3.4`: Planned test `tests/api/test_capacity_ops.py::test_receipt_reflects_readback_and_mismatch` calls the operation over HTTP and MCP and asserts matching and mismatching receipts. Tests have not run.
- `ARBORA-CAPACITY-R001.5`: Planned test `tests/fleet/test_throttle_controller_served.py::test_enforce_refuses_over_limit_and_observe_admits` asserts both modes, the 429 status and the ceiling bound. Tests have not run.
- `ARBORA-CAPACITY-R751`: Planned test `tests/fleet/test_throttle_service.py::test_raise_requires_allow_verdict` runs against a loopback engine and asserts a raise applies only on allow. Tests have not run.
- `ARBORA-CAPACITY-R752`: Planned test `tests/fleet/test_model_capacity.py::test_limit_comes_from_engine_decision` uses a recorded engine reply and asserts the limit, breaker state and fallback of 1. Tests have not run.
- `ARBORA-CAPACITY-R753`: Planned test `tests/api/test_ingest_ops.py::test_index_passes_resource_grant_to_engine` asserts the grant reaches the engine request and is never exceeded. Tests have not run.

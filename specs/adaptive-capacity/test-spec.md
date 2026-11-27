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
- `ARBORA-CAPACITY-R001.4`: Planned tests in `tests/fleet/test_throttle_controller_served.py` cover breach, bounded recovery, idempotent replay and partition isolation as requirements.md defines. Tests have not run.
- `ARBORA-CAPACITY-R001.3.1`: Planned test `tests/fleet/test_capacity_proposals.py::test_builder_fills_fields_and_digest_is_stable` builds a proposal and asserts the fields, the stable digest and no proposal for an unchanged limit. Tests have not run.
- `ARBORA-CAPACITY-R001.3.2`: Planned test `tests/fleet/test_capacity_proposals_served.py::test_refused_and_unavailable_verdicts_hold_the_limit` replays a recorded engine reply for allow, each refusal code and failure. Tests have not run.
- `ARBORA-CAPACITY-R001.3.3`: Planned test `tests/fleet/test_capacity_proposals_served.py::test_apply_modes_and_epoch_conflict` asserts the narrow, the raise, the scaling intent and the epoch-conflict re-proposal. Tests have not run.
- `ARBORA-CAPACITY-R001.3.4`: Planned test `tests/api/test_capacity_ops.py::test_receipt_reflects_readback_and_mismatch` calls the operation over HTTP and MCP and asserts matching and mismatching receipts. Tests have not run.
- `ARBORA-CAPACITY-R001.5`: Planned test `tests/fleet/test_throttle_controller_served.py::test_enforce_refuses_over_limit_and_observe_admits` asserts both modes, the 429 status and the ceiling bound. Tests have not run.
- `ARBORA-CAPACITY-R751`: Planned test `tests/fleet/test_throttle_service.py::test_raise_requires_allow_verdict` runs against a loopback engine and asserts a raise applies only on allow. Tests have not run.
- `ARBORA-CAPACITY-R752`: Planned test `tests/fleet/test_model_capacity.py::test_limit_comes_from_engine_decision` uses a recorded engine reply and asserts the limit, breaker state and fallback of 1. Tests have not run.
- `ARBORA-CAPACITY-R753`: Planned test `tests/api/test_ingest_ops.py::test_index_passes_resource_grant_to_engine` asserts the grant reaches the engine request and is never exceeded. Tests have not run.
- `ARBORA-CAPACITY-R754`: Planned test `tests/fleet/test_lease_runner.py::test_runner_holds_heartbeats_kills_on_revoke_and_releases` asserts the rollup closes when `.1` to `.6` are verified; the root test is the hot-path proof of `.6`. Tests have not run.
- `ARBORA-CAPACITY-R754.1`: Planned test `tests/api/test_capacity_lease_ops.py::test_six_lease_operations_are_registered_and_pass_engine_refusals_through` asserts the registry lists the six ids with their scopes and surfaces; a recorded engine `TRANSITION_REFUSED` reaches the caller unchanged. Tests have not run.
- `ARBORA-CAPACITY-R754.2`: Planned test `tests/api/test_capacity_lease_ops.py::test_holder_digest_comes_from_the_verified_caller` asserts a request carrying a foreign `actor_digest` field is refused as an unknown field; renewing another caller's lease returns the engine refusal; a caller without `capacity:lease` is refused before the engine is called. Tests have not run.
- `ARBORA-CAPACITY-R754.3`: Planned test `tests/fleet/test_lease_runner.py::test_every_legal_run_edge_applies_and_every_other_pair_is_refused` asserts each of A01 to A14 applies and moves to the named state; the 49 other pairs of 7 states and 9 events (63 pairs, 14 legal, 49 refused) raise `RunTransitionRefused`. Tests have not run.
- `ARBORA-CAPACITY-R754.4`: Planned test `tests/fleet/test_lease_runner.py::test_a_revoked_lease_kills_the_child_group_and_exits_75` asserts a loopback engine that answers the third renew with a revoked lease makes the runner terminate a `sleep` child and its grandchild and exit 75 within the grace. Tests have not run.
- `ARBORA-CAPACITY-R754.5`: Planned test `tests/fleet/test_lease_runner.py::test_release_runs_on_exit_on_signal_and_when_a_grandchild_outlives_the_child` asserts three runs: normal exit, SIGTERM to the runner, and a child that forks a background `sleep` then exits; each ends with exactly one release call, and the third releases while the grandchild is still alive. Tests have not run.
- `ARBORA-CAPACITY-R754.6`: Planned test `tests/fleet/test_lease_runner.py::test_runner_holds_heartbeats_kills_on_revoke_and_releases` asserts the child's environment and descriptor list hold no lease id, fence token, credential or socket; two commands share one slot in order. Tests have not run.

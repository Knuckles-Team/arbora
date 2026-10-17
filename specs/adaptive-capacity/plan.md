# Architecture and implementation plan

## Existing wiring and ownership

Arbora already has fleet dispatch and admission in `arbora.fleet` and hosted control-plane modules under `arbora.control_plane`. Extend the dispatch admission point and the single hosted operation registry. The durable graph-engine `CapacityCell` and `CapacityLease` are external public contracts. Arbora owns only runtime throttling state, policy, measurements and receipts. Do not clone the engine capacity model or add a second fleet call path.

Create cohesive `arbora.fleet.error_budget`, `throttle_controller` and `throttle_limiter` modules only if the current fleet package lacks an equivalent owner. Define typed `OutcomeSample`, `BudgetWindow`, `ThrottleDecision` and `ThrottleStatus` DTOs. The controller is pure over a window and prior state. The limiter is the sole mutable admission point. Bind those through a `throttle_service` adapter to `capacity.throttle.status` and `capacity.throttle.set_mode` operations. The exact engine method name and schema must be pinned to its published contract before an implementation advertises the feature. Until then discovery reports unavailable.

## Decision rule

At window close, classify eligible failures, calculate error fraction and compare with configured budget. Decrease on breach (`max(floor, floor(current * beta))`, `0 < beta < 1`) and increase on a healthy window (`min(engine_headroom, current + alpha)`, `alpha > 0`). `alpha`, `beta`, window size and minimum sample count are explicit versioned configuration, not hidden constants. The effective limit is `min(controller_limit, engine_headroom, policy_limit)`. A policy or engine ceiling reduction takes effect immediately; a rise proceeds through AIMD. Never compare observations across tenant, child, class or policy revision boundaries.

Decision receipts include budget/configuration digest, premise revision, prior state, sample count, error class counts, new limit, mode and correlation ID. They omit raw request content and secrets. Mode change and actual enforcement share the hosted operation audit path. Keep a bounded in-memory cache, reconstructing from durable last decision if a public engine receipt contract exists; after restart with no reliable prior state, start at a conservative floor and report the reason.

## Build sequence

1. Pin the published engine capacity/status/lease contract and existing fleet admission call sites.
2. Implement the pure controller with deterministic state transitions and typed error classification.
3. Insert one limiter in the fleet dispatch path; wire observe-only first and prove no behavior change.
4. Add authorized status/mode operations, audit receipts and policy revision invalidation.
5. Enable enforce mode per child behind explicit configuration, run served fault injection and record acceptance evidence.

## Additional owner requirements

- `GRAPHOS-CAPACITY-R001.2`: Plan the owner work and dependencies defined in requirements.md.
- `GRAPHOS-CAPACITY-R003`: Plan the owner work and dependencies defined in requirements.md.
- `GRAPHOS-CAPACITY-R004`: Plan the owner work and dependencies defined in requirements.md.
- `GRAPHOS-CAPACITY-R001.3`: Plan the owner work and dependencies defined in requirements.md.

## Served agent load profile (GRAPHOS-CAPACITY-R004)

The proposed `agent-run-fixture-v1` profile measures one production agent route with a deterministic, authorized local fixture workload.
The workload binds a message digest, agent/harness configuration digest, tool response digest, and EG dataset revision.
Use the actual AU run loop and one local read-only tool call per request.
A fake Arbora handler or isolated scorer cannot provide this evidence.
The AU load-driver/report contract has no assigned owner ID; publication of that contract remains a prerequisite.

Enter `arbora/api/http/routes.py:make_endpoint` at line 96 with an authenticated request.
Call `arbora/api/invoke/pipeline.py:invoke` at line 810 through the normal admission boundary.
Dispatch `agents.run` through `arbora/api/ops/agents.py:handle_agent` at line 43 into the composed A2A authority.
Observe terminal completion through `agents.tasks.get` using the same tenant and returned task identity.
This path already exists; the load driver and qualification report are proposed additions.

Use 100 warm-up requests, then 1,000 measured requests at each concurrency: 1, 8, and 32.
Each level uses closed-loop workers with one outstanding request per worker.
Use fresh idempotency keys. Record starts and terminal observations with a monotonic clock.
Measure latency from client submission through the complete terminal response, including transport and polling.
Use nearest-rank p95 over all measured requests; assign the 30,000 ms timeout to incomplete requests.
A failed admission, transport, tool, or terminal outcome counts once in the failure numerator.
The denominator is all 1,000 scheduled measured requests; missing samples fail qualification.
Each level must have p95 at most 2,000 ms and failure rate at most 1 percent.
These are initial acceptance limits for this fixture profile, not claims about arbitrary model workloads.

The report binds both repository revisions, workload digest, concurrency, counts, timing boundaries, and all raw sample digests.
It records each level's p95, failure fraction, and overall pass/fail.
No producer binding, unsupported profile, zero denominator, incomplete report, or changed workload permits a passing result.
Do not grant capacity or alter policy from an invalid report.

Planned proof: `tests/api/test_agent_load_served.py::test_agent_run_profile_measures_terminal_latency_and_failures`.
Boundary proof: `tests/api/test_agent_load_served.py::test_agent_run_profile_rejects_missing_samples_and_exceeded_limits`.
Test values below, equal to, and above both limits. Exercise missing bindings and zero samples.
These tests have not run. This definition provides no runtime qualification receipt.

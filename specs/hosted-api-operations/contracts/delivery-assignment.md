# Delivery assignment

Seam between `ARBORA-OPS`, Medulla and Meristem. Owner rows: `MEDULLA-SDLC-R019.3` and `MEDULLA-SDLC-R019.13` in medulla, `MERISTEM-SCM-R012.1` in meristem and `ARBORA-OPS-R896.12` in this repository.

Provider: arbora ARBORA-OPS-R896.12
Consumer: meristem MERISTEM-SCM-R012.1

## Wire shape

Arbora reserves a development lane, then starts one meristem run with `DeliveryAssignmentContext{delivery_id, scope_rows, proposal_ref, inputs, recovery_note, round_verdicts, background_limit_s}`. New inputs reach the run at the next step boundary through the host input port. The run reports `{state, summary, proposal_ref?, question?}` back through `DeliveryReport`. A lost lane lease ends the assignment, and the engine opens the next one with a recovery note.

## Refusal codes

- `LANE_QUOTA`: no slot is free; the start queues and refuses nothing to the caller.
- `ASSIGNMENT_ENDED`: a report named an ended assignment.

## Authoritative types

- `arbora/fleet/scaling/development_lane.py`: `EngineNativeDevelopmentLaneTransport`.
- meristem `meristem/harness/delivery_run.py`: `DeliveryAssignmentContext`.

## Test vectors

- `tests/development/test_assignments_served.py::test_assignments_reserve_lanes_and_keep_a_person_slot`.

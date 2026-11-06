# Data model

- `ChannelSupervisionState`: closed enum `STOPPED`, `STARTING`, `RUNNING`, `BACKING_OFF`, `DEGRADED`, `STOPPING` in `arbora/messaging/supervision.py`.
- `_TRANSITIONS`: table mapping each state to its legal next states; `transition(current, next)` returns the next state or raises `IllegalSupervisionTransition`.
- `IllegalSupervisionTransition`: typed error carrying the refused pair.
- `IntakeLeaseState` (`Held`, `Suspect`, `Lost`, `Released`), `IntakeLeaseEvent` and `IllegalLeaseTransition` (planned, `arbora/messaging/lease.py`): the intake lease machine with one `transition` function.

# GRAPHOS-MESSAGING — Messaging channel supervision lifecycle

## Contract delivery

Delivery and acceptance evidence are recorded in generated status.json.

## User stories and scope

As the Arbora process, I supervise every registered messaging channel process (telegram, mattermost, and any further `agent_utilities.messaging` entry-point backend) through one typed lifecycle: start, health-gated activation, bounded-backoff restart on a recoverable failure, graceful stop with drain, and an explicit degraded state when a configured channel's adapter is missing or fails to instantiate. As an operator, I can read a channel's current supervision state and know it reflects reality rather than a log line I have to grep for.

This specification expands the broader Arbora messaging-host ownership in [GRAPHOS-HOST-R011](../host-composition-boundary/requirements.md) with lifecycle and health-reporting contracts. The host-composition requirement remains authoritative for Arbora's broader messaging and routing responsibilities.

## Ownership boundary

Arbora supervises channel processes through InboundRouter, MessagingRegistry, and MessagingService. These modules own start, stop, health, backoff, and degraded detection. They never persist a second durable message store.

Agent-utilities owns messaging adapters and inbound message models, including MessagingBackend, EventType, InboundEvent, and MessagingConfig. This spec links the [AU owner contract](https://github.com/Knuckles-Team/agent-utilities/tree/main/specs/au-integration-reliability) and rows AU-INTEGRATION-R002 and AU-INTEGRATION-R019.

## Requirements

- **MSG-01 Typed supervision state machine.** A closed, typed state enum (`STOPPED`, `STARTING`, `RUNNING`, `BACKING_OFF`, `DEGRADED`, `STOPPING`) models one channel's supervised lifecycle, with a fixed table of legal transitions. A transition not in that table is refused with a typed error rather than silently applied or ignored. Delivered as the rollup of `GRAPHOS-MESSAGING-R001.1` (typed model, transition table, refusal tests) plus the router wiring that reports this state per backend.
- **MSG-02 Start is health-gated.** `InboundRouter.start()` moves a registered backend to `RUNNING` only after it observes `backend.is_connected`; an unconnected backend is never promoted to `RUNNING` and is recorded in a state the caller can distinguish from a transient startup race.
- **MSG-03 Restart backoff is bounded and resets on health.** On a recoverable listener failure, supervision moves the backend to `BACKING_OFF` and retries after an exponential delay from `MESSAGING_LISTEN_BACKOFF_BASE_S` (floor 1s) capped at `MESSAGING_LISTEN_BACKOFF_MAX_S` (default 60s); the delay never exceeds the cap and never busy-loops at zero. A run that stays healthy for at least `MESSAGING_LISTEN_HEALTHY_RESET_S` (default 60s) resets the delay to base before the next failure.
- **MSG-04 Stop drains, it does not abandon.** `InboundRouter.stop()` moves every supervised backend to `STOPPING` before cancellation, cancels listener and inbox-reaper tasks, awaits their completion (`return_exceptions=True`), and only then reports `STOPPED`. A cancellation observed during a backoff wait is treated as clean shutdown and never triggers a further restart.
- **MSG-05 Typed degraded adapter state and configuration-aware health.** Arbora records a configured channel whose adapter cannot be created as DEGRADED with a typed reason, while continuing other configured channels. The full behavior is the rollup of GRAPHOS-MESSAGING-R005.1.
- **MSG-05.1 Typed adapter degradation and configuration-aware health.** Preserve the configured missing/import-failed adapter record as DEGRADED. Record AdapterMissing or AdapterImportFailed. Continue creating other configured channels. Expose components.messaging_intake through authenticated GET /api/dashboard/health and graph_configure(action="health"). Return stable enabled, configured, state, alert_eligible, and backends fields. Set enabled only when MESSAGING_INTAKE_ENABLED is explicitly true and KG_DAEMON_ROLE=host. Credentials alone never enable intake. Set configured when at least one supported backend has credential material. Never return credential values. Set component state to disabled when intake is off. Use not_configured when intake is enabled without a backend. Use degraded when an enabled configured backend is unavailable. Use healthy only when every configured backend connects. Set alert_eligible true exactly when enabled and state is not_configured or degraded. Build backend rows from configured IDs and typed degraded-record IDs. Sort rows by ID. Each row contains id, configured, state, and nullable reason_code. Use not_configured when credentials are absent. Use disabled when intake is off. Use connected only when enabled and is_connected=true. Otherwise mark configured backends unavailable. Map AdapterMissing to adapter_missing. Map AdapterImportFailed to adapter_import_failed. Map false is_connected to backend_not_connected. Omit credentials and raw exception text. Alert evaluators may emit only when alert_eligible=true. Health reads never send messages. Keep unauthenticated /health and /health/ready detail-free.
- **MSG-06 Boundary: Arbora supervises, agent-utilities owns adapters.** Arbora's supervision types depend only on `agent_utilities.messaging.base.MessagingBackend`'s public adapter contract and `agent_utilities.messaging.models`. Arbora never implements a channel adapter, and agent-utilities never implements process supervision, health gating, or backoff. `AU-INTEGRATION-R002`'s `messaging_intake_enabled` threading and `AU-INTEGRATION-R019`'s direct-reply budget are AU-owned inputs this state machine observes but does not re-implement.

## Requirement map

| IDs | Arbora obligation |
|---|---|
| GRAPHOS-MESSAGING-R001, GRAPHOS-MESSAGING-R001.1 | Typed supervision state enum, transition table, and illegal-transition refusal |
| GRAPHOS-MESSAGING-R002 | Health-gated start before RUNNING |
| GRAPHOS-MESSAGING-R003 | Bounded exponential restart backoff with healthy-run reset |
| GRAPHOS-MESSAGING-R004 | Graceful stop with task drain and no restart after cancellation |
| GRAPHOS-MESSAGING-R005, GRAPHOS-MESSAGING-R005.1 | Typed adapter degradation, configuration-aware health projection, and enabled-only alert eligibility |
| GRAPHOS-MESSAGING-R006 | Supervision/adapter ownership boundary against AU-INTEGRATION-001 |

GRAPHOS-HOST-R011 remains the broader host-ownership contract; this specification provides messaging-supervision details without replacing its other obligations.

## Acceptance

1. The typed state enum and transition table exist with unit tests proving every legal transition succeeds and every illegal transition is refused with a typed error, before any router wiring is claimed complete.
2. A backend that never connects is observably not `RUNNING`. A failing listener's restart delay is bounded by the configured cap and resets after a sustained healthy run.
3. `stop()` reaches `STOPPED` only after its tasks are awaited; no task restarts after a clean cancellation.
4. A missing adapter produces a typed `DEGRADED` record, not a silent omission, and does not stop supervision of other configured channels.
5. Quality gates in [test-spec.md](test-spec.md) pass at the exact default-branch revision with no duplicate state authority.

6. Component-health JSON distinguishes disabled, not-configured, healthy, and degraded; reports per-backend safe reason codes; and computes alert_eligible from explicit operator enablement.
7. Authenticated dashboard health and graph_configure(action="health") return the same Arbora messaging projection. Unauthenticated liveness/readiness probes expose no detailed component data.
8. Component alert evaluation uses alert_eligible as its gate.
Credentials never make disabled intake alert-eligible.
An enabled but unconfigured or unavailable intake is eligible.
Requirement IDs are defined in [requirements.md](requirements.md); delivery state per ID is in `status.json`.

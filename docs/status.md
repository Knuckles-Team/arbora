# Capability status

This page describes the surface of Arbora `0.1.0` as it is shipped. Arbora
reports missing authority as a typed unavailable result. It does not substitute
a static catalog, process-local durable store, or fabricated success receipt.

## Runtime surfaces

| Surface | Availability | Current authority |
|---|---|---|
| MCP composition | Available | One FastMCP lifecycle with `stdio` and authenticated `streamable-http` transports |
| REST gateway | Available | The same application services used by MCP, projected through Arbora routes |
| Fleet gateway | Available with a checked catalog | Child lifecycle, health, OAuth admission, collision-safe naming, and per-session discovery |
| Overstory host | Available through the `webui` extra | Co-service supervision with an injected Arbora application composer |
| Unary A2A | Available | Authenticated Agent Card plus send, get, list, and cancel over durable WorkItems |
| Browser control | Available when attended identity is configured | Catalog, lease, policy, dispatch, cancellation, and provenance orchestration |
| Deployment operations | Available | Configuration, diagnostics, release canary, managed environments, backup, and restore validation |

MCP and REST handlers meet at the same application boundary. Connector widgets
delegate through admitted fleet tools. The WebUI co-service submits work to the
same multiplexer owned by the serving loop.

## Composition requirements

| Capability | Required runtime state | Behavior when absent |
|---|---|---|
| EG-backed fleet catalog | Generated server-registration and AgentComponent clients. Matching meristem read ports | Startup refuses readiness; no static catalog is selected |
| Connector runner | Generated ConnectorPack and SourceIngest clients, SDK runner, and live policy resolver | Connector execution is refused |
| Semantic content packs | Provisioned `arbora` and `meristem` packs attached through GraphSchema | Content checks report the missing or stale attachment |
| WebUI application routes | Overstory installed with its application-composer seam | Arbora runs without the optional WebUI co-service |
| Attended browser control | WebUI identity validation, backchannel revalidation, and engine mutation authorities | Browser control remains unavailable |

## Explicitly unavailable surfaces

| Surface | Current result |
|---|---|
| Durable four-family MCP resource/template reconciliation | `reingestion-unreconciled`; Arbora does not claim publication |
| Context-budget-selected A2A tool subsets | Refused before admission because the signed request does not bind an enforceable subset |
| A2A streaming, push notifications, and transition history | Not advertised by the Agent Card |
| Agent Client Protocol (ACP) | Experimental source stdio and authenticated HTTP/SSE. Opt-in central first-turn answer; unconfigured 503. Central continuation and installed pairing pending. |

Ordinary authenticated unary A2A routing to an authorized agent remains
available.

## Release evidence

The repository builds a Python wheel and publishes from a version tag after the
release workflow succeeds. A green `main` build updates source and documentation
without publishing a package.

Before changing live traffic, validate the resolved configuration, run the
deployment doctor, and run the release canary described in the
[deployment guide](deployment.md).


An active unary A2A send response includes measured AU run counts, when
available, under `metadata.arbora.usage`: `runId`, `taskId`, `contextId`,
`scope: "turn"`, and `counts`. Counts preserve the producer's nonnegative
integer request, tool-call, input-token, output-token and total-token values.
Missing usage stays absent. Completed replays and task reads do not reconstruct
usage; durable per-session usage and cost accounting remain unavailable.

Task dependency graph execution has an opt-in shared host composition path.
It requires a trusted HostedInvocation with TaskPlanConfig, native A2A run
ownership, and EG's executable-plan publication/readback contract. The selected
model proposes tasks without tools or authority fields. The host accounts for planner requests, tokens and time. It evaluates through EG and publishes under the native lease fence. It reads back canonical content, then invokes AU's existing DependencyPlan runtime. Defaults remain single-step. Missing contracts report
`UPGRADE_REQUIRED` rather than silently downgrading.

Controlled transport/model-adapter tests cover this chain; real native-engine
restart/fencing acceptance remains unqualified. Operator startup configuration
is covered by focused source tests. Planner usage is included in the Arbora execution result, but AU
terminal RunEvents currently report execution-only usage.


Operators opt in through `ARBORA_TASK_PLAN` in the existing
`~/.config/arbora/config.json` settings document. The value is JSON text, not a
client request field. Unset disables graph planning. Supply `feature_schema`
with the actual published EG `component_id`, `definition_digest` and
`kind: "feature_schema"`; `policy` is EG's `{"policy":"default"}` or a pinned
decision-policy reference. Required positive bounds are `max_concurrency`
(1–8) and `max_steps` (1–64). Optional `request_limit` (50),
`total_tokens_limit` (100000) and `timeout_seconds` (600) bound the entire run.
Unknown fields, malformed JSON, invalid pins and invalid limits refuse startup
before process authority is minted. Graph endpoints and compatible EG/AU
contracts remain required; this setting grants no caller authority.

Public current-plan responses expose scaffold items only, never executable
prompts. A retry that encounters an already attached plan currently refuses with `TASK_PLAN_RETRY_REQUIRES_ORIGINAL_PUBLICATION`. The latest mutable scaffold is not evidence of the original immutable publication payload.
Native publication receipt recovery remains an acceptance gate.


Completed native admission recovery has passed a local real-engine test with simulated model responses. Identical retries preserve the completed task and reply without additional model requests. This holds before and after restarting both services against the same persistent store. Changed input returns the typed
idempotency conflict, and public event identities survive restart. This does
not qualify concurrent execution deduplication or active-execution resume.

### Flat execution graph reads

`runs.execution_graph` is an explicitly versioned, read-only projection of an
owned run's current native dependency plan. Clients request
`arbora-execution-graph-v1`; operation discovery and the generated schema identify
the capability. Existing replay v1 and default run reads are unchanged.

The graph includes declared step IDs and predecessor-to-dependent edges, never
prompts or outputs. A complete declared topology does not mean a complete
lifecycle. Status reflects only a bounded prefix of ordered task events from the
same run and plan. Unobserved means no lifecycle evidence in that prefix, not
that a task has not started. The snapshot reports its covered cursor and whether
more observed events exist; lifecycle coverage remains incomplete even after a
terminal root event. Invalid history is unavailable, not filtered into a graph.
Expired or missing current-plan publication also makes the graph unavailable.

These are flat plan steps, not spawned child conversations. Transcript, active
messaging and completed-child continuation remain unavailable. Source-level
HTTP/MCP tests cover this projection; installed native-service and recursive
execution acceptance remain pending.

Clients must treat task IDs as untrusted display text. Escape browser markup
and terminal control characters without changing the opaque identity used for requests.

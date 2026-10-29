# Test and release contract

## Clean-checkout path

On a supported Python version, clone this repository, run `uv sync --extra test`, then `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, and `uv run mypy arbora`. Run `uvx --from pre-commit==4.6.0 pre-commit run --all-files` for repository hooks. Contract tests use in-process fake public ports with deterministic data. They do not require a cluster, sibling checkout, credentials, or local inventory. Optional served tests may provision disposable dependencies in CI and must label that tier separately.

## Positive cases

| Test | Proof |
|---|---|
| Build a host with only installed public dependency packages | Core MCP and REST route tables load; optional capability availability is truthful. |
| Invoke identical read through MCP and REST | Same authorization, tenant filter, response semantic ID and source receipt. |
| Invoke a governed write through two entrypoints with same idempotency key | Exactly one downstream effect and one durable receipt. Duplicate call returns the same outcome. |
| Open UI co-service, reload catalog and close it | One FastMCP loop/multiplexer, no blocked loop or lost generation notification. |
| Package wheel and import it in a fresh environment | Console scripts resolve and declared extras control optional imports. |
| ARBORA-HOST-R019: mount the dashboard routes. Open `/ws/dashboard` and send a `subscribe` message | REST reads answer 200. The stream sends a snapshot, then an update scoped to the subscribed widgets. |
| ARBORA-HOST-R020: call `install_decide_consumers` with a fake client. Then use a failing client, a failing assembler, and a client without SPARQL. | All four consumers install. Each fault skips only its own consumer and raises nothing. |
| ARBORA-HOST-R021: assemble a request through a fake engine client with the bound `commit_context`/`publish_context` providers | The fake client's `commit_decision` and `publish_graph` each record one call for a solved assembly; an abstained assembly records neither. |
| ARBORA-HOST-R022: install the decide consumers, then plan a task with a fake assembler and a fake capability source | The planner's `capability_search`/`guardrail_source`/`workflows` are bound and callable; the planned agent's skills/tools are non-empty and its reuse metadata reflects the fake capability source's hit. |

## Negative cases

| Test | Required refusal |
|---|---|
| Missing/expired principal, wrong tenant or scope, stale policy version | Deny before downstream effect; same error class on every surface. |
| Dependency public contract absent or wrong generation | Typed unavailable/mismatch result; no private-import fallback. |
| Duplicate server construction or co-service teardown race | No second authority; bounded shutdown and explicit failure. |
| Plant a forbidden `agent_utilities.knowledge_graph` or other private import | Package-layout check fails, including lazy import. |
| A route reimplements a stored task or graph operation | Architecture test detects a second persistence client/schema or unmatched action registration. |

## Quality and evidence

When code changes, run the repository's current CCCC, jscpd differential/census, dupehound changed/census, KISS and language-native gates. Use `.pre-commit-config.yaml` and the hosted scanner job. A new duplicate or complexity violation is fixed by reusing the owning module, not by a suppression or baseline reset. Evidence names the exact commit, command, package versions, CI run, artifact hash and representative served receipts. Compare the final remote head to that commit. If an optional/live tier is unavailable, keep acceptance pending and report the missing tier precisely.

## Served startup configuration acceptance

Run the installed launcher in an isolated workspace with controlled public dependency services.
Use seed 4109 to permute declaration ordering across 32 cases; normalized digests and refusal sets must match.
Probe a valid listener, then exercise missing, malformed, conflicting and undeclared names with zero process-start effects.
Check port boundaries 0, 1, 65535 and 65536 without printing resolved values.
Repeat after restart and compare safe evidence with the original configuration-schema acceptance.
These proposed scenarios have not been run; current helper tests do not establish their served results.

## Served installer and start acceptance

Proposed tests enter installed commands with disposable homes and controlled real services; direct helper calls are insufficient.
Use seed 4111 for 32 detection-order permutations across present, absent, malformed and version-mismatched harness installations.
Require identical normalized changes. Check unrelated file digests, absent-directory nonexistence and conditional-write conflict refusal.
Parse tool entries and instructions, invoke a controlled supported hook, then call the configured MCP server through a client.
Interrupt a multi-file application and check actual partial outcomes and safe retry against current digests.

Start terminal, web and both with an authorized workspace and controlled public client adapters.
Probe HTTP during foreground terminal interaction; observe a distinct background thread and exactly one multiplexer and graph authority.
Check missing client, denied workspace, occupied port and unavailable authority before accepting traffic.
Generate 2,000 numbered records with seeded concurrent writers; check sequence continuity or explicitly recorded gaps after rotation.
Use a 4 KiB test rotation bound and two retained files; memory and retained bytes remain within declared limits.
Inject credentials into a controlled message and require redaction in every stored and projected record.
Create two authenticated tenants in the same workspace and interleave distinct secret-free canary records into the host sink.
Exercise both permitted delivery modes: tenant-exclusive projection descriptor and authenticated filtered stream/reference.
Each tenant receives its own canaries and zero bytes, metadata or gap information identifying the other tenant's activity.
Refuse a descriptor to the shared sink even when records contain tenant tags.
Swap tenant references, forge workspace claims and replay another tenant's cursor; require refusal before returning any bytes.
Repeat across rotation, reconnect, expiry and revocation. Never reuse an old tenant binding for a new session.
If shared filesystem ownership cannot isolate a projection, descriptor mode refuses while the authenticated filtered path remains required.
Retain the different-workspace denial case. No diagnostic bytes appear on MCP stdout.
Restart the host, observe a new instance identity, and check bounded shutdown without stopping reused services.

For both MCP transports, initialize a real client, compare all eight tools and invoke an admitted and denied operation.
Keep original R023 fallback, ranking and fleet-call tests in addition to this transport check.
Unified terminal rendering requires the real terminal-client consumer; controlled adapters prove only the Arbora handoff.
The proposed served scenarios have not been run and establish no runtime delivery by themselves.

## Owner-bound capability acceptance (ARBORA-HOST-R029–R031)

- `tests/api/test_agent_browser_ops.py::test_native_desktop_actions_require_verified_grant_without_focus_capture`: exercise composed `pipeline.invoke` and `BoundOperationRuntime.dispatch`. Assert grant/tenant admission. Assert the returned outcome retains the request id. Assert no cursor capture or focus change. Missing, revoked, or cross-tenant grants must cause zero provider calls.
- `tests/api/test_agent_browser_ops.py::test_visual_state_is_run_source_and_tenant_bound`: exercise composed dispatch across multiple steps. Assert the same run carries source/frame/revision identity. Assert expiry or source change invalidates the state. Stale or cross-tenant state must be refused before dispatch.
- `tests/mcp_server/test_composition.py::test_coding_agent_flow_uses_owner_ports_and_sandbox`: exercise selected executable discovery, AU delivery of the EG-selected transformation, sandboxed WebUI rendering, and refusal if an owner port is missing. These tests are proposed; none has run.

## Planned authenticated terminal log acceptance

These cases extend ARBORA-HOST-R009.5, R009.6 and R010.7 with TUIG-RUNTIME-R002.2 through R002.6. These cases have not been run.

- `tests/host/test_terminal_host_served.py::test_effective_log_preference_is_authenticated`: start the real host and enter run_host_terminal. Check the strict effective web_logs value and host, workspace, tenant, principal, session, generation and expiry bindings. Change each binding independently. Missing, altered or expired bindings yield zero HostLogReader.read calls and zero delivered records.
- `tests/host/test_terminal_host_served.py::test_terminal_log_mode_preference_matrix`: use terminal, web and both with true and false preferences. Only terminal/both with true show a correlated HTTP log during agent interaction. False, absent preference, missing reader and all web cases produce zero TUI reads. Check HTTP serving, the required redacted sink and agent interaction remain available where their interfaces are selected.
- `tests/host/test_terminal_host_served.py::test_terminal_log_handoff_preserves_scope_and_lifecycle`: two tenants share a workspace. Use the production adapter and reader to prove tenant-local records, rotation gaps, reconnect authorization, denial clearing and bounded cancellation. Preserve existing single-engine, single-multiplexer and owned-thread shutdown assertions.

Missing receipt fields or public consumer exports block positive served acceptance. Helper success or a log file alone cannot satisfy it. Record both revisions and the authenticated handoff identity; never credentials.

## Additional owner requirements

- `ARBORA-HOST-R023.3`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-HOST-R008.6`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-HOST-R026.6`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-HOST-R022.1`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-HOST-R010.8`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-HOST-R032`: Future tests cover acceptance and refusals in requirements.md. Tests have not run.
- `ARBORA-HOST-R033`: Unit tests drive the action policy and the served engine with recording doubles. They prove a create never reaches `AddNode`, including an add-only binding with an existing answered inbox record: capability detection refuses it and a direct create leaves the record and write count unchanged. These helper tests do not qualify older-store onboarding.

## Unsupported interface refusal

`ARBORA-HOST-R010.9` binds to tests/agent_host/test_interface_refusal.py.
Sentinels reject workspace initialization, both graph builders and listener calls.
Cover provided graphs, workspace construction, tag construction and defaults.
Reject enabled options and malformed booleans. Preserve omitted and false options.
These checks do not prove authorized host logs or terminal startup.

- `ARBORA-HOST-R009.7`: Exercise the real host routes with controlled session, grant and record owners over loopback HTTP. Check tenant/session isolation, denial, revocation during a read, expiry and record bounds. No deployed grant configuration is claimed.

- `ARBORA-HOST-R009.8`: Mounted terminal checks await observed log and refusal states with a finite deadline.
  A delayed real HTTP reader exercises scheduling beyond the old fixed sleep.
  Revocation still clears previously shown records.

- `ARBORA-HOST-R009.8`: Use real temporary files and loopback HTTP to test private projections, redaction, rotation, unsafe links and disabled viewing. Keep deployed policy and restart replay pending.

- R009.8.2.2: Real retained-file restart through HTTP version two preserves
  source generation, sequence, request, timestamp and severity. Version one
  remains unchanged. Unsupported versions and malformed metadata refuse.

- R009.8.2.2.1: Run all four cases in `tests/agent_host/test_log_sink_terminal_lifecycle.py`
  against the installed `tuig==2.3.6` native wheel. Check the installed binary
  against its distribution record. Drive the real terminal through a PTY and
  inspect rendered cells. Keep the 0/0.6-second read delays and 0/0.04-second
  sink-write delays. Pause the consumer while rotating retained files, then
  resume its original cursor. Revocation and retention gaps clear old text and
  do not trigger renewed admission. Missing native tooling fails required Linux
  acceptance. Other platforms and deployed grant owners remain unqualified.
- R009.8.2.2.2: Installed Overstory metadata and gap acceptance remain open.

The R010.7.2.2.1 launcher attachment accepts an explicit borrowed runtime bundle.
It rejects conflicting legacy resource arguments and absent admission before
owner calls. Actual create_agent_server revalidates the exact bundle through
check_runtime. Changed owner bindings refuse. Successful admission still refuses
client startup while interface authority and lifecycle composition are unavailable.
These controlled tests establish no native or served-client qualification.

## R010.7.2.2.2.1 ASGI readiness

`tests/agent_host/test_runtime_lifespan.py` uses real ASGI lifespan handling with
controlled existing owners. It tests expiry, revocation, changed revisions,
resource replacement, cancellation and borrowed ownership. Missing runtime facts
refuse before readiness. These fixtures grant no deployed access and do not
qualify native session evidence, continuous request authorization or UI launch.

## Bounded RunEvent JSON

`tests/test_run_event_payloads.py` binds ARBORA-HOST-R001.2.1 and ARBORA-A2A-R001.
Maximum ASCII, BMP, control, quote and astral-Unicode replies round-trip through
the actual sink and reply projection for completed/stopped runs.
The event mapping stays unchanged. Exactly 1 MiB of serialized JSON is accepted.
One byte more becomes a bounded payload-error record retaining event identity and type.
Serialization failures emit no private values. Task reads return typed unavailable
reply metadata while preserving native completion status.
A larger-than-65,536-character output remains intact in the stored event when it
fits. It never appears as a falsely complete artifact. Failed-run partial output
and malformed legacy records are not exposed or repaired. These bounded fixture
checks do not substitute for native-engine restart acceptance.

- `ARBORA-HOST-R001.2.1.1`: `tests/mcp_server/test_task_plan.py` exercises the
  shared HostedInvocation method with controlled EG transport: scope and PDP
  refusal, post-response revocation, principal mismatch, immutable host caps,
  caller authority rejection and unavailable dependency contracts. These tests
  do not qualify native EG publication or rolling runtime execution.


### ARBORA-HOST-R001.2.1.2: executable-plan admission prerequisites

These are pending producer/consumer acceptance requirements, not passing tests:
- Repeating the same admitted plan and run returns the same durable attachment;
  a conflicting plan for that run refuses without changing canonical content.
- Stale, expired or foreign worker/epoch/token, owner, tenant or graph refuses
  before plan persistence or AU execution. Recheck current caller authority.
- A persistence failure starts no AU task. Success stores the full canonical
  tasks, dependencies, output, limits and plan/policy digests atomically.
- Close and reopen the real engine store; authorized readback returns the same
  canonical plan and binding. Revoked or foreign callers cannot read it.
- Enter through the registered run-decision provider. Planner requests consume
  the operator's run budget; only successfully attached canonical content maps
  to AU DependencyPlan. No task starts before successful attachment.
- Preserve default single-step behavior without the operator capability;
  explicit dependency requests refuse rather than silently run serially.

Controlled runtime composition evidence now lives in
`tests/mcp_server/test_task_plan_runtime.py`. The registered run-decision provider consumes a zero-tool model proposal and evaluates it through the shared HostedInvocation. It publishes both canonical plan and host-bounded proposal with the private native fence. It reads canonical content back and runs through AU's DependencyPlan loop. Publication failure, mismatched readback, exhausted
planner budget and host fields from the model refuse before task dispatch.
These checks use a real model adapter and AU runtime with controlled EG transport. They do not replace real-engine restart and fence acceptance.
Planner usage is included in Arbora's execution result and deducted from AU's
remaining limits. AU terminal RunEvents still report execution-only usage until
AU offers an initial-usage reporting input.


`tests/mcp_server/test_task_plan_settings.py` validates the normal settings
composition path, default-disabled behavior and malformed settings refusal
before process authority. The current-plan reader accepts the generated
`task_plan` field but does not project it publicly. Shared receipt verification
passes raw caller identity to EG, which owns identity hashing. An existing
attached plan refuses retry rather than replaying a later scaffold as the
original publication; original receipt recovery is still pending.

## Temporary request-loop ownership

ARBORA-HOST-R001.2.3 binds `tests/test_epistemic_loop_lifecycle.py`.
The tests require temporary clients to close before their loop exits.
They preserve the serving reader across repeated requests and enforce tenant isolation.
Failed requests, cancellation during cleanup and repeated close keep deterministic outcomes.
A cleanup failure remains visible after the remaining clients close.

## Unique action-decision audit refusal

For `ARBORA-HOST-R033`, the schema receipt tests force a fixed audit ID collision
and a write exception through the real action-policy owner. Neither may produce
an effect-authorizing receipt or call the schema publisher. The audit backend
records fixture operations only; no native persistence acceptance is claimed.

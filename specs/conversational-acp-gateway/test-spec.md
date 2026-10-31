# ARBORA-ACP — Test specification

| Test ID | Requirement | Level | Fixture and input | Expected observation |
|---|---|---|---|---|
| T-ACP-01 | ACP-01 | Unit | No principal, adapter available | Typed unauthenticated refusal; no session object returned. |
| T-ACP-02 | ACP-01 | Unit | Verified principal, adapter available | Admission succeeds with a bound snapshot. |
| T-ACP-03 | ACP-02 | Unit | Admitted session, attempted field mutation on the returned snapshot | Snapshot is immutable. A second admission call for a different principal produces an independent session. |
| T-ACP-04 | ACP-03 | Unit | Admitted session, clock advanced past idle limit with no activity | Next-use check returns typed idle-expired refusal. |
| T-ACP-05 | ACP-04 | Unit | Admitted session, recent activity but clock advanced past absolute limit | Next-use check returns typed expired refusal despite recent activity. |
| T-ACP-06 | ACP-05 | Unit | Verified principal, adapter unavailable, admission attempted | Typed `chat_adapter_unavailable` refusal; no session admitted. |
| T-ACP-07 | ACP-05 | Unit | Admitted session from a prior available state, adapter goes unavailable, message attempted | Typed `chat_adapter_unavailable` refusal on the message check. No synthesized reply. |
| T-ACP-08 | ACP-06 | Integration | Same credential resolved through the existing identity path and through ACP admission | Identical principal/tenant/scope snapshot on both paths. |
| T-ACP-09 | ACP-07 | Unit | Status/doctor read with adapter unavailable and configured limits | Output reports adapter state and limits. No token or session secret present. |
| T-ACP-10 | R001.3 | Unit | Request with empty root or unknown capability | Construction is rejected; types are frozen. |
| T-ACP-11 | R001.4 | Unit | Trusted root, read, write, then standing grant | Read passes; write needs permission; standing grant skips the prompt. |
| T-ACP-12 | R001.5 | Integration | App after `register_graph_routes` | ACP route exists; `session/new` reaches `CodingSessionAdmission.admit`. |
| T-ACP-13 | R001.6 | Unit | Untrusted root, undeclared capability, denied permission, no client | One typed refusal each; nothing forwarded. |
| T-ACP-14 | R001.7 | Unit | Admit, granted write, denied command | One trace record each; no file content. |
| T-ACP-15 | R001.8 | Integration | `session/new` with a temporary trusted root and a fake client | Read passes, write waits for permission, untrusted root refused. |

## Release proof

Record the exact commit, `uv run pytest` result for the targeted module, and the fast-subset hook results (`complexity-staged`, `kiss-staged`, `dupehound-changed`, `jscpd-differential`) on the changed files. A source test alone does not establish served or integration acceptance against a real meristem adapter. That remains open until a live receipt exists.


## R001.9 current-request authority guard

`tests/acp/test_session_policy.py` checks each message against a separately
resolved principal. Missing identity, a different principal or tenant, and
both widened and narrowed scopes refuse without updating activity. An equal
verified snapshot retains existing idle, absolute expiry and unavailable-adapter
behavior. These are policy tests only; they do not establish credential
verification, a served ACP transport, permission exchange or durable recovery.


## R001.9.1 SDK projection source qualification

`tests/acp/test_adapter.py` starts a real stdio subprocess and communicates
through the pinned ACP SDK. The host uses a FunctionModel and synthetic test
authority; client reads use a temporary file. Tests distinguish session identity
from conversation history, which remains host-owned. They cover cancellation,
refused effects and absolute client paths, bounded text and previews, absent
adapter authority and explicit unsupported recovery. These source-overlay tests
are not installed AU wheel or authenticated remote transport acceptance.

The protocol projection reuses `default_coding_presenter` from harness 0.57.0
and native Pydantic stream event types. It does not run the autonomous Pydantic
ACP adapter. Private thinking and raw tool arguments/results are not published.
Only selected client file locations and non-executing diffs are public.


`tests/acp/test_local_history.py` checks predecessor binding, completed-text codec compatibility, foreign sessions, stale runs and byte/turn limits.
It also checks failed and cancelled turns, uncertain saves and close cleanup.
`test_adapter.py` also drives two real SDK prompts through the source local
history composition and proves prior text reaches the scripted model while
cancelled/concurrent prompts and a different session do not. The local backend
startup tests use the existing ephemeral identity issuer and an unknown model
provider to prove startup/failure/exit behavior without provider credentials or
network traffic. No durable or installed release acceptance follows from these
source tests.


The stdio wrapper retains SDK JSON-RPC parsing but imposes hard 64 KiB newline
frames, 512 inbound frames, a 300-second receive idle limit and a one-hour
connection lifetime. SDK 0.11's buffer setting alone is not a hard frame limit.
EOF, truncated/oversized frames, caller cancellation and child cleanup are tested.
A streamed answer followed by a completion-store failure yields an error, never
an acknowledged persisted continuation. Local prompts are limited to 16 KiB and
outbound updates to 64 KiB per turn. None of these bounds establish durable replay.

## R001.9.2 Experimental HTTP/SSE source qualification

`tests/acp/test_http_transport.py` uses the actual ACP SDK over a real loopback
HTTP/SSE carrier, including the shared identity middleware with an ephemeral
RSA-signed JWT and injected test JWKS. It checks invalid/expired/wrong-audience
credentials, foreign principals/tenants/scopes, bounded queues/messages/body
read time, cancellation, connection lifetime, disconnect and application shutdown.
The model is scripted; this does not qualify live providers, installed producer
wheels, durable recovery or standardized ACP Streamable HTTP. Registration
without a trusted application factory fails closed with authenticated 503.

`test_native_observer.py` checks concurrent principal/run isolation and unchanged request/tool binding.
It checks absent, foreign and reused native admission, plus copied observer revocation.
It also checks callback failure, cancellation and missing producer APIs.
These host-plumbing tests are not central operation admission or history proof.
Separately admitted child runs do not inherit the parent's observer.
This includes child asyncio tasks from server tools.
The same native run cannot bind twice. Central cancellation requires an actual task-owner cancellation receipt.
Canceling an awaiting Python task alone does not settle its native lease.
The admission callback test drives the real authority dispatch with a controlled
work-item owner. It checks returned owner IDs before execution, ignores forged
message IDs and proves callback failure prevents execution. It covers owner
cancellation, callback cancellation, failed settlement and inconsistent readback.
Each case admits once without replay. These tests do not prove live engine storage.

`test_real_local_composition_success_history_cancel_and_close` starts the actual
local entrypoint in a subprocess. Only the public model provider supplies a
FunctionModel; local authority minting, default planning and bounded history stay
unchanged. It checks a successful turn, continuation, cancellation without saving
the cancelled turn, and wire close. Existing startup tests cover missing explicit
local selection, invalid limits and invalid roots without protocol success.

## R001.9.3 Governed central source qualification

`tests/acp/test_central_runtime.py` drives the real ACP SDK and HTTP/JWT path
through HostedInvocation, operation policy and A2A admission.
It retains the native task plan, AU dependency execution, server tools and owner commit. FunctionModel and
controlled graph/effect owner ports replace external services. The final answer
is published without private thinking, tool results or intermediate node output.
Tests cover first-turn success, second-turn refusal, nonterminal outcomes,
cancellation before admission and during execution, uncertain cancellation, and
same-connection bearer renewal before the original token expires. The prompt
after original expiry uses the new verified caller. These are source-path tests,
not live graph storage, provider, deployment or installed-release acceptance.

Shutdown tests delay threaded admission beyond the cancellation and shutdown waits.
The factory reports uncertainty but retains the operation callback. Late admission
then cancels through the owner without execution. Expired credentials cannot
renew between authority-watch checks.

Renewal tests refuse changed groups, roles, actor kind, policy, audience, issuer,
JWKS location and configured algorithms. Rotated expiry with unchanged verified
authorization remains supported. Active execution retains its admitted authority.

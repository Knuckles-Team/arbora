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

Record the exact commit, `uv run pytest` result for the targeted module, and the fast-subset hook results (`complexity-staged`, `kiss-staged`, `dupehound-changed`, `jscpd-differential`) on the changed files. A source test alone does not establish served or integration acceptance against a real agent-utilities adapter. That remains open until a live receipt exists.

## R001.9 duplex qualification

`tests/acp/test_duplex.py` covers bounded correlation, ordered events, client
binding and cancellation. `tests/acp/test_routes.py` runs real loopback HTTP/SSE
requests with a client that reads an isolated temporary file.
Write and command requests produce previews without effects. Fixture authority is
explicitly injected; this is not live identity or native approval acceptance.

# Unary A2A facade

Arbora exposes one authenticated, unary-only A2A boundary over the existing
agent-utilities WorkItem and signed agent-dispatch authorities. It does not
create an A2A-specific task database, broker, worker, or orchestration loop.

## Discovery and transports

The authenticated Agent Card is served at
`/.well-known/agent-card.json`. The card advertises JSON-RPC at `/a2a`,
`text/plain` messages, bearer
security, and truthfully disables streaming, push notifications, and state
transition history.

The same service is projected as the `graph_a2a` MCP tool and its exact
action-routed REST twin, `POST /graph/a2a`. Supported operations are:

| A2A JSON-RPC | `graph_a2a` action | Required scope |
|---|---|---|
| `message/send` | `send` | `kg:write` |
| `tasks/get` | `get` | `kg:read` |
| `tasks/list` | `list` | `kg:read` |
| `tasks/cancel` | `cancel` | `kg:write` |
| Agent Card | `card` | `kg:read` |

`message/send` requires an `Idempotency-Key` header on the native A2A route and
an `idempotency_key` argument on MCP/REST. Reusing a key with the same normalized
request returns the same task. Reusing it with a different request is a conflict.
Task reads, listing cursors, and cancellation are isolated to the verified
tenant and task owner.

<ol class="site-flow" aria-label="A2A request flow">
  <li class="site-flow__step"><span class="site-flow__title">Authenticate</span><span class="site-flow__body">A2A, MCP, or REST establishes a verified bearer GraphSession.</span></li>
  <li class="site-flow__step"><span class="site-flow__title">Normalize</span><span class="site-flow__body">The typed unary service resolves one canonical capability request.</span></li>
  <li class="site-flow__step"><span class="site-flow__title">Admit</span><span class="site-flow__body">The WorkItem authority applies tenant, ownership, and idempotency fences.</span></li>
  <li class="site-flow__step"><span class="site-flow__title">Dispatch</span><span class="site-flow__body">agent-utilities receives a signed agent request.</span></li>
  <li class="site-flow__step"><span class="site-flow__title">Record</span><span class="site-flow__body">RunTrace and tool-call provenance bind the result to the request.</span></li>
</ol>

## Reply persistence and bounds

Reply artifacts contain at most 65,536 characters. Larger replies are unavailable
as artifacts, not silently shortened and presented as complete. A task's native
WorkItem status remains authoritative independently of artifact availability.

`tasks/get` reads complete terminal RunEvent JSON from the caller-authorized
graph after verifying task ownership. The event payload is bounded at 1 MiB,
including Unicode escaping and metadata. Events exceeding that bound or failing
serialization retain their original identity and completion type in an explicit
payload-error record. This record never contains partial JSON or fabricated output. Task reads preserve
the native status and return no artifacts plus `metadata.arbora.reply` with
`status: unavailable` and reason `payload_too_large`, `payload_serialization_failed`,
or `reply_too_large`. Missing events yield no artifact; malformed or conflicting
stored payloads refuse. A local cache is not evidence of durable recovery.

## Inline text files

Messages accept `file` parts with a `file` object containing base64 `bytes`,
`mimeType: "text/plain"`, and an optional `name`. File content must be nonempty
UTF-8 text. A message may contain at most eight files and 65,536 total decoded
file bytes, further bounded by `MAX_UPLOAD_SIZE`. The decoded text follows the
same routing, redaction, authorization and model-input path as text parts.
Names are labels only. URI references, invalid encodings and other media types
are refused before task creation. Image input remains unavailable pending the
selected model's authoritative media capability contract.

## Tool-subset assembly status

The intended multiplexer exposure is the smallest authorized tool subset that
covers the selected agent graph within the caller's context budget. This is not
treated as an advisory list: execution must cryptographically carry and enforce
the exact selection.

The current epistemic-graph `AgentAssemble` operation returns unavailable, and
the signed AU `AgentTurnEnvelope` has no allowed-tool-subset field.
Therefore a request containing `contextBudgetTokens`, or any route decision with
a non-empty selected-tool set, fails closed before durable admission. Ordinary
routing to an existing authorized agent remains live. Streaming and push are
not part of the advertised Agent Card.

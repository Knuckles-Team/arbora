# Evidence — hosted owner/readback acceptance

This file binds live acceptance to existing hosted API rows. It supplements
source tests. Generated delivery state remains unchanged. Two fresh
idempotency keys were used. No older reservation was replayed.

## Source and deployment proof

| Proof | Exact value |
|---|---|
| Arbora source | [`15764cc9792b8f536421e8eaca5fcaa9bcea5c46`](https://github.com/Knuckles-Team/arbora/commit/15764cc9792b8f536421e8eaca5fcaa9bcea5c46) |
| Deployed image | `sha256:2c9e179fc6dc90fa010bde2735c04836660ba555bf6491ffdaf97f04a3290fe0` |
| Deployment | `arbora-f9f4f764b-84p2g`; Ready; zero restarts; 731 live payload hashes and the exact source marker verified |
| Native contract | epistemic-graph `2.27.7` |
| Hosted qualification | [Arbora Release workflow 40355229100](https://github.com/Knuckles-Team/arbora/actions/runs/40355229100) — required gates/build and supported-install qualification passed |
| Focused source tests | `uv run --offline --no-sync pytest -q tests/mcp_server/test_current_plan_reader.py tests/api/test_execution_graph.py` (command-private cache) — exit `0` |

Auditors can compare the image digest with the running deployment. The
operator receipt digest is
`58faca735613528a27720249face0901905227c1a78cf7a0f035f381a618cbf3`.

## Fresh concurrent owner/readback acceptance

Two fresh runs overlapped. Both native WorkItems started leased and later
completed successfully.

### `run-9aea72bf1a54c6680fea222cacbd5519`

* WorkItem: `work-item:8c54d0030a0b4c3151cf05cb8d5efb82a22b141bd182920ad6ff6f4cdf7ece22`.
* Plan digest: `sha256:bdc02db84e1638c356058fb272146174defcc50267ea0f6e9dc0c6d41ad52dd6`.
* Active owner: `leased`; authenticated tenant binding.
* Active reads: `current_plan` revision 0; future expiry; graph with 3 nodes and 2 edges.
* Active events: `run_started`, `task_started`.
* Terminal owner: `succeeded`; run `completed`; both plan and graph unavailable after lease end; 8 public events.

### `run-5c451764721b7e06e3344ebab0f0822d`

* WorkItem: `work-item:0fb2323079724a3f90ac4aa2230ee67f412d8d83fe605ebd7827a97fdaf135a4`.
* Plan digest: `sha256:1815c1e434ae645b9655c6850bb63738d09874b6f7763e3254cd4394f52cb68c`.
* Active owner: `leased`; authenticated tenant binding.
* Active reads: `current_plan` revision 0; future expiry; graph with 3 nodes and 2 edges.
* Active events: `run_started`, `task_started`.
* Terminal owner: `succeeded`; run `completed`; both plan and graph unavailable after lease end; 8 public events.

Each plan returned `published.verified=true`. Each active snapshot had an
observation time no later than now and an expiry later than now. The public
owner view omits native epoch, fence and expiry fields. Arbora binds those
fields from the native claim.

## Requirement binding and disposition

The receipt supplements these Arbora leaves. It does not add requirements or
promote rollups.

| Criterion | Existing requirement | Disposition |
|---|---|---|
| Verified owner admission and owner-bound current-plan publication/read | `.1.1`, `.1.2` | Already `VERIFIED`. Live owner and plan proof added. |
| Owner-scoped public event/provenance read | `.4.7` | Already `VERIFIED`. Live event proof added. |
| Negotiated flat execution graph during a live lease | `.8.1` | Already `VERIFIED`. Live flat graph proof added. |
| Full graph rollup and nested lineage | `.8`, `.8.2` | Remains `SPECIFIED`. No nested lineage proof. |

The related producer rows remain with their owners and are not promoted by a
consumer receipt:

* `MERISTEM-HARNESS-R014.6` through `MERISTEM-HARNESS-R014.11` remain `SPECIFIED`. No AU
  producer run, model-request proof, or stored-history proof occurred.
* `MEDULLA-DURABLE-KERNEL-R028.4.1` is already `VERIFIED` in epistemic-graph.
  `MEDULLA-DURABLE-KERNEL-R028.5` and `.5.1`–`.5.3` remain EG-owned `SPECIFIED`
  rows. This is consumer evidence only. No EG source landing, restart proof,
  or `.5.4` benchmark is claimed.

No `status.json` was hand-edited. Generated status remains authoritative. This
file records deployed acceptance and its limits.

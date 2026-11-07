# Competency questions

Seam between `ARBORA-HARNESS-PLUGINS` and medulla. Owner rows: `MEDULLA-CQ-ORCH-R820` in medulla and `ARBORA-HARNESS-PLUGINS-R004` and `ARBORA-HARNESS-PLUGINS-R001` in this repository.

Provider: arbora ARBORA-HARNESS-PLUGINS-R004
Consumer: medulla MEDULLA-CQ-ORCH-R820

## Wire shape

The question rows read the `Workstream` nodes, the `attachedTo` edges, and the checkpoint turns. The answer lists each harness session, its harness, its attach time and its last checkpoint text. A second question lists the skill and agent nodes that the bundle composer reads for one caller.

## Refusal codes

- `WORKSTREAM_NOT_FOUND`: the caller owns no workstream for the workspace.

## Test vectors

- `crates/medulla-rdf/tests/orchestration_competency.rs::q820_workstream_sessions_answer`.

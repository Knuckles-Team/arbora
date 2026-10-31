# ARBORA-ACCEPTANCE — evidence acceptance freeze

**Owner:** arbora for its own requirement acceptance gate. See [requirements.md](requirements.md) for the definition of every requirement ID and the computed status (`pipelines-hook spec-status`) for its delivery state and evidence. This spec does not change `scripts/check_public_specs.py`'s evidence schema; it freezes the exact subset of that schema a Arbora requirement must carry before a human auditor may move its `acceptance_state` from `NOT_AUDITED` to `ACCEPTED`.

## User outcome

A reviewer opening any Arbora spec's `status.json` can tell, without reading implementation source, whether a `LANDED` requirement is merely merged or is actually `ACCEPTED`: proven live on the default branch, exercised through a real entry point, and currently operational. A requirement cannot self-promote from `LANDED` to `ACCEPTED` by accumulating more delivery-lane commits; only a named auditor, acting on a checker's report, may record the promotion, and the recorded evidence may not later be edited to manufacture a pass.

## Requirements

- **FR-001**: Define the exact evidence-kind set a `LANDED` requirement must carry to earn `ACCEPTED`: a `merged_head` receipt naming a commit on the default branch, a `test` receipt at that same commit naming a test identifier reached through a real entry point (not a unit mock), and an operational receipt (`consumer` or `release`) at that same commit proving the row is live. Acceptance: A requirement missing any one of the three evidence kinds, or with evidence recorded at a different commit than its `merged_head`, is reported as a gap and cannot be marked `ACCEPTED`. Rows: `ARBORA-ACCEPTANCE-R001`.
- **FR-002**: Ship a typed acceptance-evidence model and a checker that, given a spec's `status.json`, lists every `LANDED` (or `CLOSED`) requirement lacking one or more of the FR-1 evidence kinds at its `merged_head` commit, naming the missing kind(s) per requirement. Acceptance: Running the checker against a fixture `status.json` with a fully evidenced row and a gapped row reports exactly the gapped row's ID and missing kind(s); a malformed or non-existent `status.json` refuses rather than reporting a false pass. Rows: `ARBORA-ACCEPTANCE-R001.1`.
- **FR-003**: Restrict who may change a requirement's `acceptance_state` to `ACCEPTED`: only a reviewer distinct from the delivery lane that produced the `merged_head` commit, acting after the FR-2 checker reports no gap for that requirement, may record the change. The implementing agent may propose evidence but may not self-attest acceptance. Acceptance: The audit trail (PR review, commit author of the `status.json` edit, or an explicit auditor field) shows a reviewer identity different from the implementation commit's author for every `ACCEPTED` transition. Rows: `ARBORA-ACCEPTANCE-R002`.
- **FR-004**: Make acceptance evidence append-only once recorded: an existing evidence entry's `kind`, `commit`, `result`, or `url` is never edited in place to flip a prior failure to a pass. A correction adds a new entry; the superseded entry is retained. Acceptance: A diff that rewrites an existing evidence entry's `result`, `commit`, or `url` field instead of appending a new entry is rejected by review; the checker in FR-2 only trusts entries matching the current `merged_head` commit. Rows: `ARBORA-ACCEPTANCE-R003`.
- **FR-005**: Operational evidence expires: a `consumer` or `release` receipt used to satisfy FR-1 carries an observation point, and a stale receipt (recorded against a commit no longer at the branch tip, or superseded by a later `merged_head`) no longer counts toward `ACCEPTED` for the current commit. Acceptance: The FR-2 checker treats operational evidence recorded at a commit other than the requirement's current `merged_head` as missing, not as satisfying the gap. Rows: `ARBORA-ACCEPTANCE-R004`.

- **FR-006**: Concept-proof scenarios must define done for each delivery phase P1 to P5. Rows: `ARBORA-ACCEPTANCE-R006`, `ARBORA-ACCEPTANCE-R006.7`.
- **FR-007**: The final program scenario must answer the vision questions through the web UI and a messaging channel. Rows: `ARBORA-ACCEPTANCE-R006.8`.

This spec owns the Arbora acceptance-audit gate, not the evidence schema itself (`scripts/check_public_specs.py` remains authoritative for `EVIDENCE_KINDS`, `DELIVERY_STATES`, and `ACCEPTANCE_STATES`) and not deployment authorization. No row becomes `ACCEPTED` here from a local claim. Acceptance requires the exact merged commit and the three evidence kinds in FR-1 recorded against it.

## User Scenarios & Testing

### User Story 1 - Trust an accepted row (Priority: P1)

A reviewer reads one requirement. The reviewer can tell whether it is merged or proven live on the default branch.

**Acceptance Scenarios**:

1. **Given** a `LANDED` row with all three evidence kinds at its merged commit, **When** the checker runs, **Then** the checker reports no gap.
2. **Given** a row that lacks one evidence kind, **When** the checker runs, **Then** the checker names the missing kind.

### User Story 2 - Close a delivery phase by scenario (Priority: P2)

A release owner closes each phase P1 to P5 only when its scenario passes. P5 is the final program gate.

**Acceptance Scenarios**:

1. **Given** the P4 scenario passed, **When** the P5 scenario runs on the ephemeral stack, **Then** the answers join connectors and virtual graphs with provenance.
2. **Given** the P5 scenario passed, **When** a person asks a vision question in the web UI and in a messaging channel, **Then** both channels return the same cited answer.

### Edge Cases

- Evidence recorded at an older commit does not count toward the current commit.
- An implementing agent cannot promote its own row.
- A malformed `status.json` refuses and never reports a pass.
- A source the caller cannot read appears in the coverage block with a reason code.

## Success Criteria

- **SC-001**: A gapped row is reported by ID with its missing evidence kinds. Rows: `ARBORA-ACCEPTANCE-R001.1`.
- **SC-002**: The P5 scenario passes on the ephemeral stack at the tagged commit. Rows: `ARBORA-ACCEPTANCE-R006.7`, `ARBORA-ACCEPTANCE-R006.8`.

## Assumptions

- The evidence schema stays in `scripts/check_public_specs.py`.
- The ephemeral stack of `ARBORA-ACCEPTANCE-R006.6` hosts every scenario that needs fixture connectors.

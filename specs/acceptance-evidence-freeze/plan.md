# ARBORA-ACCEPTANCE — Design and implementation plan

Governing spec: [spec.md](spec.md). Rows: [requirements.md](requirements.md).

## Summary

This plan serves the rows of `ARBORA-ACCEPTANCE` in requirements.md. It keeps the earlier design record under Phase 1. The north star and the delivery phase of the spec close the plan.

## Technical Context

- **Language/Version**: Python 3.14 (floor 3.12)
- **Primary Dependencies**: Named per row in requirements.md.
- **Storage**: N/A. The checker reads status files.
- **Testing**: pytest. Each row names the test and the proof level that it needs.
- **Target Platform**: Linux hosts and CI runners
- **Project Type**: Service and command-line tools
- **Performance Goals**: N/A. A row states its own budget when it has one.
- **Constraints**: Public files stay neutral and portable. A row states any further limit.
- **Scale/Scope**: The rows of requirements.md.

## Constitution Check

- **I. One owner per concept**: No departure is recorded in this plan; the rows carry the proof.
- **II. Extend before you create**: No departure is recorded in this plan; the rows carry the proof.
- **III. Specs describe the current state only**: No departure is recorded in this plan; the rows carry the proof.
- **IV. Requirements are neutral and controlled**: No departure is recorded in this plan; the rows carry the proof.
- **V. Hard cutover, no legacy path**: No departure is recorded in this plan; the rows carry the proof.
- **VI. Names carry no version suffix**: No departure is recorded in this plan; the rows carry the proof.
- **VII. One engine, no second database, no sync**: No departure is recorded in this plan; the rows carry the proof.
- **VIII. Ask the graph, not a bespoke operation**: No departure is recorded in this plan; the rows carry the proof.
- **IX. Connectors depend on the SDK and are called through arbora**: No departure is recorded in this plan; the rows carry the proof.
- **X. Every ontology class rolls up to the core**: No departure is recorded in this plan; the rows carry the proof.
- **XI. Code stays small, simple and unduplicated**: No departure is recorded in this plan; the rows carry the proof.
- **XII. No ratchets and no suppressions**: No departure is recorded in this plan; the rows carry the proof.
- **XIII. Zero flakes**: No departure is recorded in this plan; the rows carry the proof.
- **XIV. Compile first, then review**: No departure is recorded in this plan; the rows carry the proof.
- **XV. Python 3.14 standard, 3.12 floor**: No departure is recorded in this plan; the rows carry the proof.
- **XVI. Install and deploy only from published releases**: No departure is recorded in this plan; the rows carry the proof.
- **XVII. Publish the gate's tool before the gate**: No departure is recorded in this plan; the rows carry the proof.
- **XVIII. Landed means reachable**: No departure is recorded in this plan; the rows carry the proof.
- **XIX. A tag push publishes, with no manual approval**: No departure is recorded in this plan; the rows carry the proof.
- **XX. Secrets are never traced or logged**: No departure is recorded in this plan; the rows carry the proof.
- **XXI. Public files stay portable and neutral**: No departure is recorded in this plan; the rows carry the proof.

## Project Structure

```text
specs/acceptance-evidence-freeze/        existing: spec.md, plan.md, requirements.md, tasks.md, test-spec.md
```

Code paths are named per row in requirements.md. This plan adds no path of its own.

## Phase 0: Research

No unknowns; no research.md.

## Phase 1: Design

### Existing system and reuse

`scripts/check_public_specs.py` already defines `EVIDENCE_KINDS`, `DELIVERY_STATES`, and `ACCEPTANCE_STATES`, and its `_receipt_errors`/`_requirement_entry_errors` functions already refuse a `status.json` that claims `ACCEPTED` or `LANDED` without a matching `merged_head` receipt. This spec does not duplicate that schema. It adds a narrower, standalone **readiness** checker that answers a different question than the existing validator: not "is this `status.json` well-formed," but "which `LANDED` rows, across one or more specs, are not yet evidenced enough for an auditor to accept them." The existing validator is a structural gate; this checker is an audit worklist generator, reusable by a human auditor before they touch `acceptance_state` at all.

### Architecture

A new module, `scripts/check_acceptance_evidence.py`, exposes:

- a typed `EvidenceRecord` and `RequirementGap` data model (the "typed acceptance-evidence model" of `ARBORA-ACCEPTANCE-R001`), built with `dataclasses` to stay dependency-free like `check_public_specs.py`;
- a pure function `gaps_for_status(data: dict) -> list[RequirementGap]` that, given parsed `status.json` content, returns one `RequirementGap` per `LANDED`/`CLOSED` requirement missing `merged_head`, `test`, or operational (`consumer`/`release`) evidence at the same commit;
- a `load_status(path: Path) -> dict` loader that raises `AcceptanceEvidenceError` (never returns a false-empty result) on missing file, unreadable JSON, or a non-object document — the refusal path required by `ARBORA-ACCEPTANCE-R001.1`;
- a `main()` CLI entry point usable standalone (`python3 scripts/check_acceptance_evidence.py specs/<name>/status.json`) or across every tracked spec directory with no arguments, printing a deterministic gap report and a nonzero exit code when any gap exists.

No existing module is modified; `check_public_specs.py`'s structural validation keeps running unchanged in the same pre-commit/CI gate.

### Interfaces and data model

```python
@dataclass(frozen=True)
class EvidenceRecord:
    kind: str
    commit: str
    result: str
    url: str


@dataclass(frozen=True)
class RequirementGap:
    requirement_id: str
    merged_head_commit: str
    missing_kinds: tuple[str, ...]
```

`missing_kinds` is a subset of `{"test", "operational"}` plus the degenerate case where `merged_head` itself is absent (reported as `{"merged_head"}`, since no gate can proceed without it). This is additive: no existing JSON field, required key, or exit-code contract of `check_public_specs.py` changes.

### Live integration path

Entry point: a developer or CI job runs `python3 scripts/check_acceptance_evidence.py` (no args) from the repository root; it discovers every `specs/*/status.json` tracked under `specs/`, loads each with `load_status`, and prints one line per gapped requirement plus a summary count, exiting `1` if any gap exists and `0` otherwise — mirroring `check_public_specs.py`'s own `main()` contract so it can be wired into the same pre-commit hook group later without a second convention.

### Quality and release gates

Targeted pytest: `tests/test_check_acceptance_evidence.py`, covering a fully evidenced row (no gap), a `LANDED` row missing `test` evidence, a `LANDED` row whose operational evidence is stale against a newer `merged_head`, and the refusal path for a missing/malformed `status.json`. No scanner gaming: the checker only reports, it never mutates `status.json` or a requirement's state.

### Risks, alternatives, and decisions

Alternative considered: extending `check_public_specs.py`'s `_requirement_entry_errors` to also fail on an incomplete `ACCEPTED`-readiness set for `LANDED` rows. Rejected for this slice because that function enforces structural correctness of claims already made (a row that claims `ACCEPTED` without evidence is a lie and must hard-fail CI); a `LANDED` row that is simply not yet audited is not a lie and must not fail the public-specs gate. The two concerns stay in separate tools so the readiness worklist can be run informationally without blocking delivery-lane PRs.

### Concept-proof phases

Delivery follows five phases. Each phase closes a larger loop. A family joins the earliest phase whose scenario exercises it (`ARBORA-ACCEPTANCE-R006`).

| Phase | Proves | Families |
|---|---|---|
| P1 | An agent writes an evidenced claim and a later run retrieves it under policy | Durable kernel core writes, contract basics, arbora identity, host and ingress, meristem harness cycle, retrieval and context bundles, run-trace vocabulary |
| P2 | The loop is reliable, inspectable and measurably useful | Provenance and bitemporal reads, version-checked writes, retrieval outcomes and evaluation, one SDK source-ingest connector |
| P3 | Ownership boundaries hold with several agents and humans | Meristem boundary cuts, arbora fleet and A2A, node-level visibility, contradiction records |
| P4 | The unified planner earns its scope | One cross-modal plan with its falsification gate, write-time shapes and a small entailment set, memory decay and consolidation |
| P5 | Answers across connectors and across virtual graphs (`ARBORA-ACCEPTANCE-R006.7`), then scale and breadth | Cross-connector competency answers, federated reads over virtual graphs, execution answers from onboarded tools, source refusals in coverage, cluster and resharding, lakehouse formats, finance, media vocabularies, wire-protocol parity, every other family |

P5 work starts only after P4 passes. Each phase has a scenario; P1 is `R006.1`, P2 `R006.2`, P3 `R006.3`, P4 `R006.4` and P5 `R006.7`. If the P4 falsification gate fails, the unified planner stops expanding. Medulla then serves as the durable evidence store, and cross-modal work runs through contracts.

## Phase 2: Task approach

tasks.md carries one numbered task line for each active row. Tasks run producer first. A task marked `[P]` may run in parallel with its neighbours.

## Complexity Tracking

none

## Build contract

Each row names its test. The repository pre-commit and pre-push hooks and the pull request gates apply. No row may use a suppression or a ratchet.

## Production caller chain

Each row names its entry point and caller chain in its own text. This plan adds no separate chain.

## Cross-repo seams

The `Depends on` lines of requirements.md name each seam by the row ID that owns it in the other repository.

## North star

Delivery runs in phases P1 to P5. Concept-proof scenarios define each phase (`ARBORA-ACCEPTANCE-R006`). The exit gate is competency questions. The ontology answers how to execute a task and produces cross-source reports from connector virtual graphs. Deployment comes only from published package releases. Releases ship delivered cross-repository spec bundles.

## Delivery phase

Rows `R006.1` to `R006.4` serve P1 to P4. Row `R006.7` serves P5, the final validation. P5 runs only on the ephemeral stack, because it needs onboarded fixture connectors and their virtual graphs.

P5 scenario design: the scenario asserts four answers through the served `ask` and `find` operations. They are a cross-connector join, a federated read over two virtual graphs and engine facts, an execution-plan answer, and refusals that name excluded sources in the coverage block. Arbora supplies the stack and the served operations. Medulla supplies the engine rows `MEDULLA-CQ-XSRC-R012`, `MEDULLA-CQ-CONN-R090` and `MEDULLA-CQ-ORCH-R012`. The release manifest and its scenario field are `PIPE-RELEASE-R023`. The earlier tracker checks for the live door (discovery, connector calls, data, agents, governance, terminal UI) map to P1 to P4. The one open tracker check, a cross-source query over at least two ingested sources, is closed by P5.

The program final gate has four parts. All specs land. Connectors reach zero open rows with service health green. Deployment comes from published releases on `main`. The vision questions are answered through the web UI and a messaging channel. Row `ARBORA-ACCEPTANCE-R006.8` is the last part and closes P5 together with `R006.7`. `PIPE-RELEASE-R023` and the pipelines section "Program exit gates" list the other parts.

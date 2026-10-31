# ARBORA-ACCEPTANCE — Design and implementation plan

## Existing system and reuse

`scripts/check_public_specs.py` already defines `EVIDENCE_KINDS`, `DELIVERY_STATES`, and `ACCEPTANCE_STATES`, and its `_receipt_errors`/`_requirement_entry_errors` functions already refuse a `status.json` that claims `ACCEPTED` or `LANDED` without a matching `merged_head` receipt. This spec does not duplicate that schema. It adds a narrower, standalone **readiness** checker that answers a different question than the existing validator: not "is this `status.json` well-formed," but "which `LANDED` rows, across one or more specs, are not yet evidenced enough for an auditor to accept them." The existing validator is a structural gate; this checker is an audit worklist generator, reusable by a human auditor before they touch `acceptance_state` at all.

## Architecture

A new module, `scripts/check_acceptance_evidence.py`, exposes:

- a typed `EvidenceRecord` and `RequirementGap` data model (the "typed acceptance-evidence model" from FR-2/R001), built with `dataclasses` to stay dependency-free like `check_public_specs.py`;
- a pure function `gaps_for_status(data: dict) -> list[RequirementGap]` that, given parsed `status.json` content, returns one `RequirementGap` per `LANDED`/`CLOSED` requirement missing `merged_head`, `test`, or operational (`consumer`/`release`) evidence at the same commit;
- a `load_status(path: Path) -> dict` loader that raises `AcceptanceEvidenceError` (never returns a false-empty result) on missing file, unreadable JSON, or a non-object document — the refusal path required by FR-2;
- a `main()` CLI entry point usable standalone (`python3 scripts/check_acceptance_evidence.py specs/<name>/status.json`) or across every tracked spec directory with no arguments, printing a deterministic gap report and a nonzero exit code when any gap exists.

No existing module is modified; `check_public_specs.py`'s structural validation keeps running unchanged in the same pre-commit/CI gate.

## Interfaces and data model

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

## Live integration path

Entry point: a developer or CI job runs `python3 scripts/check_acceptance_evidence.py` (no args) from the repository root; it discovers every `specs/*/status.json` tracked under `specs/`, loads each with `load_status`, and prints one line per gapped requirement plus a summary count, exiting `1` if any gap exists and `0` otherwise — mirroring `check_public_specs.py`'s own `main()` contract so it can be wired into the same pre-commit hook group later without a second convention.

## Quality and release gates

Targeted pytest: `tests/test_check_acceptance_evidence.py`, covering a fully evidenced row (no gap), a `LANDED` row missing `test` evidence, a `LANDED` row whose operational evidence is stale against a newer `merged_head`, and the refusal path for a missing/malformed `status.json`. No scanner gaming: the checker only reports, it never mutates `status.json` or a requirement's state.

## Risks, alternatives, and decisions

Alternative considered: extending `check_public_specs.py`'s `_requirement_entry_errors` to also fail on an incomplete `ACCEPTED`-readiness set for `LANDED` rows. Rejected for this slice because that function enforces structural correctness of claims already made (a row that claims `ACCEPTED` without evidence is a lie and must hard-fail CI); a `LANDED` row that is simply not yet audited is not a lie and must not fail the public-specs gate. The two concerns stay in separate tools so the readiness worklist can be run informationally without blocking delivery-lane PRs.

## Concept-proof phases

Delivery follows five phases. Each phase closes a larger loop. A family joins the earliest phase whose scenario exercises it (`ARBORA-ACCEPTANCE-R006`).

| Phase | Proves | Families |
|---|---|---|
| P1 | An agent writes an evidenced claim and a later run retrieves it under policy | Durable kernel core writes, contract basics, arbora identity, host and ingress, meristem harness cycle, retrieval and context bundles, run-trace vocabulary |
| P2 | The loop is reliable, inspectable and measurably useful | Provenance and bitemporal reads, version-checked writes, retrieval outcomes and evaluation, one SDK source-ingest connector |
| P3 | Ownership boundaries hold with several agents and humans | Meristem boundary cuts, arbora fleet and A2A, node-level visibility, contradiction records |
| P4 | The unified planner earns its scope | One cross-modal plan with its falsification gate, write-time shapes and a small entailment set, memory decay and consolidation |
| P5 | Answers across connectors and across virtual graphs (`ARBORA-ACCEPTANCE-R006.7`), then scale and breadth | Cross-connector competency answers, federated reads over virtual graphs, execution answers from onboarded tools, source refusals in coverage, cluster and resharding, lakehouse formats, finance, media vocabularies, wire-protocol parity, every other family |

P5 work starts only after P4 passes. Each phase has a scenario; P1 is `R006.1`, P2 `R006.2`, P3 `R006.3`, P4 `R006.4` and P5 `R006.7`. If the P4 falsification gate fails, the unified planner stops expanding. Medulla then serves as the durable evidence store, and cross-modal work runs through contracts.

## North star

Delivery runs in phases P1 to P5. Concept-proof scenarios define each phase (`ARBORA-ACCEPTANCE-R006`). The exit gate is competency questions. The ontology answers how to execute a task and produces cross-source reports from connector virtual graphs. Deployment comes only from published package releases. Releases ship delivered cross-repository spec bundles.

## Delivery phase

Rows `R006.1` to `R006.4` serve P1 to P4. Row `R006.7` serves P5, the final validation. P5 runs only on the ephemeral stack, because it needs onboarded fixture connectors and their virtual graphs.

P5 scenario design: the scenario asserts four answers through the served `ask` and `find` operations. They are a cross-connector join, a federated read over two virtual graphs and engine facts, an execution-plan answer, and refusals that name excluded sources in the coverage block. Arbora supplies the stack and the served operations. Medulla supplies the engine rows `MEDULLA-CQ-XSRC-R012`, `MEDULLA-CQ-CONN-R090` and `MEDULLA-CQ-ORCH-R012`. The release manifest and its scenario field are `PIPE-RELEASE-R023`. The earlier tracker checks for the live door (discovery, connector calls, data, agents, governance, terminal UI) map to P1 to P4. The one open tracker check, a cross-source query over at least two ingested sources, is closed by P5.

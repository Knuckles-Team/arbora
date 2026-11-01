# ARBORA-ACCEPTANCE — Quickstart

Run the P1 closed-loop scenario (`ARBORA-ACCEPTANCE-R006.1`) on one node with the in-process stack of `ARBORA-ACCEPTANCE-R006.5`:

```
uv run pytest tests/scenarios/test_p1_closed_loop.py::test_write_retrieve_under_policy_and_trace
```

The scenario passes when the claim returns with all provenance fields, the trace links it to the run, and the foreign tenant and stopped-engine steps refuse with stable codes.

Run the acceptance checker with `uv run python scripts/check_acceptance_evidence.py`.

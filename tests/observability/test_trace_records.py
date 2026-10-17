"""Trace record ids and the error-detail sink."""

from __future__ import annotations

import pytest

from arbora.observability import error_detail_sink as eds
from arbora.observability import trace_ontology as to


def test_trace_and_outcome_ids_are_stable_and_refuse_an_empty_run():
    assert to.trace_id("run-1") == to.trace_id("run-1")
    assert to.outcome_id("run-1").startswith("outcome:")
    with pytest.raises(ValueError):
        to.trace_id("")


class _Engine:
    def __init__(self, rows=None, fail=False):
        self.nodes = {}
        self.rows = rows
        self.fail = fail

    def add_node(self, node_id, label, properties):
        self.nodes[node_id] = (label, properties)

    def link_nodes(self, *_args):
        return None

    def query_cypher(self, _query, _params):
        if self.fail:
            raise RuntimeError("engine down")
        return self.rows or []


def test_error_detail_sink_writes_and_reads_back_one_record():
    record = {
        "error_class": "E",
        "failing_layer": "engine",
        "traceback": "tb",
        "tenant_id": "t",
    }
    engine = _Engine(rows=[record])
    sink = eds.GraphErrorDetailSink(engine)
    sink("corr-1", record)
    assert engine.nodes["error_detail:corr-1"][0] == eds.ERROR_DETAIL_NODE_LABEL
    assert sink.resolve("corr-1") == record


@pytest.mark.parametrize("engine", [None, _Engine(fail=True), _Engine(rows=[])])
def test_error_detail_sink_resolves_nothing_without_a_durable_record(engine):
    assert eds.GraphErrorDetailSink(engine).resolve("corr-1") is None

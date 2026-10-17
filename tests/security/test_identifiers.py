"""Identifier gates for interpolated graph and SQL names."""

from __future__ import annotations

import pytest

from graph_os.security.identifiers import (
    InvalidIdentifierError,
    quote_sql_identifier,
    validate_identifier,
    validate_sql_identifier,
)

MALICIOUS = [
    "x; DROP TABLE--",
    'x" ; DROP TABLE users; --',
    "x` ; DROP TABLE users; --",
    "x' OR '1'='1",
    "Ｐerson",
    "café",
    "",
    "1abc",
    "a b",
    "a\nb",
    "a.b",
]
GATES = [validate_identifier, validate_sql_identifier, quote_sql_identifier]


@pytest.mark.parametrize("gate", GATES)
@pytest.mark.parametrize("name", MALICIOUS)
def test_every_gate_refuses_injection_shapes(gate, name) -> None:
    with pytest.raises(InvalidIdentifierError) as refused:
        gate(name)
    if name:
        assert name not in str(refused.value) and name not in repr(refused.value)


@pytest.mark.parametrize(
    ("gate", "longest"),
    [(validate_identifier, 128), (validate_sql_identifier, 63)],
)
def test_length_ceiling(gate, longest) -> None:
    assert gate("a" * longest) == "a" * longest
    with pytest.raises(InvalidIdentifierError):
        gate("a" * (longest + 1))


def test_refusal_names_the_kind_and_is_a_value_error() -> None:
    with pytest.raises(ValueError) as refused:
        validate_identifier("bad;name", kind="relationship type")
    assert refused.value.kind == "relationship type"


@pytest.mark.parametrize("name", ["Person", "kg_edges", "_private", "Node_1"])
def test_valid_names_pass_and_quote(name) -> None:
    assert validate_identifier(name) == name
    assert quote_sql_identifier(name) == f'"{name}"'

"""The names of the finance delegation grant (ARBORA-DATA-MARKET-R002).

An owner's delegation to the scheduler is one engine resource grant on the resource
``finance-delegation:<owner>``; these are the only names both the writer
(``finance.delegation.grant``) and the reader (the schedule host) share.
"""

from __future__ import annotations

__all__ = ["DELEGATION_PROTOCOL", "delegation_grant_id", "delegation_resource"]

#: The engine protocol the delegation is recorded under (finance data is graph data).
DELEGATION_PROTOCOL = "knowledge"


def delegation_resource(owner: str) -> str:
    return f"finance-delegation:{owner}"


def delegation_grant_id(owner: str, grant: str) -> str:
    return f"finance-delegation:{owner}:{grant}"

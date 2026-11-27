"""Authenticated changeset review over medulla ``Decide`` (ARBORA-OPS-R870).

``review.changeset`` reads the diff through the ``SourceReader`` port, builds finding
options with the ``review_producer`` port (default: the repository-manager review tool
through ``fleet.call``), asks ``Decide`` with ``ChangesetReview`` and returns only the
sealed record digest, bound to the exact base and head revisions.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.ops._common import Params, bound_service, build_read_op
from arbora.api.ops._decide import decide_record, record_digest
from arbora.api.registry import OpSpec, Verb


class ReviewChangesetParams(Params):
    repository: str = Field(min_length=1, max_length=256)
    base_rev: str = Field(min_length=1, max_length=128)
    head_rev: str = Field(min_length=1, max_length=128)


class ReviewChangesetResult(Params):
    value: dict[str, Any]


async def handle_review_changeset(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    producer = bound_service(
        context, "review_producer", reason="review producer is not composed"
    )
    reader = bound_service(
        context, "source_reader", reason="source reader is not composed"
    )
    diff = await reader.diff(
        repository=params["repository"],
        base_rev=params["base_rev"],
        head_rev=params["head_rev"],
        context=context,
    )
    findings = await producer.findings(diff=diff, context=context)
    record = await decide_record(
        context,
        "changeset_review",
        {
            "repository": params["repository"],
            "base_rev": params["base_rev"],
            "head_rev": params["head_rev"],
            "findings": list(findings),
        },
    )
    return {
        "value": {
            "record_digest": record_digest(record),
            "base_rev": params["base_rev"],
            "head_rev": params["head_rev"],
        }
    }


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            verb=Verb.ASK,
            op_id="review.changeset",
            summary="Review a changeset and return the sealed decision digest",
            examples=("review this changeset",),
            params=ReviewChangesetParams,
            result=ReviewChangesetResult,
            handler="arbora.api.ops.review.handle_review_changeset",
            scope="kg:read",
        ),
    )


specs = operations

__all__ = ["ReviewChangesetParams", "handle_review_changeset", "operations", "specs"]

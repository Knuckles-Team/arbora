"""Versioned, idempotent data migrations of Arbora's persisted records.

A persisted record type is written by one function and stored with the one
label model of :func:`arbora.unique_records.record_body`. When its label or
its field set changes, the change ships with a migration here, in the same
pull request. ``tests/test_persisted_shapes.py`` fails when the writer's
current shape matches no migration's declared ``shape_digest``.

:func:`run_data_migrations` runs at serving start under the process authority
(after the base graphs exist). Each migration that completes is recorded as
one ``DataMigration`` node ``data_migration:<migration_id>`` holding its
counts, so a later start skips it and an operator can read what ran. A
migration that leaves rows behind is not recorded and runs again next start.
A failure is logged with its cause; serving continues, and readers that
depend on the repaired rows keep failing closed with a reason that names them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from arbora.unique_records import create_record, record_body

logger = logging.getLogger(__name__)

__all__ = [
    "DATA_MIGRATIONS",
    "DATA_MIGRATION_LABEL",
    "DataMigration",
    "MigrationResult",
    "current_shapes",
    "run_data_migrations",
    "shape_digest",
]

DATA_MIGRATION_LABEL = "DataMigration"

#: Rows one migration step reads per query; the loop runs until none remain.
_BATCH = 200
#: Hard stop for one migration in one start: a writer still producing bad rows
#: must not spin startup forever.
_MAX_BATCHES = 500


@dataclass(frozen=True)
class MigrationResult:
    """What one migration found, changed and left behind."""

    found: int
    changed: int
    remaining: int


@dataclass(frozen=True)
class DataMigration:
    """One recorded data migration of one persisted label.

    ``shape_digest`` is :func:`shape_digest` of the label's current writer
    once this migration has run: the shape gate requires the newest migration
    of a label to declare the shape its writer produces today.
    """

    migration_id: str
    label: str
    shape_digest: str
    summary: str
    apply: Callable[[Any, str], MigrationResult]


def shape_digest(label: str, fields: Iterable[str]) -> str:
    """Fingerprint of one persisted record shape: its label and field names."""
    document = json.dumps(
        {"label": label, "fields": sorted(set(fields))}, separators=(",", ":")
    )
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def _rows(engine: Any, query: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        r for r in engine.query_cypher(query, dict(params)) or [] if isinstance(r, dict)
    ]


def _count_type_only(engine: Any, label: str) -> int:
    rows = _rows(
        engine,
        "MATCH (n {type: $label}) WHERE n.node_type IS NULL RETURN count(n) AS n",
        {"label": label},
    )
    return int(rows[0].get("n") or 0) if rows else 0


def label_type_only_rows(label: str) -> Callable[[Any, str], MigrationResult]:
    """A migration step: add the ``node_type`` label to ``type``-only ``label`` rows.

    Sets exactly the one field the current writer sets and old rows lack
    (``node_type`` = ``label``, see :func:`arbora.unique_records.record_body`).
    Every other field and timestamp is left as it was. Each row is changed by
    an atomic compare-and-set that applies only while ``type`` is ``label``
    and ``node_type`` is absent, so a re-run or a concurrent run changes
    nothing twice.
    """

    def apply(engine: Any, migration_id: str) -> MigrationResult:
        found = _count_type_only(engine, label)
        changed = 0
        for _ in range(_MAX_BATCHES):
            ids = [
                str(row["id"])
                for row in _rows(
                    engine,
                    "MATCH (n {type: $label}) WHERE n.node_type IS NULL "
                    f"RETURN n.id AS id LIMIT {_BATCH}",
                    {"label": label},
                )
                if row.get("id")
            ]
            if not ids:
                break
            progressed = False
            for node_id in ids:
                if engine.compare_and_set_node(
                    node_id,
                    {"type": label, "node_type": None},
                    {"node_type": label},
                    idempotency_key=f"arbora:data-migration:{migration_id}:{node_id}",
                ):
                    changed += 1
                    progressed = True
            if not progressed:
                break  # rows the CAS will not change: report them as remaining
        return MigrationResult(
            found=found, changed=changed, remaining=_count_type_only(engine, label)
        )

    return apply


def _action_decision_shape() -> str:
    """The shape the one ``ActionDecision`` writer produces today."""
    from arbora.access.action_policy import (
        ACTION_DECISION_LABEL,
        ActionDecision,
        ActionRequest,
        action_decision_record,
    )

    sample = ActionDecision(
        decision="deny", tier="forbidden", request=ActionRequest(kind="k", target="t")
    )
    body = record_body(ACTION_DECISION_LABEL, action_decision_record(sample))
    return shape_digest(ACTION_DECISION_LABEL, body)


#: Every data migration, oldest first. Append only: never edit or reorder a
#: migration that has shipped; a new label or shape change adds a new entry.
DATA_MIGRATIONS: tuple[DataMigration, ...] = (
    DataMigration(
        migration_id="0001-action-decision-node-type-label",
        label="ActionDecision",
        # shape_digest("ActionDecision", record_body(..., action_decision_record(..)))
        shape_digest="759c9595052f9bb75aba6f0f52c70e7fc54cee6ac83cbf6446091ce78babc2bd",
        summary=(
            "Label ActionDecision rows written type-only by arbora <= 0.1.8.dev4 "
            "(ServedEngine wrote no node_type before ccce23e3) so the policy "
            "ledger can read them; adds node_type only."
        ),
        apply=label_type_only_rows("ActionDecision"),
    ),
)


def current_shapes() -> dict[str, str]:
    """``{label: shape digest}`` of every persisted type the shape gate covers."""
    return {"ActionDecision": _action_decision_shape()}


def _recorded(engine: Any, migration_id: str) -> bool:
    return bool(
        _rows(
            engine,
            "MATCH (m:DataMigration {migration_id: $id}) RETURN m.id AS id LIMIT 1",
            {"id": migration_id},
        )
    )


def _record(engine: Any, migration: DataMigration, result: MigrationResult) -> None:
    from arbora import __version__

    create_record(
        engine,
        f"data_migration:{migration.migration_id}",
        DATA_MIGRATION_LABEL,
        {
            "migration_id": migration.migration_id,
            "label": migration.label,
            "summary": migration.summary,
            "shape_digest": migration.shape_digest,
            "found": result.found,
            "changed": result.changed,
            "remaining": result.remaining,
            "arbora_version": __version__,
            "applied_at": datetime.now(UTC).isoformat(),
            "applied_unix": time.time(),
        },
    )


def run_data_migrations(
    engine: Any, migrations: Iterable[DataMigration] = DATA_MIGRATIONS
) -> list[dict[str, Any]]:
    """Run every unrecorded migration under the ambient (process) session.

    Returns one report per migration: ``status`` is ``recorded`` (ran before),
    ``applied`` (ran and recorded now), ``incomplete`` (rows remain; not
    recorded) or ``failed`` (with the error class). Never raises.
    """
    reports: list[dict[str, Any]] = []
    if engine is None or not callable(getattr(engine, "compare_and_set_node", None)):
        logger.warning("data migrations skipped: the engine has no compare-and-set")
        return reports
    for migration in migrations:
        report: dict[str, Any] = {"migration_id": migration.migration_id}
        try:
            if _recorded(engine, migration.migration_id):
                report["status"] = "recorded"
            else:
                result = migration.apply(engine, migration.migration_id)
                report.update(
                    found=result.found,
                    changed=result.changed,
                    remaining=result.remaining,
                )
                if result.remaining:
                    report["status"] = "incomplete"
                else:
                    _record(engine, migration, result)
                    report["status"] = "applied"
        except Exception as exc:  # noqa: BLE001 - one migration's fault must not stop serving
            report.update(
                status="failed", error=f"{type(exc).__name__}: {str(exc)[:200]}"
            )
        log = (
            logger.info if report["status"] in ("recorded", "applied") else logger.error
        )
        log("data migration %s", json.dumps(report, sort_keys=True))
        reports.append(report)
    return reports

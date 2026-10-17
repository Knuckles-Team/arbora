"""Render the engine's exported row-visibility policy bundle into external-engine policy.

The engine exports one bundle per tenant describing row visibility (owner, visibility
and grants) plus the marking-to-role predicates it derives. This module fetches that
bundle, renders it into each target engine's native policy shape (Trino file rules,
OpenSearch document-level-security roles, Lakekeeper tuple sets) and applies it
idempotently. It does not define what a marking means and does not decide row
visibility: it is a faithful mechanical translation. A bundle whose ``governs`` names
a mechanism this module does not recognise is denied, never applied on a best-effort
basis. Any fetch or parse failure fails closed (deny all targets).

Applying is off unless ``EXTERNAL_POLICY_SYNC_ENABLED`` is set.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict

from agent_utilities.core.config import setting

logger = logging.getLogger(__name__)


def _render_dls_query(excluded_markings: Any) -> dict[str, Any]:
    names = sorted({str(m).strip() for m in excluded_markings if str(m).strip()})
    if not names:
        return {"match_all": {}}
    clause = (
        {"term": {"marking": names[0]}}
        if len(names) == 1
        else {"terms": {"marking": names}}
    )
    return {"bool": {"must_not": [clause]}}


def _render_dls_query_for_role(role_tokens: Any, all_markings: Any) -> dict[str, Any]:
    held = {str(t).strip() for t in role_tokens}
    return _render_dls_query(
        [name for name in all_markings if f"marking:{name}" not in held]
    )


__all__ = [
    "GOVERNS_SUPPORTED",
    "MARKING_ROLE_PREFIX",
    "RESERVED_MARKING_COLUMN",
    "RequiresRolePredicate",
    "MarkingPolicyEntry",
    "BundleCaller",
    "PolicyBundle",
    "FetchedBundle",
    "BundleFetcher",
    "TrinoTargetTable",
    "TrinoRule",
    "OpenSearchRoleConfig",
    "OpenFgaTuple",
    "LakekeeperTupleSet",
    "RenderedTarget",
    "Renderer",
    "TrinoRenderer",
    "OpenSearchRenderer",
    "LakekeeperRenderer",
    "TargetClient",
    "InMemoryTargetClient",
    "ApplyOutcome",
    "ApplyReport",
    "Applier",
    "external_policy_sync_enabled",
    "invalidate_cache",
    "is_dirty",
    "clear_dirty_state",
]

# Matches au's `Marking.role_token` (`ontology/permissioning.py`) and eg's
# `MARKING_ROLE_PREFIX` (`policy_export/mod.rs`) -- kept as one named constant
# per repo so the two conventions stay visibly the SAME string, never
# independently re-typed. Do not change without changing both.
MARKING_ROLE_PREFIX = "marking:"

# Matches eg's `RESERVED_MARKING_COLUMN` (`policy_export/mod.rs`) -- the
# proposed, not-yet-universally-adopted per-row marking-name column
# convention. See the module doc's per-target gap table for what actually
# carries this today (OpenSearch: yes, as `marking`; Trino: no).
RESERVED_MARKING_COLUMN = "_markings"

# The only `governs` value the engine's bundle emits today. A bundle whose
# `governs` is not a subset of this set is DENIED ,
# never best-effort rendered.
GOVERNS_SUPPORTED: frozenset[str] = frozenset({"M1"})

# Trino's real file-based-access-control table-rule key for a row filter is
# `filter`, NOT `row_filter` -- the bundle contract names the field
# `row_filter` (an engine-neutral name chosen for the JSON contract), so this
# module translates at render time. Named here so the translation is a single
# grep-able site, not an inline literal.
_TRINO_FILTER_KEY = "filter"


def _exact_user_pattern(subject: str) -> str:
    """Trino's file-based access control matches a rule's ``user`` as a REGEX
    against the username. A subject is an opaque identifier, not a pattern, so
    it is escaped before it becomes one -- an unescaped ``.`` or ``+`` in a
    subject would silently widen a per-subject rule to other users, which is
    the same class of defect (a rule matching more principals than the bundle
    describes) as the catch-all this module refuses to emit at all. See
    :class:`TrinoRenderer`.
    """
    return re.escape(subject)


# ── Bundle schema (mirrors eg's `policy_export::PolicyBundle`, `serde`-exact) ──


@dataclass(frozen=True, slots=True)
class RequiresRolePredicate:
    """Decoded `MarkingPredicate::RequiresRole` (eg `policy_export/mod.rs`).

    A row carrying ``column`` == ``role``'s marking name is visible only to a
    principal whose effective role set contains ``role`` (always
    ``marking:<name>``, per :data:`MARKING_ROLE_PREFIX`).
    """

    role: str
    column: str

    @property
    def marking_name(self) -> str:
        """The bare marking name (`role` with the ``marking:`` prefix stripped)."""
        if self.role.startswith(MARKING_ROLE_PREFIX):
            return self.role[len(MARKING_ROLE_PREFIX) :]
        return self.role


def _decode_predicate(predicate_json: str) -> RequiresRolePredicate:
    """Decode one `markings.<name>.predicate` JSON string.

    Fails loudly (never silently substitutes a permissive/restrictive default)
    on an unrecognized `kind` or a malformed payload -- a renderer that cannot
    understand a predicate must not guess.
    """
    try:
        raw = json.loads(predicate_json)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"malformed marking predicate JSON: {predicate_json!r}"
        ) from exc
    kind = raw.get("kind")
    if kind != "requires_role":
        raise ValueError(
            f"unrecognized marking predicate kind {kind!r} -- this module only "
            "understands 'requires_role' (eg's MarkingPredicate::RequiresRole); "
            "a future predicate kind must add a matching decoder here before "
            "any renderer can safely consume it"
        )
    role = raw.get("role")
    column = raw.get("column")
    if not role or not column:
        raise ValueError(f"malformed requires_role predicate: {predicate_json!r}")
    return RequiresRolePredicate(role=str(role), column=str(column))


@dataclass(frozen=True, slots=True)
class MarkingPolicyEntry:
    """One `markings.<name>` bundle entry -- the opaque predicate, decoded."""

    predicate_json: str

    def decode(self) -> RequiresRolePredicate:
        return _decode_predicate(self.predicate_json)


@dataclass(frozen=True, slots=True)
class BundleCaller:
    """Python mirror of eg's `policy_export::BundleCaller`.

    **This is ONE subject, not a population.** eg has no engine-owned principal
    inventory (its `policy_export` module doc's "Owed" section), so a bundle
    describes exactly the verified caller it was generated for. A renderer MUST
    NOT read it as "the set of principals": it cannot answer "who holds role
    R?", and it says nothing at all about any other subject's roles.

    It replaced a `principals: dict[subject, roles]` map on 2026-09-04
    (`plans/refactor/DECISIONS.md` RF-RULING-001). That map was both forgeable
    (`Method::PolicyExport` accepted it from the caller) and structurally
    misreadable as an inventory -- see :class:`TrinoRenderer` for the concrete
    fail-open that misreading produced.
    """

    subject: str
    effective_roles: tuple[str, ...]

    def holds(self, role: str) -> bool:
        """Does THIS caller hold ``role``? The only membership question a
        single-caller bundle can answer."""
        return role in self.effective_roles


@dataclass(frozen=True, slots=True)
class PolicyBundle:
    """Python mirror of eg's `policy_export::PolicyBundle` (extended per the W0 review appendix: `governs` + `tenant`/`graphs`, A1/A3).

    Constructed via :meth:`from_json` from the engine's exported bundle -- never
    hand-built for anything but a test fixture, so a real bundle's shape is
    always validated on the way in.
    """

    version: str
    generated_from: str
    governs: tuple[str, ...]
    tenant: str
    graphs: tuple[str, ...]
    caller: BundleCaller
    markings: dict[str, MarkingPolicyEntry]

    @classmethod
    def from_json(cls, raw: str | dict[str, Any]) -> PolicyBundle:
        data = json.loads(raw) if isinstance(raw, str) else raw
        try:
            markings = {
                str(name): MarkingPolicyEntry(
                    predicate_json=json.dumps(entry["predicate"])
                )
                if isinstance(entry.get("predicate"), (dict, list))
                else MarkingPolicyEntry(predicate_json=str(entry["predicate"]))
                for name, entry in dict(data["markings"]).items()
            }
            raw_caller = dict(data["caller"])
            caller = BundleCaller(
                subject=str(raw_caller["subject"]),
                effective_roles=tuple(str(r) for r in raw_caller["effective_roles"]),
            )
            return cls(
                version=str(data["version"]),
                generated_from=str(data["generated_from"]),
                governs=tuple(str(g) for g in data["governs"]),
                tenant=str(data["tenant"]),
                graphs=tuple(str(g) for g in data["graphs"]),
                caller=caller,
                markings=markings,
            )
        except KeyError as exc:
            raise ValueError(
                f"policy bundle JSON is missing required field {exc}"
            ) from exc

    def governs_recognized(self) -> bool:
        """the binding rule: a bundle governing anything outside
        :data:`GOVERNS_SUPPORTED` must be denied, never partially applied."""
        return bool(self.governs) and set(self.governs) <= GOVERNS_SUPPORTED


@dataclass(frozen=True, slots=True)
class FetchedBundle:
    """A :class:`PolicyBundle` plus fetch-time metadata the bundle itself does
    not carry (it has no wall-clock timestamp -- `generated_from` is a content
    hash, not a time). Staleness (never longer than one policy
    epoch) is therefore a property of the FETCH, not the bundle.
    """

    bundle: PolicyBundle | None
    fetched_at_monotonic: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.bundle is not None and self.error is None


class BundleFetcher:
    """Fetches the engine's exported bundle over `/policy/export`.

    The HTTP client is imported lazily inside :meth:`fetch`, so constructing a
    `BundleFetcher` never reaches network code and renderer/Applier tests stay
    fixture-only.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token_provider: TokenProvider | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_provider = token_provider
        self._timeout = timeout_seconds

    def fetch(
        self, *, tenant: str, graphs: list[str], marking_names: list[str]
    ) -> FetchedBundle:
        import time

        import httpx

        headers = {}
        if self._token_provider is not None:
            headers["Authorization"] = f"Bearer {self._token_provider()}"
        params: list[tuple[str, str | int | float | bool | None]] = [("tenant", tenant)]
        params.extend(("graph", g) for g in graphs)
        params.extend(("marking", m) for m in marking_names)
        try:
            with httpx.Client(timeout=self._timeout, headers=headers) as client:
                response = client.get(f"{self._base_url}/policy/export", params=params)
                response.raise_for_status()
                bundle = PolicyBundle.from_json(response.json())
            return FetchedBundle(bundle=bundle, fetched_at_monotonic=time.monotonic())
        except Exception as exc:  # noqa: BLE001 -- any fetch/parse failure is fail-closed data
            logger.warning("policy bundle fetch failed: %s", exc)
            return FetchedBundle(
                bundle=None, fetched_at_monotonic=time.monotonic(), error=str(exc)
            )


class TokenProvider(Protocol):
    def __call__(self) -> str: ...


# ── Render targets ──────────────────────────────────────────────────────────


class TrinoRule(TypedDict):
    catalog: str
    schema: str
    table: str
    filter: str


class OpenSearchRoleConfig(TypedDict):
    index_pattern: str
    role: str
    dls_query: dict[str, Any]


class OpenFgaTuple(TypedDict):
    user: str
    relation: str
    object: str


class LakekeeperTupleSet(TypedDict):
    namespace: str
    table: str
    openfga_tuples: list[OpenFgaTuple]


@dataclass(frozen=True, slots=True)
class RenderedTarget:
    """One target's rendered output plus a stable digest for idempotency."""

    target: str
    payload: list[dict[str, Any]]
    not_applicable: tuple[str, ...] = field(default_factory=tuple)
    """Human-readable reasons a marking could NOT be rendered for this target
    (e.g. Lakekeeper row-granularity mismatch) -- never silently dropped."""

    def digest(self) -> str:
        canonical = json.dumps(self.payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Renderer(Protocol):
    """`render(bundle) -> RenderedTarget`, per the applier-side split."""

    def render(self, bundle: PolicyBundle) -> RenderedTarget: ...


@dataclass(frozen=True, slots=True)
class TrinoTargetTable:
    """A physical Trino/Iceberg table this deployment renders marking filters
    against. `has_markings_column` MUST be asserted true by the caller after
    confirming the live schema -- this module refuses to guess."""

    catalog: str
    schema: str
    table: str
    has_markings_column: bool = False


@dataclass(frozen=True, slots=True)
class TrinoRenderer:
    """Renders `renderings.trino` -- at most one `filter` rule per (marking,
    table), scoped to the ONE subject the bundle describes
    (:class:`BundleCaller`), pushdown-safe by construction: Trino's own
    file-based access control evaluates `filter` against the table's real
    columns as part of query planning, before any projection/aggregation runs
    -- there is no "already-fetched row" state for a `filter` expression to be
    blind to (the same reasoning the OpenSearch DLS rendering gives for why
    its mechanism is pushdown-safe, and the reasoning the contract requires
    every renderer to state explicitly rather than assume).

    **This renderer never emits a catch-all `"user": ".*"` rule.** It used to,
    whenever no principal in the bundle's `principals` map held the marking
    role. That was fail-OPEN, for a reason the map's shape hid:

    * Trino evaluates table rules **in order, first match wins**. A `.*` rule
      matches every user, so the FIRST marking that emitted one shadowed every
      rule appended after it -- including every other marking's row filter, on
      the same table, in the same render. Rows carrying those markings became
      visible to everyone, silently.
    * The trigger was routine, not exotic. A `principals` map is a population
      type, so "nobody in it holds the role" read as "nobody holds the role".
      Once eg started exporting one verified caller instead of a caller-supplied
      map (`plans/refactor/DECISIONS.md` RF-RULING-001), "nobody in it" became
      the ordinary case for every marking that caller is not cleared for.

    A single-caller bundle simply cannot express a population deny-list: it has
    no way to say anything about a subject it does not name. So this renderer
    says nothing about them -- it emits a rule for the caller when the caller
    lacks the marking role, emits nothing when the caller is cleared, and
    reports the undescribable remainder in
    :attr:`RenderedTarget.not_applicable` on every marking it renders. A gap
    that is reported is a gap an operator can close; a `.*` rule that disables
    the rest of the ruleset is not.
    """

    tables: tuple[TrinoTargetTable, ...]

    def render(self, bundle: PolicyBundle) -> RenderedTarget:
        if not bundle.governs_recognized():
            raise ValueError(
                f"cannot render an unrecognized-governs bundle: {bundle.governs}"
            )
        rules: list[dict[str, Any]] = []
        not_applicable: list[str] = []
        caller = bundle.caller
        for name, entry in bundle.markings.items():
            predicate = entry.decode()
            for tbl in self.tables:
                target = f"{tbl.catalog}.{tbl.schema}.{tbl.table}"
                if not tbl.has_markings_column:
                    not_applicable.append(
                        f"marking '{name}': {target} has no "
                        f"'{predicate.column}' column (not rendered)"
                    )
                    continue
                # The bundle names exactly one subject, so exactly one subject
                # can be ruled on. Everyone else is unaddressed and REPORTED --
                # never covered by a catch-all that would shadow the rest of
                # this ruleset (see the class doc).
                not_applicable.append(
                    f"marking '{name}': {target} rule covers only the bundle's caller "
                    f"'{caller.subject}'; a per-subject deny-list for the rest of the "
                    "population is not derivable from a single-caller bundle (eg has no "
                    "principal inventory) and is deliberately NOT rendered as a "
                    "'user': '.*' catch-all, which Trino's first-match-wins rule order "
                    "would let shadow every later rule"
                )
                if caller.holds(predicate.role):
                    continue  # cleared: no filter rule needed for this caller
                rules.append(
                    {
                        "catalog": tbl.catalog,
                        "schema": tbl.schema,
                        "table": tbl.table,
                        "user": _exact_user_pattern(caller.subject),
                        _TRINO_FILTER_KEY: (
                            f"NOT contains({predicate.column}, '{predicate.marking_name}')"
                        ),
                    }
                )
        return RenderedTarget(
            target="trino", payload=rules, not_applicable=tuple(not_applicable)
        )


@dataclass(frozen=True, slots=True)
class OpenSearchRenderer:
    """Renders `renderings.opensearch` -- one DLS role for the ONE subject the
    bundle describes (:class:`BundleCaller`), reusing the
    `search.dls.render_dls_query_for_role` (this module does not re-derive the
    query shape; see the module doc's "OpenSearch — CLOSED" section for why
    that reuse is deliberate, not incidental).

    OpenSearch DLS is an allow-list per role -- a role's query says which
    documents that role MAY see -- so a bundle describing fewer subjects
    under-grants (fail-closed) rather than over-granting. There is nothing to
    fix here for RF-RULING-001; the renderer simply has one subject to render
    instead of a map's worth."""

    index_patterns: tuple[str, ...]

    def render(self, bundle: PolicyBundle) -> RenderedTarget:
        if not bundle.governs_recognized():
            raise ValueError(
                f"cannot render an unrecognized-governs bundle: {bundle.governs}"
            )
        all_marking_names = sorted(bundle.markings)
        caller = bundle.caller
        query = _render_dls_query_for_role(caller.effective_roles, all_marking_names)
        rows: list[dict[str, Any]] = [
            {
                "index_pattern": pattern,
                "role": f"ca26-{caller.subject}",
                "dls_query": query,
            }
            for pattern in self.index_patterns
        ]
        return RenderedTarget(
            target="opensearch",
            payload=rows,
            not_applicable=(
                f"bundle describes only its caller '{caller.subject}'; DLS roles for "
                "other subjects are not derivable from it (eg has no principal "
                "inventory)",
            )
            if rows
            else (),
        )


@dataclass(frozen=True, slots=True)
class LakekeeperTableRef:
    """A physical Lakekeeper namespace/table this deployment can grant/deny at
    Lakekeeper's own granularity floor."""

    namespace: str
    table: str


@dataclass(frozen=True, slots=True)
class LakekeeperRenderer:
    """Renders `renderings.lakekeeper` -- **only** for markings whose registry
    entry maps to a whole-table object (`table_scope`); every ordinary
    row-scoped marking is reported `not_applicable`, never fabricated as a
    tuple that would not actually gate row content. See the module doc's
    "Lakekeeper — STILL OPEN, AND STRUCTURALLY DIFFERENT" section."""

    table_scope: dict[str, LakekeeperTableRef]
    """marking name -> the one table it wholly covers, when applicable."""

    def render(self, bundle: PolicyBundle) -> RenderedTarget:
        if not bundle.governs_recognized():
            raise ValueError(
                f"cannot render an unrecognized-governs bundle: {bundle.governs}"
            )
        by_table: dict[tuple[str, str], list[OpenFgaTuple]] = {}
        not_applicable: list[str] = []
        caller = bundle.caller
        for name, entry in bundle.markings.items():
            predicate = entry.decode()
            ref = self.table_scope.get(name)
            if ref is None:
                not_applicable.append(
                    f"marking '{name}': row-scoped marking, Lakekeeper's OpenFGA model has "
                    "no row-level relation -- structurally unrenderable at any table this "
                    "lane knows about (not a data gap; the model itself has no row grain)"
                )
                continue
            key = (ref.namespace, ref.table)
            tuples = by_table.setdefault(key, [])
            # An OpenFGA tuple set is an ALLOW-list, so a bundle naming one
            # subject under-grants (fail-closed) rather than over-granting.
            # The un-named remainder is reported, never assumed cleared.
            not_applicable.append(
                f"marking '{name}': grant covers only the bundle's caller "
                f"'{caller.subject}'; clearances for other subjects are not derivable "
                "from a single-caller bundle (eg has no principal inventory)"
            )
            if caller.holds(predicate.role):
                subject = caller.subject
                tuples.append(
                    {
                        "user": f"oidc~{subject}"
                        if not subject.startswith("oidc~")
                        else subject,
                        "relation": "select",
                        "object": f"table:{ref.namespace}/{ref.table}",
                    }
                )
        payload = [
            {"namespace": ns, "table": tbl, "openfga_tuples": tuples}
            for (ns, tbl), tuples in sorted(by_table.items())
        ]
        return RenderedTarget(
            target="lakekeeper", payload=payload, not_applicable=tuple(not_applicable)
        )


# ── Applier ──────────────────────────────────────────────────────────────────


class TargetClient(Protocol):
    """Applies a rendered target's payload to the real engine, and reports the
    last digest it successfully applied (idempotency: the :class:`Applier`
    skips the call entirely when the digest is unchanged)."""

    def last_applied_digest(self) -> str | None: ...

    def apply(self, rendered: RenderedTarget, digest: str) -> None: ...

    def deny_all(self) -> None:
        """Apply a deny-all (empty-permit) configuration -- the fail-closed
        path when no valid bundle is available. MUST NOT raise for "already
        deny-all"; MUST be idempotent the same way `apply` is."""
        ...


@dataclass
class InMemoryTargetClient:
    """A `TargetClient` that records calls instead of reaching a real engine
    -- used by every unit test in this lane, and as the reference
    implementation a live client wraps. Deliberately in this module (not
    test-only) so a caller wiring a dry-run/staging Applier has a real,
    documented no-op target without writing one from scratch."""

    name: str
    applied: list[RenderedTarget] = field(default_factory=list)
    deny_all_calls: int = 0
    _last_digest: str | None = None

    def last_applied_digest(self) -> str | None:
        return self._last_digest

    def apply(self, rendered: RenderedTarget, digest: str) -> None:
        self.applied.append(rendered)
        self._last_digest = digest

    def deny_all(self) -> None:
        self.deny_all_calls += 1
        self._last_digest = "deny-all"


class ApplyOutcome(TypedDict):
    target: str
    action: str  # "applied" | "skipped-unchanged" | "denied-all" | "not-applicable"
    digest: str | None
    not_applicable: list[str]


class ApplyReport(TypedDict):
    disabled: bool
    outcomes: list[ApplyOutcome]


_DENY_ALL_DIGEST = "deny-all"


class Applier:
    """Diffs each renderer's output against the target's last-applied digest
    before writing (idempotent apply, the contract), and fails closed
    -- deny-all on every configured target -- for a missing bundle, a fetch
    error, or an unrecognized `governs`. Never reports success on a partial
    multi-target apply: :meth:`apply_all`'s report lists every target's real
    outcome, and a caller checking "did P8 hold" must inspect all of them, not
    assume a truthy return means every target succeeded.
    """

    def __init__(
        self, renderers: dict[str, Renderer], clients: dict[str, TargetClient]
    ) -> None:
        if set(renderers) != set(clients):
            raise ValueError(
                f"renderers/clients target-name mismatch: {sorted(renderers)} vs {sorted(clients)}"
            )
        self._renderers = renderers
        self._clients = clients

    def apply_all(self, fetched: FetchedBundle) -> ApplyReport:
        if not external_policy_sync_enabled():
            return ApplyReport(disabled=True, outcomes=[])

        outcomes: list[ApplyOutcome] = []
        if (
            not fetched.ok
            or fetched.bundle is None
            or not fetched.bundle.governs_recognized()
        ):
            reason = fetched.error or (
                f"unrecognized governs: {fetched.bundle.governs}"
                if fetched.bundle
                else "no bundle"
            )
            logger.warning("fail-closed: denying all targets (%s)", reason)
            for name, client in self._clients.items():
                client.deny_all()
                outcomes.append(
                    ApplyOutcome(
                        target=name,
                        action="denied-all",
                        digest=_DENY_ALL_DIGEST,
                        not_applicable=[],
                    )
                )
            return ApplyReport(disabled=False, outcomes=outcomes)

        bundle = fetched.bundle
        for name, renderer in self._renderers.items():
            client = self._clients[name]
            rendered = renderer.render(bundle)
            digest = rendered.digest()
            if client.last_applied_digest() == digest:
                outcomes.append(
                    ApplyOutcome(
                        target=name,
                        action="skipped-unchanged",
                        digest=digest,
                        not_applicable=list(rendered.not_applicable),
                    )
                )
                continue
            client.apply(rendered, digest)
            outcomes.append(
                ApplyOutcome(
                    target=name,
                    action="applied",
                    digest=digest,
                    not_applicable=list(rendered.not_applicable),
                )
            )
        return ApplyReport(disabled=False, outcomes=outcomes)


# ── Feature flag + invalidation hook (permission_sync.py's `sync_access` calls
# `invalidate_cache` after a marking is newly applied inbound, per the lane's
# "Call sequence" design) ──────────────────────────────────────────────────


def external_policy_sync_enabled() -> bool:
    """`EXTERNAL_POLICY_SYNC_ENABLED` -- default OFF. Gates the whole
    apply path per the lane's migration/rollback design: flipping this on
    before cutovers are deliberate is the exact "denies Lakekeeper
    access entirely" scenario the lane file warns against."""
    return bool(
        str(setting("EXTERNAL_POLICY_SYNC_ENABLED", "")).strip().lower()
        in {"1", "true", "yes", "on"}
    )


_DIRTY_TENANTS: set[str] = set()


def invalidate_cache(tenant: str, node_id: str) -> None:
    """Mark `tenant` dirty so the next :class:`Applier` cycle re-fetches
    eagerly instead of waiting out the poll interval -- called from
    the access sync after a marking is newly applied inbound. `node_id` is
    accepted but not tracked: the bundle is per tenant, so any single node's change dirties
    the whole tenant's next fetch, never a per-node partial refresh.
    """
    del node_id  # bundle granularity is per-tenant, not per-node
    if tenant:
        _DIRTY_TENANTS.add(tenant)


def is_dirty(tenant: str) -> bool:
    return tenant in _DIRTY_TENANTS


def clear_dirty_state(tenant: str) -> None:
    _DIRTY_TENANTS.discard(tenant)

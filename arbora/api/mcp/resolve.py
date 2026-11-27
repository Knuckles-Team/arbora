"""Intent resolution over already-authorized operation and fleet descriptors.

The caller filters descriptors through scope, principal and Eunomia policy before
passing them here. An order can select an operation, but it cannot authorize it.
The order is the ranking port's (ARBORA-FLEET-R083.2): medulla chooses it, and the
lexical floor answers when the engine cannot. This module scores nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections import Counter, OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from arbora.fleet.ranking import FLOOR_ONLY, RankCandidate, Ranked, RankingPort

_WORDS = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an the of for to in on with and or is are be do what how why my me i you your please".split()
    # ARBORA-FLEET-R014.2: request verbs say how to answer, not what to run.
    + "show display tell give across".split()
    # ARBORA-FLEET-R014.3: question and function words name no operation.
    + "which who whom whose where when there that this these those it its as at by"
    " from into no any can could should would does did has have".split()
)
_READ_VERBS = frozenset({"ask", "why", "find"})
_VERBS = _READ_VERBS | {"write", "act", "manage"}
#: Catalog, glossary, stewardship, lineage, prompt-candidate and virtual-graph
#: source questions. The engine models these as graph entities
#: (MEDULLA-TYPED-PACKS-R106/R108, MEDULLA-DECISION-ENGINE-R814, MEDULLA-FEDERATED-QUERY-R722)
#: and answers them through its query planner (ARBORA-OPS-R855.4).
_GRAPH_QUESTION = re.compile(
    r"\b(catalog|glossary|steward\w*|lineage|upstream|downstream|virtual[ -]graph"
    r"|who owns|owner of|(connector|data) sources?|prompt candidates?"
    r"|system[ -]prompt candidates?)\b"
)
_GRAPH_OPS = ("query.nl", "query.uql")
#: The Agent Library is the engine's own record (ARBORA-FLEET-R052.4.3): a
#: question about it is read from the engine's ``AgentLibrary`` method under
#: ``agent:library-read``, never from the fleet catalog, which holds no agents.
_AGENT_LIBRARY_QUESTION = re.compile(r"\bagent library\b")
_AGENT_LIBRARY_OP = "eg.storage.AgentLibrary"
#: ARBORA-FLEET-R055: ``how can I execute task <iri>`` is the engine's composed answer.
_EXECUTE_TASK_QUESTION = re.compile(
    r"^\W*how can i execute task\s+(\S+)", re.IGNORECASE
)
_EXECUTION_ANSWER_OP = re.compile(r"^eg\.[a-z_]+\.AgentExecutionAnswer$")
#: A delegation names the fleet, not an operation word: ``act`` with an intent
#: that opens with the verb ``delegate`` reaches ``fleet.call`` (ARBORA-FLEET-R052.4.6).
_DELEGATION = re.compile(r"^\W*delegate\b")
_DELEGATE_OP = "fleet.call"
#: The share of a plain question's content terms an op must match to answer it
#: instead of the engine planner (ARBORA-FLEET-R014.3).
_PLAIN_QUESTION_COVERAGE = 0.5
_CACHE_LIMIT = 256
_MIN_EVIDENCE = 0.05
#: The clamp of ``top_k``, applied after the port's order (ARBORA-FLEET-R083.2).
_TOP_K_CLAMP = 20
_RANK_QUESTION = "catalog.operation_rank"


def _tokens(value: str) -> Counter[str]:
    return Counter(word for word in _WORDS.findall(value.lower()) if word not in _STOP)


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _verb(value: object) -> str:
    return str(getattr(value, "value", value)).lower()


def _first_str(item: Mapping[str, Any], *keys: str, default: str = "") -> str:
    """The first truthy value among ``keys``, read from an untrusted mapping."""

    for key in keys:
        value = item.get(key)
        if value:
            return str(value)
    return default


def _str_tuple(item: Mapping[str, Any], key: str) -> tuple[str, ...]:
    return tuple(str(value) for value in item.get(key, ()))


@dataclass(frozen=True)
class Descriptor:
    """A generated op descriptor or one visible fleet catalog item."""

    id: str
    verb: str
    summary: str
    examples: tuple[str, ...] = ()
    domain: str = ""
    tags: tuple[str, ...] = ()
    kind: str = "op"
    params_schema: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, item: Mapping[str, Any]) -> Descriptor:
        """Accept registry descriptors and fleet catalog projections uniformly."""
        identifier = _first_str(item, "op", "id")
        if not identifier:
            raise ValueError("A routing descriptor requires an op or id")
        return cls(
            id=identifier,
            verb=_verb(item.get("verb", "act")),
            summary=_first_str(item, "summary", "description"),
            examples=_str_tuple(item, "examples"),
            domain=_first_str(item, "domain"),
            tags=_str_tuple(item, "tags"),
            kind=_first_str(item, "kind", default="op"),
            params_schema=item.get("params_schema") or {},
        )


@dataclass(frozen=True)
class Candidate:
    descriptor: Descriptor
    score: float
    matched_terms: tuple[str, ...]


@dataclass(frozen=True)
class Resolution:
    """Routing decision; ``preview`` requires the invocation layer's plan lease."""

    op: str | None
    params: Mapping[str, Any]
    preview: bool
    missing_required: tuple[str, ...]
    alternatives: tuple[str, ...]
    why: str
    fallback: bool = False


class IntentResolver:
    """Bounded router: the ranking port orders, the scope-partitioned cache bounds."""

    def __init__(
        self, *, cache_limit: int = _CACHE_LIMIT, ranking: RankingPort = FLOOR_ONLY
    ) -> None:
        if cache_limit < 1:
            raise ValueError("cache_limit must be positive")
        self._secret = secrets.token_bytes(32)
        self._cache_limit = cache_limit
        self._cache: OrderedDict[
            tuple[str, ...], tuple[tuple[Candidate, ...], Ranked]
        ] = OrderedDict()
        #: Medulla chooses the order; the lexical floor answers when it cannot.
        self._ranking: RankingPort = ranking

    def scope_ref(
        self,
        tenant: str,
        policy_revision: str,
        *,
        audience: str = "",
        scopes: Sequence[str] = (),
    ) -> str:
        """Opaque process-local reference for verified authority, never caller hints."""
        if not tenant or not policy_revision:
            raise ValueError("Verified tenant and policy revision are required")
        authority = json.dumps(
            [tenant, policy_revision, audience, sorted(scopes)], separators=(",", ":")
        ).encode()
        return hmac.digest(self._secret, authority, "sha256").hex()

    def _pool(
        self,
        verb: str,
        intent: str,
        descriptors: Sequence[Descriptor | Mapping[str, Any]],
        scope_ref: str | None,
        top_k: int,
    ) -> tuple[tuple[Descriptor, ...], tuple[str, ...], int]:
        if verb not in _VERBS:
            raise ValueError(f"Unknown intent verb: {verb}")
        visible = tuple(_coerce(item) for item in descriptors)
        limit = max(1, min(top_k, _TOP_K_CLAMP))
        key = (
            verb,
            _digest(" ".join(intent.lower().split())),
            _digest([_fingerprint(item) for item in visible]),
            scope_ref or "unverified",
            str(limit),
        )
        pool = tuple(item for item in visible if verb == "find" or item.verb == verb)
        return pool, key, limit

    def _cached(
        self, key: tuple[str, ...]
    ) -> tuple[tuple[Candidate, ...], Ranked] | None:
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
        return cached

    def _keep(
        self, key: tuple[str, ...], result: tuple[Candidate, ...], ranked: Ranked
    ) -> None:
        """Cache an engine order, or a floor order when no engine is bound at all."""
        if ranked.source == "medulla" or not getattr(
            self._ranking, "configured", False
        ):
            self._cache[key] = (result, ranked)
            self._cache.move_to_end(key)
            _evict_oldest(self._cache, self._cache_limit)

    def arrange(
        self,
        verb: str,
        intent: str,
        descriptors: Sequence[Descriptor | Mapping[str, Any]],
        *,
        scope_ref: str | None,
        top_k: int = 5,
    ) -> tuple[Candidate, ...]:
        """Order only the visible input snapshot; unseen items never enter cache."""
        pool, key, limit = self._pool(verb, intent, descriptors, scope_ref, top_k)
        cached = self._cached(key)
        if cached is not None:
            return cached[0]
        ordered = _port_outcome(self._ranking, intent, pool)
        result = _candidates_in_order(pool, ordered, intent, limit)
        self._keep(key, result, ordered)
        return result

    async def arrange_sourced(
        self,
        verb: str,
        intent: str,
        descriptors: Sequence[Descriptor | Mapping[str, Any]],
        *,
        scope_ref: str | None,
        top_k: int = 5,
    ) -> tuple[tuple[Candidate, ...], Ranked]:
        """The order of one request awaiting an engine ask, and who produced it."""
        pool, key, limit = self._pool(verb, intent, descriptors, scope_ref, top_k)
        cached = self._cached(key)
        if cached is not None:
            return cached
        ordered = await _port_outcome_async(self._ranking, intent, pool)
        result = _candidates_in_order(pool, ordered, intent, limit)
        self._keep(key, result, ordered)
        return result, ordered

    async def arrange_async(
        self,
        verb: str,
        intent: str,
        descriptors: Sequence[Descriptor | Mapping[str, Any]],
        *,
        scope_ref: str | None,
        top_k: int = 5,
    ) -> tuple[Candidate, ...]:
        """:meth:`arrange` awaiting an engine ask (the served path)."""
        result, _ = await self.arrange_sourced(
            verb, intent, descriptors, scope_ref=scope_ref, top_k=top_k
        )
        return result

    def resolve(
        self,
        verb: str,
        intent: str,
        descriptors: Sequence[Descriptor | Mapping[str, Any]],
        *,
        scope_ref: str | None,
        params: Mapping[str, Any] | None = None,
    ) -> Resolution:
        """Select an NL op; mutations always require a reviewed preview first."""
        ranked = self.arrange(verb, intent, descriptors, scope_ref=scope_ref)
        supplied = dict(params or {})
        graph_question = bool(_GRAPH_QUESTION.search(intent.lower()))
        if verb == "find":
            ids = tuple(candidate.descriptor.id for candidate in ranked)
            if graph_question:
                visible = {_coerce(item).id for item in descriptors}
                first = tuple(op for op in _GRAPH_OPS if op in visible)
                ids = (*first, *(i for i in ids if i not in first))
            return Resolution(
                None,
                supplied,
                False,
                (),
                ids,
                "Discovery results; no operation executed",
            )
        if verb == "act" and _DELEGATION.search(intent.lower()):
            delegated = _delegate_route(supplied, descriptors)
            if delegated is not None:
                return delegated
        if verb == "ask" and _AGENT_LIBRARY_QUESTION.search(intent.lower()):
            library = _agent_library_route(supplied, descriptors)
            if library is not None:
                return library
        if verb == "ask":
            execution = _execution_answer_route(intent, supplied, descriptors)
            if execution is not None:
                return execution
        if verb == "ask" and graph_question and "text" not in supplied:
            routed = _graph_question_route(intent, supplied, descriptors)
            if routed is not None:
                return routed
        if verb == "ask" and not supplied and _nl_visible(descriptors):
            answering = _plain_question_answers(ranked, intent)
            if not answering:
                return replace(
                    _ask_fallback(verb, intent, supplied, descriptors),
                    alternatives=tuple(c.descriptor.id for c in ranked),
                )
            ranked = answering
        winner = ranked[0] if ranked else None
        if winner is None or winner.score < _MIN_EVIDENCE:
            return _ask_fallback(verb, intent, supplied, descriptors)
        if verb == "ask" and _missing_required(
            winner.descriptor.params_schema, supplied
        ):
            # ARBORA-FLEET-R014.1: a plain question cannot fill a required
            # parameter, so an op that needs one does not win `ask`. The next
            # runnable match does; with none, the NL planner answers.
            runnable = next(
                (
                    candidate
                    for candidate in ranked
                    if candidate.score >= _MIN_EVIDENCE
                    and not _missing_required(
                        candidate.descriptor.params_schema, supplied
                    )
                ),
                None,
            )
            if runnable is None:
                fallback = _ask_fallback(verb, intent, supplied, descriptors)
                if fallback.op is not None:
                    return replace(
                        fallback,
                        alternatives=tuple(c.descriptor.id for c in ranked),
                    )
            else:
                winner = runnable
        selected = winner.descriptor
        missing = _missing_required(selected.params_schema, supplied)
        return Resolution(
            op=selected.id,
            params=supplied,
            preview=verb not in _READ_VERBS,
            missing_required=missing,
            alternatives=tuple(
                candidate.descriptor.id
                for candidate in ranked
                if candidate.descriptor.id != selected.id
            ),
            why=f"Matched {', '.join(winner.matched_terms) or 'semantic similarity'}",
        )

    async def resolve_async(
        self,
        verb: str,
        intent: str,
        descriptors: Sequence[Descriptor | Mapping[str, Any]],
        *,
        scope_ref: str | None,
        params: Mapping[str, Any] | None = None,
    ) -> Resolution:
        """:meth:`resolve` after awaiting the engine's order (the served path).

        The awaited order lands in the scope-partitioned cache, which the
        synchronous resolution then reads; a floor answer is never cached when an
        engine is bound, so the resolution recomputes the same floor order.
        """
        await self.arrange_async(verb, intent, descriptors, scope_ref=scope_ref)
        return self.resolve(
            verb, intent, descriptors, scope_ref=scope_ref, params=params
        )


def _coerce(item: Descriptor | Mapping[str, Any]) -> Descriptor:
    return item if isinstance(item, Descriptor) else Descriptor.from_mapping(item)


def _text(item: Descriptor) -> str:
    return " ".join((item.summary, *item.examples, item.domain, *item.tags))


def _port_candidates(pool: Sequence[Descriptor]) -> list[RankCandidate]:
    return [RankCandidate(item.id, item.id, _text(item), "operation") for item in pool]


def _port_outcome(
    ranking: RankingPort, intent: str, pool: Sequence[Descriptor]
) -> Ranked:
    candidates = _port_candidates(pool)
    ordered = getattr(ranking, "ordered", None)
    if ordered is not None:
        return ordered(_RANK_QUESTION, intent, candidates)
    return Ranked(ranking.rank(_RANK_QUESTION, intent, candidates), "medulla")


async def _port_outcome_async(
    ranking: RankingPort, intent: str, pool: Sequence[Descriptor]
) -> Ranked:
    candidates = _port_candidates(pool)
    ordered = getattr(ranking, "aordered", None)
    if ordered is not None:
        return await ordered(_RANK_QUESTION, intent, candidates)
    return Ranked(ranking.rank(_RANK_QUESTION, intent, candidates), "medulla")


def _candidates_in_order(
    pool: Sequence[Descriptor], ordered: Ranked, intent: str, limit: int
) -> tuple[Candidate, ...]:
    """The port's order, clamped to ``limit`` after it, with its evidence.

    A candidate is evidenced when the engine put it first or the request shares a
    routing term with it; the port, not this function, chose the order.
    """
    wanted = frozenset(_tokens(intent))
    by_id = {item.id: item for item in pool}
    out: list[Candidate] = []
    for index, op_id in enumerate(i for i in ordered.order if i in by_id):
        item = by_id[op_id]
        shared = wanted & (
            set(_WORDS.findall(item.id.lower())) | set(_tokens(_text(item)))
        )
        evidenced = bool(shared) or (ordered.source == "medulla" and index == 0)
        out.append(Candidate(item, 1.0 if evidenced else 0.0, tuple(sorted(shared))))
    return tuple(out[:limit])


def _evict_oldest(cache: OrderedDict[Any, Any], limit: int) -> None:
    while len(cache) > limit:
        cache.popitem(last=False)


def _fingerprint(item: Descriptor) -> tuple[object, ...]:
    return (
        item.id,
        item.verb,
        item.summary,
        item.examples,
        item.domain,
        item.tags,
        item.kind,
        item.params_schema,
    )


def _missing_required(
    schema: Mapping[str, Any], params: Mapping[str, Any]
) -> tuple[str, ...]:
    required = schema.get("required", ())
    return tuple(sorted(str(name) for name in required if name not in params))


def _nl_visible(descriptors: Sequence[Descriptor | Mapping[str, Any]]) -> bool:
    return any(
        item.id == "query.nl" and item.verb == "ask"
        for item in (_coerce(entry) for entry in descriptors)
    )


def _engine_method(item: Descriptor) -> bool:
    """A generated engine method op: structured params, never a text answer."""
    return item.domain == "eg" or item.id.startswith("eg.")


def _plain_question_answers(
    ranked: Sequence[Candidate], intent: str
) -> tuple[Candidate, ...]:
    """The ranked ops that may answer a plain question, in rank order.

    ARBORA-FLEET-R014.3: a question with no parameters is a data, catalog,
    lineage or ownership question for the engine planner unless an op names
    it. A generated engine method needs structured parameters, so it never
    answers one. Another op answers only when it matches at least half of the
    question's content terms; one shared word ("agents", "request") is not
    evidence.
    """
    terms = set(_tokens(intent))
    if not terms:
        return ()
    return tuple(
        candidate
        for candidate in ranked
        if not _engine_method(candidate.descriptor)
        and len(set(candidate.matched_terms)) >= _PLAIN_QUESTION_COVERAGE * len(terms)
    )


def _delegate_route(
    params: Mapping[str, Any],
    descriptors: Sequence[Descriptor | Mapping[str, Any]],
) -> Resolution | None:
    """Route a delegation intent to the governed ``fleet.call`` the caller can see.

    The text names no tool, so the call's ``server`` and ``tool`` stay required
    parameters the caller supplies, from ``find`` or the catalog. Admission,
    the effect authority and the plan gate all run on the op as for any call.
    """
    for item in (_coerce(entry) for entry in descriptors):
        if item.id == _DELEGATE_OP and item.verb == "act":
            return Resolution(
                item.id,
                params,
                False,
                _missing_required(item.params_schema, params),
                (),
                "Delegation: a governed fleet call to the named tool",
            )
    return None


def _agent_library_route(
    params: Mapping[str, Any],
    descriptors: Sequence[Descriptor | Mapping[str, Any]],
) -> Resolution | None:
    """Route an Agent Library question to the engine's ``AgentLibrary`` read.

    Only the authorized op the caller can see qualifies, so scope and policy
    filtering already happened. A plain question cannot fill the op's required
    parameters, so they come back as ``missing_required``.
    """
    for item in (_coerce(entry) for entry in descriptors):
        if item.id == _AGENT_LIBRARY_OP and item.verb == "ask":
            return Resolution(
                item.id,
                params,
                False,
                _missing_required(item.params_schema, params),
                (),
                "Agent Library question: read from the engine's AgentLibrary method",
            )
    return None


def _execution_answer_route(
    intent: str,
    params: Mapping[str, Any],
    descriptors: Sequence[Descriptor | Mapping[str, Any]],
) -> Resolution | None:
    """Route ``how can I execute task <iri>`` to the engine's execution answer.

    The seven parts are the engine's; arbora composes none of them and holds no
    planner. Only the authorized generated op the caller can see qualifies.
    """
    matched = _EXECUTE_TASK_QUESTION.match(intent.strip())
    if matched is None:
        return None
    iri = matched.group(1)
    for item in (_coerce(entry) for entry in descriptors):
        if item.verb == "ask" and _EXECUTION_ANSWER_OP.match(item.id):
            request = {**dict(params.get("request") or {}), "task_iri": iri}
            filled = {**params, "request": request}
            return Resolution(
                item.id,
                filled,
                False,
                _missing_required(item.params_schema, filled),
                (),
                "Execution question: the engine's composed AgentExecutionAnswer",
            )
    return None


def _graph_question_route(
    intent: str,
    params: Mapping[str, Any],
    descriptors: Sequence[Descriptor | Mapping[str, Any]],
) -> Resolution | None:
    """Route a catalog-class question to the engine planner, ahead of ranking.

    Only the authorized ``query.nl`` the caller can see qualifies, so scope and
    policy filtering already happened. The text goes through unchanged and the
    engine binds tenant, row caps and provenance (ARBORA-OPS-R855.4).
    """
    visible = tuple(_coerce(item) for item in descriptors)
    if not any(item.id == "query.nl" and item.verb == "ask" for item in visible):
        return None
    return Resolution(
        "query.nl",
        {**params, "text": intent},
        False,
        (),
        tuple(i.id for i in visible if i.id == "query.uql"),
        "Catalog, lineage or virtual-graph question: answered by the engine planner over its ontology",
    )


def _ask_fallback(
    verb: str,
    intent: str,
    params: Mapping[str, Any],
    descriptors: Sequence[Descriptor | Mapping[str, Any]],
) -> Resolution:
    visible = tuple(_coerce(item) for item in descriptors)
    has_nl = any(item.id == "query.nl" and item.verb == "ask" for item in visible)
    if verb != "ask" or not has_nl:
        return Resolution(
            None, params, False, (), (), "No matching authorized operation"
        )
    return Resolution(
        "query.nl",
        {**params, "text": intent},
        False,
        (),
        (),
        "Using the authorized engine natural-language planner",
        True,
    )
